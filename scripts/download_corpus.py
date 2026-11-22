#!/usr/bin/env python3
"""Download the documents listed in data/corpus.csv into data/raw/.

Writes data/manifest.csv (one row per document) and prints a summary.

Usage:
    python scripts/download_corpus.py [--force]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = ROOT / "data" / "corpus.csv"
DEFAULT_OUT_DIR = ROOT / "data" / "raw"
DEFAULT_MANIFEST = ROOT / "data" / "manifest.csv"

USER_AGENT = "auditable-regulatory-rag/0.1"
TIMEOUT = (10, 60)  # (connect, read) seconds
RETRIES = 2  # retries after the first attempt
BACKOFF_SECONDS = 2
RETRY_STATUSES = {429, 500, 502, 503, 504}

SUPPORTED_FORMATS = {"pdf", "html", "xml"}
MIN_TEXT_BYTES = 2000  # minimum size for an HTML/XML document
MIN_PDF_BYTES = 1000

# Section that must be present in eCFR documents, keyed by doc_id.
# Any other "cfr-part<N>" doc_id falls back to section "<N>.1".
ECFR_SECTION_MARKERS = {
    "cfr-part11": "11.10",
    "cfr-part820": "820.10",
}

MANIFEST_FIELDS = [
    "doc_id",
    "source_url",
    "downloaded_at",
    "sha256",
    "size_bytes",
    "content_type",
    "status",
    "error",
]


class DownloadError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sniff_content_type(data: bytes) -> str:
    """Guess a content type from file bytes (for files already on disk)."""
    head = data[:1024].lstrip().lower()
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"<?xml") and b"<html" not in head:
        return "application/xml"
    if head.startswith(b"<!doctype html") or b"<html" in head:
        return "text/html"
    return "application/octet-stream"


def section_marker(doc_id: str) -> str | None:
    if doc_id in ECFR_SECTION_MARKERS:
        return ECFR_SECTION_MARKERS[doc_id]
    m = re.fullmatch(r"cfr-part(\d+)", doc_id)
    return f"{m.group(1)}.1" if m else None


def check_content(doc_id: str, fmt: str, content_type: str, data: bytes) -> None:
    """Raise DownloadError if the content does not look like the expected format."""
    ct = content_type.lower()
    if fmt == "pdf":
        if "pdf" not in ct and "octet-stream" not in ct:
            raise DownloadError(f"unexpected content type for pdf: {content_type!r}")
        if not data.startswith(b"%PDF"):
            raise DownloadError("body does not start with %PDF")
        if len(data) < MIN_PDF_BYTES:
            raise DownloadError(f"pdf too small ({len(data)} bytes)")
        return

    if fmt == "html" and "html" not in ct:
        raise DownloadError(f"unexpected content type for html: {content_type!r}")
    if fmt == "xml" and "xml" not in ct:
        raise DownloadError(f"unexpected content type for xml: {content_type!r}")
    if len(data) < MIN_TEXT_BYTES:
        raise DownloadError(f"{fmt} too small ({len(data)} bytes, min {MIN_TEXT_BYTES})")

    marker = section_marker(doc_id)
    if marker:
        text = data.decode("utf-8", errors="replace")
        # XML: <DIV8 N="11.10" ...>; HTML: "§ 11.10" (or &#xA7; / &sect;).
        patterns = [
            rf'N="{re.escape(marker)}"',
            rf"(§|&#xA7;|&#167;|&sect;)\s*(&nbsp;|\s)*{re.escape(marker)}\b",
        ]
        if not any(re.search(p, text, re.IGNORECASE) for p in patterns):
            raise DownloadError(f"section marker {marker!r} not found in content")


def fetch(session: requests.Session, url: str) -> requests.Response:
    """GET with retries on network errors and transient HTTP statuses."""
    last_error = ""
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
        if resp.status_code in RETRY_STATUSES:
            last_error = f"HTTP {resp.status_code}"
            continue
        return resp
    raise DownloadError(f"{last_error} (after {RETRIES + 1} attempts)")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {row["doc_id"]: row for row in csv.DictReader(f)}


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def process_row(
    row: dict,
    out_dir: Path,
    session: requests.Session,
    previous: dict[str, dict],
    force: bool,
    changes: list[str],
) -> dict:
    doc_id = row["doc_id"].strip()
    url = (row.get("source_url") or "").strip()
    fmt = (row.get("source_format") or row.get("format") or "").strip().lower()
    result = {k: "" for k in MANIFEST_FIELDS}
    result.update(doc_id=doc_id, source_url=url)

    if not url or url.upper() == "TODO":
        result.update(status="skipped", error="no source_url")
        return result
    if fmt not in SUPPORTED_FORMATS:
        result.update(status="failed", error=f"unsupported source_format {fmt!r}")
        return result

    path = out_dir / f"{doc_id}.{fmt}"
    prev = previous.get(doc_id, {})

    if path.exists() and not force:
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        ctype = sniff_content_type(data)
        if prev.get("sha256") == sha and prev.get("downloaded_at"):
            downloaded_at = prev["downloaded_at"]
        else:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            downloaded_at = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
            if prev.get("sha256"):
                changes.append(f"{doc_id}: file on disk differs from previous manifest")
        error = "already exists (use --force to re-download)"
        try:
            check_content(doc_id, fmt, ctype, data)
        except DownloadError as e:
            error += f"; WARNING existing file fails check: {e}"
        result.update(
            downloaded_at=downloaded_at,
            sha256=sha,
            size_bytes=str(len(data)),
            content_type=ctype,
            status="skipped",
            error=error,
        )
        return result

    old_sha = sha256_file(path) if path.exists() else prev.get("sha256", "")

    try:
        resp = fetch(session, url)
        if resp.status_code == 403:
            raise DownloadError("HTTP 403 Forbidden: download manually")
        if resp.status_code != 200:
            raise DownloadError(f"HTTP {resp.status_code}")
        data = resp.content
        ctype = resp.headers.get("Content-Type", "")
        check_content(doc_id, fmt, ctype, data)
    except DownloadError as e:
        msg = str(e)
        if path.exists():
            msg += " (existing file kept)"
        result.update(status="failed", error=msg)
        return result

    write_atomic(path, data)
    sha = hashlib.sha256(data).hexdigest()
    if old_sha and old_sha != sha:
        changes.append(f"{doc_id}: sha256 changed {old_sha[:12]} -> {sha[:12]}")
    result.update(
        downloaded_at=utc_now(),
        sha256=sha,
        size_bytes=str(len(data)),
        content_type=ctype,
        status="ok",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    with args.corpus.open(newline="", encoding="utf-8") as f:
        corpus = list(csv.DictReader(f))

    previous = load_manifest(args.manifest)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    rows: list[dict] = []
    changes: list[str] = []
    for row in corpus:
        if not (row.get("doc_id") or "").strip():
            continue
        result = process_row(row, args.out_dir, session, previous, args.force, changes)
        print(f"[{result['status']:>7}] {result['doc_id']}"
              + (f" ({result['error']})" if result["error"] else ""))
        rows.append(result)

    write_manifest(args.manifest, rows)

    counts = {s: sum(r["status"] == s for r in rows) for s in ("ok", "skipped", "failed")}
    print()
    print(f"Summary: {counts['ok']} ok, {counts['skipped']} skipped, {counts['failed']} failed"
          f" (manifest: {args.manifest})")
    for r in rows:
        if r["status"] == "failed":
            print(f"  FAILED  {r['doc_id']}: {r['error']}")
        elif r["status"] == "skipped" and "WARNING" in r["error"]:
            print(f"  WARNING {r['doc_id']}: {r['error']}")
        elif r["status"] == "skipped" and r["error"] == "no source_url":
            print(f"  SKIPPED {r['doc_id']}: no source_url")
    if changes:
        print("Content changes:")
        for c in changes:
            print(f"  CHANGED {c}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
