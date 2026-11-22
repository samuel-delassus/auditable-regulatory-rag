# Project journal

Short log of decisions, problems and lessons learned.
One entry per work session.

## 2026-11-05 to 2026-11-07 — Corpus definition and download

**Done**
- Defined a corpus of 11 FDA documents (2 regulations, 8 final guidances, 1 draft) in `data/corpus.csv`.
- Wrote `scripts/download_corpus.py` with Claude Code; 11 files downloaded, SHA-256 hashes recorded in `data/manifest.csv`.

**Decisions**
- Source PDFs are not versioned: the corpus is rebuilt from `corpus.csv` and verified with hashes (reproducible, lightweight repo).
- Mixed regulations (binding) and guidances (non-binding) so answers can state which kind of source they rely on.
- Dates: the date printed on the document wins over the web page (e.g. Part 11 guidance: PDF says August 2003, web page says September 2003).
  Partial dates (YYYY-MM) are allowed rather than inventing a day.
- Removed the `issuer` column: costly to fill, error-prone, and no feature uses it. Rule: no metadata without a feature that needs it.
- eCFR regulations fetched through the eCFR API (XML, pinned to 2026-10-23) instead of the HTML pages: structured sections and a frozen version.

**Problems**
- The eCFR HTML pages returned HTTP 200 but redirected to a bot-block page.
  A status-code check would have accepted it; the content check (section marker "11.10") caught it.

**Working with AI assistants**
- Good: proposed a plan before coding, flagged the eCFR risk itself, tested in a scratch folder without touching my data.
- I had to specify: recompute hashes from the files on disk (not from the old manifest), never spoof a browser User-Agent on a 403, add a content check for HTML/XML.

**Next**
- Write `scripts/extract.py` myself, starting with the Part 11 XML.
- Verify manually the questions drafted with Claude in `eval/reference_part11.csv`.


## 2026-11-22 — Sections extraction (eCFR regulations)

**Done**
- Explored the eCFR XML structure: DIV5 = part, DIV6 = subpart, DIV8 = section; text in P, amendment history in CITA.
- Wrote `scripts/extract.py` step by step with guidance from Claude (chat): one JSONL file per regulation in `data/sections/`, one line per section.
- Result: Part 11 = 10 sections; Part 820 = 6 sections (3 reserved sections skipped).

**Decisions**
- Raw files and extracted sections are not versioned: they are rebuilt from corpus.csv (download) and the scripts (extraction).
- No change history inside the download script: manifest.csv is versioned, so `git log -p data/manifest.csv` shows what changed between downloads.
  Rule: commit the manifest after each download that changes it.
- Each section stores the SHA-256 of its source file, so any answer can be traced back to the exact document version. Extraction refuses a document that is missing from the manifest.
- CITA (amendment history) is kept in a separate `citation` field, not in the text, to avoid polluting retrieval.
- Reserved sections ("[Reserved]", no text) are skipped.
- The extraction handles the structures found in the current corpus only; any unknown tag triggers a warning instead of being silently dropped.
- Only one version per document; keeping older versions is for v2.

**Problems**
- Part 820 contains a table (nested in DIV elements). Extracting only P elements would have silently dropped it. Tables are converted to text: caption, then one line per row ("cell | cell").
- § 820.10(e) mentions regulatory consequences of non-compliance: question q032 ("What fines...") is not fully unanswerable; to review.

**Working with AI assistants**
- Claude (chat) helped with the script skeleton, design suggestions and Python syntax; I wrote, debugged and verified the code.

**Next**
- Extract the 9 PDF guidance documents into the same JSONL format.
- Verify the reference questions in `eval/reference_part11.csv`.