"""Extract sections from FDA guidance PDFs into JSONL (same schema as extract_cfr.py).

Sections follow the document's own numbered headings (I., A., 1., (1), 2.1, Q1., ...)
and the unnumbered headings listed in its table of contents (Appendix, Glossary, ...).
Cover and table of contents are skipped; repeated headers/footers, page numbers
and line numbers are removed; footnotes are appended to the section that cites them.

Usage: python scripts/extract_pdf.py [--doc DOC_ID ...]
"""
import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

import pdfplumber

from extract_cfr import (CORPUS, MANIFEST, OUT_DIR, RAW_DIR, check_unique_ids, clean,
                         load_manifest)

LONG_SECTION = 15_000          # chars: warn above this
# Some spaces are missing from the PDFs' text and only visible as a gap. The default
# 3pt tolerance glues such words ("Thefollowing", gap 2.5-3pt in guid-cds); real
# word gaps without a space go down to ~1.7pt (guid-part11-scope), so split at 1.5pt.
# A change of font or size also splits words (superscripts become separate words).
WORD_OPTS = {"x_tolerance": 1.5, "extra_attrs": ["fontname", "size"]}
JOIN_GAP = 0.5                 # pt: touching words are joined without a space
HYPHEN_GAP = 0.2               # x font size: "Risk" + "-Based" -> "Risk-Based"
HEADER_BAND = 0.10             # top 10% of the page
FOOTER_BAND = 0.90             # bottom 10% of the page
REPEAT_RATIO = 0.5             # band line on >= 50% of body pages = header/footer
KNOWN_HEADERS = re.compile(
    r"^(contains nonbinding recommendations|draft\s*[-–—]\s*not for implementation)$", re.I)
PAGE_NUMBER = re.compile(r"^(?:page\s+)?(\d+)(?:\s+of\s+\d+)?$", re.I)
TOC_PAGE_HINT = re.compile(r"\.{4,}\s*\d*$")                       # strong leader: TOC page
TOC_ENTRY = [re.compile(r"^(?P<title>.*?\S)\s*(?:\.\s*){2,}(?P<page>\d*)$"),  # "Title ..... 12"
             re.compile(r"^(?P<title>.*?\S)\s+\.\s+(?P<page>\d+)$")]           # "Title . 12"
TOC_TITLE = re.compile(r"^table of contents$", re.I)
NUMBERING = [  # (type, regex); "n" is the number as printed
    ("decimal", re.compile(r"^(?P<n>\d+(?:\.\d+)+)\.?\s+(?P<t>\S.*)$")),
    ("decimal", re.compile(r"^SECTION\s+(?P<n>\d+)\.?\s+(?P<t>\S.*)$")),
    # "A.Examples": the space after the number may be missing (then a capitalised word follows)
    ("roman", re.compile(r"^(?P<n>[IVX]+)\.(?:\s+|(?=[A-Z][a-z]))(?P<t>\S.*)$")),
    ("upper", re.compile(r"^(?P<n>[A-Z])\.(?:\s+|(?=[A-Z][a-z]))(?P<t>\S.*)$")),
    ("digit", re.compile(r"^(?P<n>\d+)\.(?:\s+|(?=[A-Z][a-z]))(?P<t>\S.*)$")),
    ("lower", re.compile(r"^(?P<n>[a-z])\.\s+(?P<t>\S.*)$")),
    ("paren", re.compile(r"^(?P<n>\((?:\d+|[a-z])\))\s*(?P<t>[A-Za-z].*)$")),
    ("question", re.compile(r"^(?P<n>Q\d+)\.\s+(?P<t>\S.*)$")),
]
APPENDIX = re.compile(r"^(?P<n>(?:appendix|attachment|annex)\s+[A-Z0-9]+)\b", re.I)
UNNUMBERED_TITLES = {"glossary", "references", "definitions", "bibliography"}
LABEL = re.compile(r"^(?P<n>\w+\s+\d+[A-Za-z]?)\s*[:.]\s")      # "Example 1: ..."
BULLET = re.compile(r"^(?:[•·▪●◦○■□\-–]|o(?=\s+[A-Z]))\s")  # "o": Word level-2 bullet
# Word Symbol/Wingdings bullets are private-use characters: map them to Unicode.
PUA_BULLETS = str.maketrans({"": "•", "": "▪", "": "□", "": "➢",
                             "": "✓", "": "❖"})
SENTENCE_END = re.compile(r"[.:;!?)\]]$")
FOOTNOTE_MARK = re.compile(r"\[\d+\]")
# "pre- and post-market"; a whole word, so a URL ending "development-" + "and-use..." joins
SUSPENDED_HYPHEN = re.compile(r"^(?:and/or|and|or|nor|to)(?=[\s,]|$)")
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
         "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15}


