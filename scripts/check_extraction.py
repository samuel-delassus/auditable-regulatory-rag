"""Check the extracted sections of every document in data/corpus.csv (XML and PDF).

For each document:
- data/sections/<doc_id>.jsonl exists, every line has the expected keys in order;
- doc_id matches, section_id unique, text not empty, char_count == len(text);
- page_start/page_end: null for XML, integers with start <= end for PDF;
- source_sha256 == manifest sha256 == sha256 of the raw file;
- coverage: words of the source file (re-read here, independently of the
  extractors) that are missing from the JSONL (title, subpart, text, citation).

Coverage counts words of 3+ letters as a multiset. Text left out on purpose is
subtracted and reported, never hidden: for XML the part heading, Authority,
Source and [Reserved] sections; for PDF the pages before the first extracted
page (cover, TOC), lines repeated on >= 50% of the body pages (running
headers/footers, "Page N") and table header rows repeated on a table's
continuation page. Coverage fails above MAX_MISSING.

Usage: python scripts/check_extraction.py   (exit code 1 if any check fails)
"""
import csv
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pdfplumber

from extract_cfr import CORPUS, MANIFEST, OUT_DIR, RAW_DIR, load_manifest

KEYS = ["doc_id", "section_id", "location", "subpart", "title", "text", "citation",
        "char_count", "source_sha256", "page_start", "page_end"]
MAX_MISSING = 0.001   # fraction of source words allowed missing (PDF quirks)
REPEAT_RATIO = 0.5


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]{3,}", text)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def xml_source_words(path: Path) -> tuple[Counter, Counter]:
    """(words to find in the output, words excluded by design)."""
    root = ET.parse(path).getroot()
    excluded = Counter()
    for el in list(root):
        if el.tag in ("HEAD", "AUTH", "SOURCE"):
            excluded.update(words("".join(el.itertext())))
    # Reserved placeholders: sections (DIV8) and subparts (DIV6, e.g. "Subparts C-O [Reserved]").
    reserved = [div for div in root.iter() if div.tag.startswith("DIV")
                and div.find("HEAD") is not None
                and "[Reserved]" in "".join(div.find("HEAD").itertext())]
    inner = {id(d) for r in reserved for d in r.iter() if d is not r}
    for div in reserved:
        if id(div) not in inner:  # count nested reserved divs once
            excluded.update(words("".join(div.itertext())))
    return Counter(words(" ".join(root.itertext()))) - excluded, excluded


def pdf_source_words(path: Path, first_page: int) -> tuple[Counter, Counter]:
    """(words to find in the output, words excluded: repeated running lines)."""
    with pdfplumber.open(path) as pdf:
        pages = [[l["text"] for l in p.extract_text_lines(x_tolerance=1.5)]
                 for p in pdf.pages[first_page - 1:]]
    key = lambda line: re.sub(r"\d+", "#", " ".join(line.lower().split()))
    counts = Counter(k for lines in pages for k in {key(l) for l in lines})
    repeated = {k for k, n in counts.items() if n >= max(2, REPEAT_RATIO * len(pages))}
    source, excluded = Counter(), Counter()
    for lines in pages:
        for line in lines:
            (excluded if key(line) in repeated else source).update(words(line))
    return source, excluded


def repeated_table_header_words(path: Path, first_page: int) -> tuple[Counter, int]:
    """Words of table header rows repeated at the top of a table's continuation page.

    Tables are found with extract_pdf's reader (the only part of this check that relies
    on the extractor); the rule is applied here: the first table of a page whose first
    row equals the first row of the last table on the previous page."""
    from extract_pdf import read_pdf
    pages = read_pdf(path)["pages"]
    excluded, n_rows = Counter(), 0
    prev_first = None  # first row of the last table on the previous page
    for i in range(first_page - 1, len(pages)):
        tables = [it for it in pages[i] if it["kind"] == "table"]
        firsts = [" | ".join(" ".join(cell) for cell in t["rows"][0]) for t in tables]
        if firsts and firsts[0] == prev_first:
            excluded.update(words(firsts[0]))
            n_rows += 1
        prev_first = firsts[-1] if firsts else None
    return excluded, n_rows


