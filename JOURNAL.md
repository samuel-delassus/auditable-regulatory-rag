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

**Working with Claude Code**
- Good: proposed a plan before coding, flagged the eCFR risk itself, tested in a scratch folder without touching my data.
- I had to specify: recompute hashes from the files on disk (not from the old manifest), never spoof a browser User-Agent on a 403, add a content check for HTML/XML.

**Next**
- Write `scripts/extract.py` myself, starting with the Part 11 XML.
- Verify manually the questions drafted with Claude in `eval/reference_part11.csv`.