def norm(text: str) -> str:
    """Comparison key for headings: lowercase alphanumerics only, no footnote marks."""
    return re.sub(r"[^a-z0-9]", "", FOOTNOTE_MARK.sub("", text).lower())


def numbering_type(text: str) -> str:
    return next((kind for kind, rx in NUMBERING if rx.match(text)), "unnumbered")


def is_styled(fontname: str) -> bool:
    return any(s in fontname for s in ("Bold", "Italic", "Oblique", "Black", "Semibold"))


def weighted_mode(pairs) -> float:
    """Most common value, weighted (value, weight); ties broken by smallest value."""
    c = Counter()
    for value, weight in pairs:
        c[value] += weight
    return min(c, key=lambda v: (-c[v], v)) if c else 0.0


def inside(w: dict, bbox) -> bool:
    cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


# ---------------------------------------------------------------- page reading

def find_line_number_words(pages_words: list[list[dict]], body_left: float) -> list[set[int]]:
    """Indexes of digit-only words in the left margin, if the document is line-numbered."""
    per_page = [{i for i, w in enumerate(words)
                 if w["text"].isdigit() and w["x1"] < body_left - 4}
                for words in pages_words]
    total = sum(len(s) for s in per_page)
    # Guard: only a real line-numbered document has many of them.
    return per_page if total >= 3 * len(pages_words) else [set() for _ in pages_words]


def _overlap(a: dict, b: dict) -> float:
    return min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])


def group_lines(words: list[dict]) -> list[list[dict]]:
    """Group words into lines by vertical overlap.

    Lines are built from "anchor" words (2+ characters, not smaller than their
    neighbours) first; then single characters, symbols and superscripts join the
    line they overlap most. Otherwise a bullet glyph with a tall box between two
    lines would chain them into one line with interleaved words."""
    is_anchor = [len(w["text"]) >= 2 and not _is_superscript(w, words) for w in words]

    lines: list[dict] = []
    for w in sorted((w for w, a in zip(words, is_anchor) if a),
                    key=lambda w: (round(w["top"], 1), w["x0"])):
        for line in reversed(lines[-3:]):
            if _overlap(line, w) >= 0.3 * min(line["bottom"] - line["top"], w["bottom"] - w["top"]):
                line["words"].append(w)
                line["top"] = min(line["top"], w["top"])
                line["bottom"] = max(line["bottom"], w["bottom"])
                break
        else:
            lines.append({"top": w["top"], "bottom": w["bottom"], "words": [w]})
    for w in sorted((w for w, a in zip(words, is_anchor) if not a),
                    key=lambda w: (round(w["top"], 1), w["x0"])):
        best = max(lines, key=lambda l: (_overlap(l, w), -l["top"]), default=None)
        if best is not None and _overlap(best, w) > 0:
            best["words"].append(w)
        else:
            lines.append({"top": w["top"], "bottom": w["bottom"], "words": [w]})
    lines.sort(key=lambda l: (round(l["top"], 1), min(w["x0"] for w in l["words"])))
    return [sorted(l["words"], key=lambda w: w["x0"]) for l in lines]


def _is_superscript(w: dict, words: list[dict]) -> bool:
    """True if `w` is smaller and raised relative to a nearby larger word on its line."""
    return any(v["size"] > 1.15 * w["size"] and v["bottom"] > w["bottom"] + 1
               and _overlap(v, w) > 0
               and min(abs(v["x0"] - w["x1"]), abs(w["x0"] - v["x1"])) < 40
               for v in words)


def make_line(page_no: int, words: list[dict]) -> dict:
    """Build a line: text (superscript digits as [n]), size, style."""
    size = weighted_mode((round(w["size"], 1), len(w["text"])) for w in words)
    main = [w for w in words if abs(w["size"] - size) < 0.5]
    main_bottom = max(w["bottom"] for w in main)
    text, prev, first_is_marker = "", None, False
    styled_chars = total_chars = 0
    first_styled = None  # style of the first normal word (a heading number must be styled)
    for w in words:
        sup = w["size"] < 0.85 * size and w["bottom"] < main_bottom - 1
        t = w["text"].replace("­", "-").translate(PUA_BULLETS)  # soft hyphen, bullets
        if sup and re.fullmatch(r"\d+(,\d+)*,?", t):
            t = "".join(f"[{n}]" for n in t.strip(",").split(","))  # "14,15" -> "[14][15]"
            if prev is None:
                first_is_marker = True
                t += " "
            sep = ""
        elif sup or prev is None or w["x0"] - prev["x1"] < JOIN_GAP or (
                re.match(r"-\w", t) and w["x0"] - prev["x1"] < HYPHEN_GAP * size):
            sep = ""  # touching words ("21" + "st"), or start of line
        else:
            sep = " "
        text += sep + t
        prev = w
        if not sup:
            if first_styled is None:
                first_styled = is_styled(w["fontname"])
            total_chars += len(w["text"])
            styled_chars += len(w["text"]) if is_styled(w["fontname"]) else 0
    return {
        "kind": "line", "page": page_no,
        "top": min(w["top"] for w in words), "bottom": max(w["bottom"] for w in words),
        "x0": words[0]["x0"], "size": size, "text": " ".join(text.split()),
        "styled": total_chars > 0 and styled_chars >= 0.8 * total_chars,
        "first_styled": bool(first_styled),
        "starts_with_marker": first_is_marker,
    }