def check_doc(row: dict, manifest: dict) -> tuple[list[str], list[str]]:
    """Return (errors, info lines) for one document."""
    doc_id, fmt = row["doc_id"], row["source_format"]
    errors, info = [], []
    out_path = OUT_DIR / f"{doc_id}.jsonl"
    raw_path = RAW_DIR / f"{doc_id}.{fmt}"
    if not out_path.exists():
        return [f"missing {out_path}"], info
    lines = out_path.read_text(encoding="utf-8").splitlines()
    sections = []
    for n, line in enumerate(lines, 1):
        try:
            sections.append(json.loads(line))
        except json.JSONDecodeError as e:
            errors.append(f"line {n}: invalid JSON ({e})")
    if not sections:
        return errors + ["no section"], info

    # Schema and per-section rules.
    for s in sections:
        where = f"section {s.get('section_id')!r}"
        if list(s) != KEYS:
            errors.append(f"{where}: keys {list(s)} != expected order")
            continue
        if s["doc_id"] != doc_id:
            errors.append(f"{where}: doc_id {s['doc_id']!r}")
        if not s["text"].strip():
            errors.append(f"{where}: empty text")
        if s["char_count"] != len(s["text"]):
            errors.append(f"{where}: char_count {s['char_count']} != {len(s['text'])}")
        if fmt == "xml" and (s["page_start"], s["page_end"]) != (None, None):
            errors.append(f"{where}: page_start/page_end should be null for XML")
        if fmt == "pdf" and not (isinstance(s["page_start"], int) and isinstance(s["page_end"], int)
                                 and s["page_start"] <= s["page_end"]):
            errors.append(f"{where}: bad pages {s['page_start']}-{s['page_end']}")
    dups = [sid for sid, n in Counter(s.get("section_id") for s in sections).items() if n > 1]
    if dups:
        errors.append(f"duplicate section_id: {dups}")

    # Source hash: JSONL == manifest == file on disk.
    entry = manifest.get(doc_id)
    file_sha = sha256_file(raw_path) if raw_path.exists() else None
    if entry is None:
        errors.append(f"not in {MANIFEST}")
    else:
        if file_sha != entry["sha256"]:
            errors.append(f"{raw_path} sha256 {str(file_sha)[:12]} != manifest {entry['sha256'][:12]}")
        bad = {s.get("source_sha256") for s in sections} - {entry["sha256"]}
        if bad:
            errors.append(f"source_sha256 != manifest: {sorted(b[:12] for b in bad if b)}")

    # Coverage against the source file.
    if raw_path.exists():
        if fmt == "xml":
            source, excluded = xml_source_words(raw_path)
            excl_what = "part heading, Authority, Source, [Reserved] sections"
        else:
            first = min(s["page_start"] for s in sections if isinstance(s.get("page_start"), int))
            source, excluded = pdf_source_words(raw_path, first)
            headers, n_rows = repeated_table_header_words(raw_path, first)
            headers &= source  # only words still counted in the source
            source -= headers
            excluded += headers
            excl_what = (f"pp. 1-{first - 1} (cover, TOC), repeated header/footer lines, "
                         f"{n_rows} repeated table header rows")
        output = Counter(w for s in sections
                         for w in words(" ".join(str(s.get(k, "")) for k in
                                                 ("title", "subpart", "text", "citation"))))
        missing = source - output
        total, n_missing = sum(source.values()), sum(missing.values())
        ratio = n_missing / total if total else 0.0
        info.append(f"coverage: {total - n_missing}/{total} source words found "
                    f"({n_missing} missing, {ratio:.2%})"
                    + (f": {', '.join(f'{w}×{n}' for w, n in missing.most_common(10))}"
                       if missing else ""))
        info.append(f"excluded by design: {sum(excluded.values())} words ({excl_what})")
        if ratio > MAX_MISSING:
            errors.append(f"coverage: {ratio:.2%} of source words missing (max {MAX_MISSING:.2%})")
    else:
        errors.append(f"missing source file {raw_path}")

    info.insert(0, f"{len(sections)} sections, {sum(s.get('char_count', 0) for s in sections)} chars")
    return errors, info


def main() -> None:
    with CORPUS.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    manifest = load_manifest(MANIFEST)
    failed = 0
    for row in rows:
        errors, info = check_doc(row, manifest)
        status = "FAIL" if errors else "OK"
        failed += bool(errors)
        print(f"{status:4} {row['doc_id']} ({row['source_format']})")
        for line in info:
            print(f"     {line}")
        for e in errors:
            print(f"     ERROR {e}")
    print(f"\n{len(rows) - failed}/{len(rows)} documents OK")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
