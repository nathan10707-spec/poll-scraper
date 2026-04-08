#!/usr/bin/env python3
"""
Batch file downloader.

Usage:
    python download.py urls.txt [--out DIR]

    urls.txt  — plain text file, one URL per line (blank lines and # comments ignored)
    --out DIR — destination folder (default: ./downloads)
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_RETRIES = 4
RETRY_DELAYS = [2, 4, 8, 16]  # seconds


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_urls(path: Path) -> list[str]:
    urls = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def filename_for(response: requests.Response, url: str) -> str:
    """Derive a filename from Content-Disposition header, falling back to the URL."""
    cd = response.headers.get("Content-Disposition", "")
    # Try filename*=UTF-8''... then filename="..."
    for pattern in (
        r"filename\*=UTF-8''([^\s;]+)",
        r'filename="([^"]+)"',
        r"filename=([^\s;]+)",
    ):
        m = re.search(pattern, cd, re.IGNORECASE)
        if m:
            return _sanitize(unquote(m.group(1)))

    # Fall back to the last path segment of the URL
    name = os.path.basename(urlparse(url).path)
    return _sanitize(unquote(name)) if name else f"file_{int(time.time())}"


def _sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(". ") or "file"


def download(url: str, out_dir: Path, session: requests.Session) -> bool:
    """Download a single URL. Returns True on success."""
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            response = session.get(url, stream=True, timeout=60, allow_redirects=True)
            response.raise_for_status()

            name = filename_for(response, url)
            dest = out_dir / name

            if dest.exists() and dest.stat().st_size > 0:
                log(f"  Skipped (already exists): {name}")
                return True

            with open(dest, "wb") as fh:
                for chunk in response.iter_content(chunk_size=65_536):
                    fh.write(chunk)

            size_kb = dest.stat().st_size / 1024
            log(f"  Saved: {name}  ({size_kb:.0f} KB)")
            return True

        except requests.HTTPError as exc:
            log(f"  HTTP {exc.response.status_code} — {url}")
            return False  # Don't retry 4xx/5xx

        except Exception as exc:
            if attempt < MAX_RETRIES:
                log(f"  Error ({exc}), retrying in {delay}s…")
                time.sleep(delay)
            else:
                log(f"  Failed after {MAX_RETRIES} attempts: {exc}")
                return False

    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Download files from a URL list")
    parser.add_argument("urls_file", type=Path, help="Text file with one URL per line")
    parser.add_argument("out_dir", type=Path, nargs="?", default=None,
                        help="Output directory (positional, optional)")
    parser.add_argument("--out", type=Path, default=None,
                        metavar="DIR", help="Output directory (default: ./downloads)")
    args = parser.parse_args()

    # Accept output dir as either a positional arg or --out flag
    args.out = args.out_dir or args.out or Path("downloads")

    if not args.urls_file.exists():
        sys.exit(f"File not found: {args.urls_file}")

    urls = load_urls(args.urls_file)
    if not urls:
        sys.exit("No URLs found in file.")

    args.out.mkdir(parents=True, exist_ok=True)
    log(f"URLs to download : {len(urls)}")
    log(f"Output directory : {args.out.resolve()}")

    session = requests.Session()
    session.headers.update(HEADERS)

    ok, failed = 0, 0
    for i, url in enumerate(urls, start=1):
        log(f"\n[{i}/{len(urls)}] {url}")
        if download(url, args.out, session):
            ok += 1
        else:
            failed += 1

    log(f"\nDone — {ok} downloaded, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