def contains(outer, inner, tol: float = 1.0) -> bool:
    return (outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1]
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def table_item(page_no: int, table, words: list[dict]) -> dict | None:
    """A table as text, one line per row, cells joined by " | ".

    Word often draws a shaded box behind each text line of a cell, which pdfplumber
    reads as sub-cells. So: keep only cells not inside another cell, group them into
    row bands (overlapping vertical ranges), and join each column's pieces in a band.
    Returns None for a one-column box: its text stays in the running text."""
    cells = sorted({tuple(c) for row in table.rows for c in row.cells if c})
    cells = [c for c in cells if c[2] - c[0] >= 8 and c[3] - c[1] >= 5]  # double borders
    cells = [c for c in cells if not any(o != c and contains(o, c) for o in cells)]
    in_box = [w for w in words if inside(w, table.bbox)]
    if any(not any(inside(w, c) for c in cells) for w in in_box):
        # Text in the table's box but in no cell: not a reliable table (e.g. boxes
        # drawn around prose). Keep it all as running text rather than lose words.
        return {"kind": "rejected"}

    bands: list[list] = []  # [top, bottom, [cells]]
    for c in sorted(cells, key=lambda c: (c[1], c[0])):
        if bands and c[1] < bands[-1][1] - 1:
            bands[-1][1] = max(bands[-1][1], c[3])
            bands[-1][2].append(c)
        else:
            bands.append([c[1], c[3], [c]])

    rows, n_cols = [], 0
    for _, _, band in bands:
        columns: dict[int, list] = {}
        for c in sorted(band, key=lambda c: (c[1], c[0])):
            columns.setdefault(round(c[0]), []).append(c)
        cells = []  # each cell: its text lines, joined later by join_lines (needs vocab)
        for x in sorted(columns):
            ws = [w for w in words if any(inside(w, c) for c in columns[x])]
            cells.append([make_line(page_no, l)["text"] for l in group_lines(ws)])
        while cells and not cells[-1]:
            cells.pop()
        while cells and not cells[0]:
            cells.pop(0)
        if cells:
            rows.append(cells)
            n_cols = max(n_cols, len(cells))
    if n_cols <= 1:
        return None
    x0, top, x1, bottom = table.bbox
    return {"kind": "table", "page": page_no, "top": top, "bottom": bottom, "x0": x0,
            "rows": rows,
            "text": " ".join(line for row in rows for cell in row for line in cell)}


def read_pdf(path: Path) -> dict:
    """Return pages as lists of items (lines and tables), plus document-level stats."""
    with pdfplumber.open(path) as pdf:
        heights = [p.height for p in pdf.pages]
        raw = [p.extract_words(**WORD_OPTS) for p in pdf.pages]
        all_words = [w for ws in raw for w in ws]
        body_size = weighted_mode((round(w["size"], 1), len(w["text"])) for w in all_words)
        body_left = weighted_mode((round(w["x0"]), 1) for w in all_words
                                  if not w["text"].isdigit())
        line_numbers = find_line_number_words(raw, body_left)

        pages, n_line_numbers, n_tables, rejected_tables = [], 0, 0, []
        for i, page in enumerate(pdf.pages):
            boxes = [(w["x0"], w["top"], w["x1"], w["bottom"])
                     for j, w in enumerate(raw[i]) if j in line_numbers[i]]
            n_line_numbers += len(boxes)

            def keep(obj, boxes=boxes):
                if obj.get("object_type") != "char":
                    return True
                return not any(inside(obj, b) for b in boxes)

            fpage = page.filter(keep) if boxes else page
            words = fpage.extract_words(**WORD_OPTS)
            items = []
            tables = fpage.find_tables()
            # Shaded lines inside a cell can also come out as small nested tables.
            tables = [tb for tb in tables
                      if not any(o is not tb and contains(o.bbox, tb.bbox) for o in tables)]
            for tb in tables:
                item = table_item(i + 1, tb, words)
                if item and item["kind"] == "rejected":
                    rejected_tables.append(i + 1)
                elif item:  # real table: its words leave the running text
                    items.append(item)
                    words = [w for w in words if not inside(w, tb.bbox)]
            n_tables += len(items)
            items += [make_line(i + 1, ws) for ws in group_lines(words)]
            items.sort(key=lambda it: (it["top"], it["x0"]))
            pages.append(items)
    return {"pages": pages, "heights": heights, "body_size": body_size,
            "line_numbers": n_line_numbers, "tables": n_tables,
            "rejected_tables": rejected_tables}


