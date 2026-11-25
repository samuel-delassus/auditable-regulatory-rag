"""Show the sections of an extracted document.

Usage:
    python scripts/show_sections.py <doc_id>               # list of sections
    python scripts/show_sections.py <doc_id> <section_id>  # full text of one section
"""
import json
import sys
from pathlib import Path

doc_id = sys.argv[1]
path = Path("data/sections") / f"{doc_id}.jsonl"
sections = [json.loads(line) for line in path.open(encoding="utf-8")]

if len(sys.argv) == 2:
    for s in sections:
        pages = f"pp. {s['page_start']}-{s['page_end']}" if s["page_start"] else ""
        print(f"{s['section_id']:<12} {pages:<12} {s['char_count']:>6} chars  {s['title']}")
else:
    for s in sections:
        if s["section_id"] == sys.argv[2]:
            print(f"{s['title']}\n{s['subpart']}  {s['location']}\n")
            print(s["text"])