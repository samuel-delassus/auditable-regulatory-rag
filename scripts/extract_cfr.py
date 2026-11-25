"""Extract sections from eCFR XML files into JSONL."""
import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path

CORPUS = Path("data/corpus.csv")
RAW_DIR = Path("data/raw")
OUT_DIR = Path("data/sections")
MANIFEST = Path("data/manifest.csv")


def clean(text: str) -> str:
    """Collapse whitespace into single spaces."""
    return " ".join(text.split())


def table_to_text(table: ET.Element) -> str:
    """Convert an XML table to text: caption, then one line per row."""
    lines = []
    caption = table.find("CAPTION")
    if caption is not None:
        lines.append(clean("".join(caption.itertext())))
    for row in table.iter("TR"):
        cells = [clean("".join(cell.itertext())) for cell in row if cell.tag in ("TH", "TD")]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def check_unique_ids(doc_id: str, sections: list[dict]) -> None:
    """Stop with an error if two sections of a document share a section_id."""
    seen: dict[str, str] = {}
    for s in sections:
        if s["section_id"] in seen:
            raise SystemExit(
                f"ERROR {doc_id}: duplicate section_id {s['section_id']!r} "
                f"({seen[s['section_id']]!r} and {s['title']!r}); nothing written for this document")
        seen[s["section_id"]] = s["title"]


def load_manifest(path: Path) -> dict[str, dict]:
    """Read manifest.csv into {doc_id: row}."""
    with path.open(newline="", encoding="utf-8") as f:
        return {row["doc_id"]: row for row in csv.DictReader(f)}


def extract_cfr(doc_id: str, xml_path: Path, source_sha256: str) -> tuple[list[dict], int]:
    """Return (sections, number of reserved sections skipped) for an eCFR XML file."""
    root = ET.parse(xml_path).getroot()
    sections = []
    reserved = 0

    orphans = root.findall("DIV8")
    if orphans:
        print(f"WARNING {doc_id}: {len(orphans)} section(s) outside any subpart, not extracted")

    for subpart in root.findall("DIV6"):
        subpart_title = clean(subpart.find("HEAD").text or "")
        for section in subpart.iter("DIV8"):
            if section.get("TYPE") != "SECTION":
                continue
            section_id = section.get("N")
            title = clean(section.find("HEAD").text or "")
            paragraphs = []
            citation = ""
            for child in section:
                if child.tag == "HEAD":
                    continue  # title already read
                elif child.tag == "P":
                    paragraphs.append(clean("".join(child.itertext())))
                elif child.tag == "CITA":
                    citation = clean("".join(child.itertext()))
                elif child.tag in ("DIV", "TABLE"):
                    for table in child.iter("TABLE"):
                        paragraphs.append(table_to_text(table))
                    for div in child.iter("DIV"):
                        for grandchild in div:
                            if grandchild.tag not in ("DIV", "TABLE"):
                                print(f"WARNING {doc_id} §{section_id}: "
                                      f"<{grandchild.tag}> next to a table, not extracted")
                else:
                    print(f"WARNING {doc_id} §{section_id}: unknown tag {child.tag}")

            text = "\n".join(paragraphs)
            if not text or "[Reserved]" in title:
                reserved += 1
                continue

            sections.append({
                "doc_id": doc_id,
                "section_id": section_id,
                "location": f"§{section_id}",
                "subpart": subpart_title,
                "title": title,
                "text": text,
                "citation": citation,
                "char_count": len(text),
                "source_sha256": source_sha256,
                "page_start": None,  # no pages in eCFR XML; keeps one schema with PDFs
                "page_end": None,
            })

    if not sections:
        print(f"WARNING {doc_id}: no section extracted")
    return sections, reserved


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with CORPUS.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["source_format"] == "xml"]
    manifest = load_manifest(MANIFEST)

    for row in rows:
        doc_id = row["doc_id"]
        entry = manifest.get(doc_id)
        if entry is None or entry["status"] not in ("ok", "skipped") or not entry["sha256"]:
            print(f"ERROR {doc_id}: no valid entry in {MANIFEST}, run download_corpus.py first")
            continue

        sections, reserved = extract_cfr(doc_id, RAW_DIR / f"{doc_id}.xml", entry["sha256"])
        check_unique_ids(doc_id, sections)

        out_path = OUT_DIR / f"{doc_id}.jsonl"
        with out_path.open("w", encoding="utf-8") as f:
            for section in sections:
                f.write(json.dumps(section, ensure_ascii=False) + "\n")

        print(f"{doc_id}: {len(sections)} sections extracted, "
              f"{reserved} reserved skipped -> {out_path}")


if __name__ == "__main__":
    main()