# ---------------------------------------------------------------- page structure

def in_band(it: dict, h: float) -> bool:
    return it["kind"] == "line" and (it["bottom"] < HEADER_BAND * h or it["top"] > FOOTER_BAND * h)


def find_toc(pages: list[list[dict]], heights: list[float]) -> tuple[list[int], list[dict]]:
    """TOC page indexes (0-based) and entries [{title, page, x0, type}]."""
    toc_pages, entries = [], []
    for i, items in enumerate(pages):
        lines = [it for it in items if it["kind"] == "line" and not in_band(it, heights[i])]
        hits = sum(bool(TOC_PAGE_HINT.search(l["text"])) for l in lines)
        if hits < 3 and not any(TOC_TITLE.match(l["text"]) for l in lines):
            if toc_pages:
                break  # TOC is contiguous
            continue
        toc_pages.append(i)
        pending = []  # first lines of a wrapped entry
        for l in lines:
            if TOC_TITLE.match(l["text"]) or KNOWN_HEADERS.match(l["text"]):
                continue
            m = next((m for rx in TOC_ENTRY if (m := rx.match(l["text"]))), None)
            if m:
                title = clean(" ".join([p["text"] for p in pending] + [m["title"]]))
                x0 = (pending[0] if pending else l)["x0"]
                entries.append({"title": title, "page": int(m["page"]) if m["page"] else None,
                                "x0": x0, "type": numbering_type(title)})
                pending = []
            else:
                pending.append(l)
    return toc_pages, entries


def remove_headers_footers(pages, heights, body_pages) -> tuple[dict[int, int], int]:
    """Drop repeated header/footer lines on body pages, in place.

    Returns ({physical page: printed page number}, number of lines removed)."""
    keys = Counter()
    for i in body_pages:
        keys.update({re.sub(r"\d+", "#", it["text"].lower())
                     for it in pages[i] if in_band(it, heights[i])})
    repeated = {k for k, n in keys.items() if n >= max(2, REPEAT_RATIO * len(body_pages))}

    printed, removed = {}, 0
    for i in body_pages:
        kept = []
        for it in pages[i]:
            if in_band(it, heights[i]):
                m = PAGE_NUMBER.match(it["text"])
                if m:
                    printed[i + 1] = int(m[1])
                if m or KNOWN_HEADERS.match(it["text"]) \
                        or re.sub(r"\d+", "#", it["text"].lower()) in repeated:
                    removed += 1
                    continue
            kept.append(it)
        pages[i] = kept
    return printed, removed


def split_footnotes(pages, heights, body_pages, body_size) -> dict[int, list[dict]]:
    """Move footnote lines out of the page flow, in place. Returns {page: [notes]}."""
    notes_by_page, last_note = {}, None
    for i in body_pages:
        items = pages[i]
        start = len(items)
        while (start > 0 and items[start - 1]["kind"] == "line"
               and items[start - 1]["size"] < 0.92 * body_size
               and items[start - 1]["top"] > 0.2 * heights[i]):
            start -= 1
        zone = items[start:]
        if not zone:
            continue
        if not zone[0]["starts_with_marker"]:
            if last_note is not None and zone[0]["top"] > 0.5 * heights[i]:
                pass  # continuation of the previous page's last footnote
            else:  # small text above the footnotes (or none): start at the first marker
                k = next((k for k, l in enumerate(zone) if l["starts_with_marker"]), None)
                if k is None:
                    continue
                start += k
                zone = zone[k:]
        notes = []
        for line in zone:
            m = re.match(r"^\[(\d+)\] ?(.*)$", line["text"]) if line["starts_with_marker"] else None
            if m:
                last_note = {"n": int(m[1]), "page": i + 1, "lines": [m[2]]}
                notes.append(last_note)
            else:
                last_note["lines"].append(line["text"])  # continuation (same or previous page)
        pages[i] = items[:start]
        notes_by_page[i + 1] = notes
    return notes_by_page


# ---------------------------------------------------------------- text assembly

def build_vocab(pages, body_pages) -> Counter:
    """Words seen inside lines (not at a line end), to decide on line-end hyphens."""
    vocab = Counter()
    for i in body_pages:
        for it in pages[i]:
            tokens = it["text"].split()[:-1]
            vocab.update(re.sub(r"^\W+|\W+$", "", t).lower() for t in tokens)
    return vocab


def join_lines(texts: list[str], vocab: Counter, stats: Counter) -> str:
    """Join wrapped lines (paragraphs, table cells, footnotes, headings).

    A line ending with "/" or a hyphen is joined without a space ("failures/deviations",
    "AI-DSF"). The hyphen is dropped only if the joined word is attested elsewhere in
    the document and the hyphenated one is not. Suspended hyphens keep their space:
    "pre- and post-market", "Internet- and network-connected"."""
    out = ""
    for t in texts:
        if not out:
            out = t
            continue
        m = re.search(r"(\w+)-$", out)
        if out.endswith("/") and re.search(r"\w/$", out):
            out += t
            stats["slash_joined"] += 1
        elif m and SUSPENDED_HYPHEN.match(t):
            out += " " + t
            stats["hyphen_suspended"] += 1
        elif m:
            nxt = re.match(r"^([a-z]\w*)", t)
            if nxt and vocab[f"{m[1]}{nxt[1]}".lower()] and not vocab[f"{m[1]}-{nxt[1]}".lower()]:
                out = out[:-1] + t
                stats["hyphen_removed"] += 1
            else:
                out += t
                stats["hyphen_kept"] += 1
        else:
            out += " " + t
    return out


def table_text(rows: list[list[list[str]]], vocab: Counter, stats: Counter) -> list[str]:
    """Table rows as text: cells joined by " | ", lines of a cell joined by join_lines."""
    return [" | ".join(join_lines(cell, vocab, stats) for cell in row) for row in rows]


def paragraphs(items: list[dict], body_size: float, vocab: Counter, stats: Counter) -> list[str]:
    paras, current, prev = [], [], None
    table_header, table_page = None, None  # first row and last page of the table in progress
    for it in items:
        if it["kind"] == "table":
            if current:
                paras.append(join_lines(current, vocab, stats))
            rows = table_text(it["rows"], vocab, stats)
            # A table continued on the next page repeats its header row: keep the first only.
            continued = table_page is not None and it["page"] == table_page + 1
            if continued and rows and rows[0] == table_header:
                rows = rows[1:]
                stats["table_header_dropped"] += 1
            elif not continued:
                table_header = rows[0] if rows else None
            table_page = it["page"]
            if rows:
                paras.append("\n".join(rows))
            current, prev = [], None
            continue
        table_header, table_page = None, None
        new_para = prev is None or BULLET.match(it["text"]) or (
            it["page"] == prev["page"] and it["top"] - prev["bottom"] > 0.5 * body_size) or (
            it["page"] != prev["page"] and SENTENCE_END.search(prev["text"]))
        if new_para and current:
            paras.append(join_lines(current, vocab, stats))
            current = []
        current.append(it["text"])
        prev = it
    if current:
        paras.append(join_lines(current, vocab, stats))
    return [clean(p) if "\n" not in p else p for p in paras]


# ---------------------------------------------------------------- headings

class HeadingParser:
    """Recognise headings and keep the stack of open headings.

    A numbered heading whose numbering type is already open is a sibling of that
    heading (deeper ones are closed); otherwise it is a child of the innermost one.
    Unnumbered headings come from the TOC (or Appendix/Glossary): top-level if at
    the TOC's left margin, otherwise nested by font size."""

    def __init__(self, body_size: float, toc_entries: list[dict]):
        self.body_size = body_size
        self.toc_left = min((e["x0"] for e in toc_entries), default=None)
        self.toc_unnumbered = {norm(e["title"]): e["x0"] for e in toc_entries
                               if e["type"] == "unnumbered"}
        self.stack: list[dict] = []   # {type, depth, size, id, index}
        self.last_roman = 0

    def match(self, line: dict) -> dict | None:
        """Return {type, n, text, top} if the line is a heading, else None."""
        if line["kind"] != "line" or line["size"] < 0.92 * self.body_size:
            return None
        larger = line["size"] > 1.1 * self.body_size
        if not (line["styled"] or larger):
            return None
        text = FOOTNOTE_MARK.sub("", line["text"]).strip()
        for kind, rx in NUMBERING:
            m = rx.match(text)
            if m:
                if not (line["first_styled"] or larger):
                    return None  # e.g. "2. <italic book title>" in a reference list
                if kind == "upper" and m["n"] in ROMAN and self.last_roman and \
                        ROMAN[m["n"]] == self.last_roman + 1:
                    kind = "roman"
                elif kind == "roman" and self.last_roman and len(m["n"]) == 1 and \
                        ROMAN.get(m["n"], 0) != self.last_roman + 1:
                    kind = "upper"  # e.g. "I." after "H." under a roman heading
                return {"type": kind, "n": m["n"], "text": text, "top": False}
        key = norm(text)
        if key not in self.toc_unnumbered and len(key) >= 20:
            # First line of a wrapped heading: the TOC title starts with it.
            key = min((k for k in self.toc_unnumbered if k.startswith(key)), default=key)
        if key in self.toc_unnumbered:
            top = self.toc_unnumbered[key] <= self.toc_left + 3 or bool(APPENDIX.match(text))
            m = LABEL.match(text) or APPENDIX.match(text)
            return {"type": "unnumbered", "n": m["n"] if m else text.strip(" :"),
                    "text": text, "top": top}
        m = APPENDIX.match(text)
        if (m and len(text) < 100) or text.lower().strip(" :") in UNNUMBERED_TITLES:
            return {"type": "unnumbered", "n": m["n"] if m else text.strip(" :"),
                    "text": text, "top": True}
        return None

    def push(self, h: dict, size: float, index: int) -> tuple[str, int | None]:
        """Open a heading; return (section_id, index of the parent heading)."""
        st, kind = self.stack, h["type"]
        depth = h["n"].count(".") + 1 if kind == "decimal" else 0
        if kind == "unnumbered":
            if h["top"]:
                st.clear()
            else:
                while st and st[-1]["size"] <= size + 0.1:
                    st.pop()
        elif kind == "decimal":
            while st and not (st[-1]["type"] == "decimal" and st[-1]["depth"] < depth) \
                    and not (depth > 1 and st[-1]["type"] == "unnumbered"):
                st.pop()
        elif any(s["type"] == kind for s in st):
            while st[-1]["type"] != kind:
                st.pop()
            st.pop()
        if kind == "roman":
            self.last_roman = ROMAN.get(h["n"], 0)

        parent = st[-1] if st else None
        if kind == "decimal" or parent is None or (kind == "unnumbered" and h["top"]):
            section_id = h["n"]
        elif kind == "paren":
            section_id = parent["id"] + h["n"]          # "V.A(1)", like "11.10(a)"
        else:
            section_id = f"{parent['id']}.{h['n']}"
        st.append({"type": kind, "depth": depth, "size": size, "id": section_id,
                   "index": index})
        return section_id, parent["index"] if parent else None


def continues_heading(prev: dict, line: dict, parser: HeadingParser) -> bool:
    """True if `line` is the wrapped second line of heading `prev`."""
    return (line["kind"] == "line" and line["page"] == prev["page"] and line["styled"]
            and abs(line["size"] - prev["size"]) < 0.5
            and line["top"] - prev["bottom"] < 0.5 * prev["size"]
            and parser.match(line) is None)


# ---------------------------------------------------------------- per document

def location(section_id: str, start: int, end: int) -> str:
    pages = f"p. {start}" if start == end else f"pp. {start}-{end}"
    if section_id == "preamble":
        return f"Preamble, {pages}"
    if re.match(r"^([IVX]+|[A-Z]|\d+)(\.|\(|$)", section_id):
        return f"Section {section_id}, {pages}"
    return f"{section_id}, {pages}"


def extract_pdf(doc_id: str, pdf_path: Path, source_sha256: str) -> tuple[list[dict], dict]:
    warnings: list[str] = []
    stats = Counter()
    doc = read_pdf(pdf_path)
    pages, heights, body_size = doc["pages"], doc["heights"], doc["body_size"]
    n_pages = len(pages)

    toc_pages, toc_entries = find_toc(pages, heights)
    if toc_pages:
        body_start = toc_pages[-1] + 1
        before = [i for i in range(toc_pages[0]) if i > 0]
    else:
        body_start = 1
        before = []
        warnings.append("no table of contents found: skipped page 1 as cover")
    for i in before:
        first = next((it["text"] for it in pages[i] if it["kind"] == "line"), "")
        warnings.append(f"p. {i + 1} before the TOC skipped as cover "
                        f"({len(pages[i])} lines, starts: {first[:60]!r})")
    body_pages = list(range(body_start, n_pages))
    for page_no in doc["rejected_tables"]:
        if page_no > body_start:
            warnings.append(f"p. {page_no}: table with text outside its cells, "
                            f"kept as plain text (row/column structure lost)")

    printed, n_headers = remove_headers_footers(pages, heights, body_pages)
    notes_by_page = split_footnotes(pages, heights, body_pages, body_size)
    vocab = build_vocab(pages, body_pages)

    # Walk the body flow and cut it at headings.
    parser = HeadingParser(body_size, toc_entries)
    raw_sections = [{"section_id": "preamble", "parent": None, "title": "Preamble", "type": "",
                     "items": [], "page_start": None, "markers": []}]
    prev_heading = None
    for i in body_pages:
        for it in pages[i]:
            if prev_heading is not None and continues_heading(prev_heading, it, parser):
                raw_sections[-1]["title"] = join_lines(
                    [raw_sections[-1]["title"], FOOTNOTE_MARK.sub("", it["text"]).strip()],
                    vocab, stats)
                raw_sections[-1]["markers"] += FOOTNOTE_MARK.findall(it["text"])
                prev_heading = it
                continue
            h = parser.match(it)
            if h:
                section_id, parent = parser.push(h, it["size"], len(raw_sections))
                # Footnote markers are removed from the title but kept to attach the note.
                raw_sections.append({"section_id": section_id, "parent": parent,
                                     "title": h["text"], "type": h["type"], "items": [],
                                     "page_start": it["page"],
                                     "markers": FOOTNOTE_MARK.findall(it["text"])})
                prev_heading = it
                continue
            prev_heading = None
            raw_sections[-1]["items"].append(it)
            if raw_sections[-1]["page_start"] is None:
                raw_sections[-1]["page_start"] = it["page"]

    # subpart = full heading path of the parents (titles may have wrapped).
    for s in raw_sections:
        path, p = [], s["parent"]
        while p is not None:
            path.append(raw_sections[p]["title"])
            p = raw_sections[p]["parent"]
        s["subpart"] = " > ".join(reversed(path))

    # Footnotes: attach each note to the section that cites [n].
    for s in raw_sections:
        s["paras"] = paragraphs(s["items"], body_size, vocab, stats)
        s["notes"] = []
        s["page_end"] = max([it["page"] for it in s["items"]] + [s["page_start"] or 0])
    n_notes = 0
    for page_no in sorted(notes_by_page):
        for note in notes_by_page[page_no]:
            n_notes += 1
            marker = f"[{note['n']}]"
            # Latest section citing [n] that starts on or before the note's page
            # (numbering can restart, e.g. in a reproduced appendix).
            target = next((s for s in reversed(raw_sections)
                           if s["page_start"] and s["page_start"] <= page_no
                           and (marker in s["markers"] or any(marker in p for p in s["paras"]))),
                          None)
            if target is None:
                target = next((s for s in reversed(raw_sections)
                               if s["page_start"] and s["page_start"] <= page_no), raw_sections[0])
                warnings.append(f"footnote {note['n']} (p. {page_no}): marker not found in text, "
                                f"attached to {target['section_id']}")
            target["notes"].append(f"{marker} " + clean(join_lines(note["lines"], vocab, stats)))
            target["page_end"] = max(target["page_end"], page_no)

    # A number printed twice in the PDF (e.g. two "2.4" in guid-gpsv): the second
    # becomes "2.4(2)", the title stays as printed.
    used: set[str] = set()
    for s in raw_sections:
        sid = s["section_id"]
        if sid in used:
            k = 2
            while f"{sid}({k})" in used:
                k += 1
            s["section_id"] = f"{sid}({k})"
            warnings.append(f"section number {sid!r} printed more than once in the PDF: "
                            f"{s['title']!r} (p. {s['page_start']}) written as {s['section_id']!r}")
        used.add(s["section_id"])

    sections = []
    for k, s in enumerate(raw_sections):
        text = "\n".join(s["paras"] + s["notes"])
        if not text:
            if s["section_id"] != "preamble" or s["page_start"]:
                has_children = any(o["parent"] == k for o in raw_sections)
                why = "heading only, kept in its subsections' subpart" if has_children else "no text"
                warnings.append(f"section {s['section_id']} ({s['title']!r}) is empty ({why}), "
                                f"not written")
            continue
        if len(text) > LONG_SECTION:
            warnings.append(f"section {s['section_id']} is very long ({len(text)} chars)")
        sections.append({
            "doc_id": doc_id,
            "section_id": s["section_id"],
            "location": location(s["section_id"], s["page_start"], s["page_end"]),
            "subpart": s["subpart"],
            "title": s["title"],
            "text": text,
            "citation": "",
            "char_count": len(text),
            "source_sha256": source_sha256,
            "page_start": s["page_start"],
            "page_end": s["page_end"],
        })

    # Cross-check headings against the table of contents.
    if toc_entries:
        found = {norm(s["title"]) for s in raw_sections}
        for e in toc_entries:
            if norm(e["title"]) not in found:
                warnings.append(f"TOC entry not found as a heading: {e['title']!r} (p. {e['page']})")
        toc_keys = {norm(e["title"]) for e in toc_entries}
        toc_types = {e["type"] for e in toc_entries}
        for s in raw_sections[1:]:
            if s["type"] in toc_types and norm(s["title"]) not in toc_keys:
                warnings.append(f"heading not in TOC: {s['title']!r} (p. {s['page_start']})")

    offsets = Counter(p - n for p, n in printed.items())
    info = {
        "pages": n_pages,
        "body": (body_start + 1, n_pages),
        "skipped": list(range(1, body_start + 1)),
        "toc_pages": [i + 1 for i in toc_pages],
        "offsets": offsets,
        "unnumbered_pages": [i + 1 for i in body_pages if i + 1 not in printed],
        "headers_removed": n_headers,
        "line_numbers_removed": doc["line_numbers"],
        "tables": doc["tables"],
        "footnotes": n_notes,
        "stats": stats,
        "warnings": warnings,
    }
    return sections, info


def page_ranges(pages: list[int]) -> str:
    """[3, 4, 5, 9] -> '3-5, 9'."""
    out, start = [], None
    for k, p in enumerate(pages):
        if start is None:
            start = p
        if k + 1 == len(pages) or pages[k + 1] != p + 1:
            out.append(f"{start}-{p}" if p != start else f"{p}")
            start = None
    return ", ".join(out)


def summary_line(doc_id: str, sections: list[dict], info: dict, out_path: Path) -> str:
    gap = ", ".join(f"printed = physical {'-' if o >= 0 else '+'} {abs(o)} ({n} pages)"
                    for o, n in sorted(info["offsets"].items())) or "no printed page numbers found"
    if info["offsets"] and info["unnumbered_pages"]:
        gap += f"; no printed number on pp. {page_ranges(info['unnumbered_pages'])}"
    st = info["stats"]
    return (f"{doc_id}: {len(sections)} sections, body pp. {info['body'][0]}-{info['body'][1]} "
            f"of {info['pages']} (skipped pp. {page_ranges(info['skipped'])}, "
            f"TOC pp. {page_ranges(info['toc_pages']) or 'none'})\n"
            f"  pages: {gap}\n"
            f"  removed: {info['headers_removed']} header/footer lines, "
            f"{info['line_numbers_removed']} line numbers; tables: {info['tables']}; "
            f"footnotes: {info['footnotes']}; "
            f"repeated table headers dropped: {st['table_header_dropped']}\n"
            f"  line ends: hyphen kept {st['hyphen_kept']}, hyphen removed {st['hyphen_removed']}, "
            f"suspended hyphen {st['hyphen_suspended']}, slash joined {st['slash_joined']}\n"
            f"  {len(info['warnings'])} warning(s) -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--doc", action="append", help="only this doc_id (repeatable)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with CORPUS.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["source_format"] == "pdf"]
    if args.doc:
        unknown = set(args.doc) - {r["doc_id"] for r in rows}
        if unknown:
            raise SystemExit(f"ERROR: not a pdf doc_id in {CORPUS}: {sorted(unknown)}")
        rows = [r for r in rows if r["doc_id"] in args.doc]
    manifest = load_manifest(MANIFEST)

    for row in rows:
        doc_id = row["doc_id"]
        entry = manifest.get(doc_id)
        if entry is None or entry["status"] not in ("ok", "skipped") or not entry["sha256"]:
            print(f"ERROR {doc_id}: no valid entry in {MANIFEST}, run download_corpus.py first")
            continue

        sections, info = extract_pdf(doc_id, RAW_DIR / f"{doc_id}.pdf", entry["sha256"])
        check_unique_ids(doc_id, sections)
        for w in info["warnings"]:
            print(f"WARNING {doc_id}: {w}")
        if not sections:
            print(f"WARNING {doc_id}: no section extracted")

        out_path = OUT_DIR / f"{doc_id}.jsonl"
        with out_path.open("w", encoding="utf-8") as f:
            for section in sections:
                f.write(json.dumps(section, ensure_ascii=False) + "\n")
        print(summary_line(doc_id, sections, info, out_path))


if __name__ == "__main__":
    main()
