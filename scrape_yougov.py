#!/usr/bin/env python3
"""
YouGov Economist Tables Scraper
Downloads weekly Economist/YouGov poll result tables (2020-01-01 to 2026-01-31)

Usage:
    python scrape_yougov.py [--debug] [--out DIR]

    --debug   Run with a visible browser window for troubleshooting
    --out DIR Save files to DIR instead of ./downloads

Requirements:
    pip install -r requirements.txt
    playwright install chromium
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Configuration ─────────────────────────────────────────────────────────────

SEARCH_URL = (
    "https://yougov.com/en-us/search"
    "?q_term=Economist%20Tables"
    "&q_type=surveys"
    "&q_from=2020-01-01"
    "&q_to=2026-01-31"
)
BASE_URL = "https://yougov.com"
REQUEST_DELAY = 1.5   # seconds between page navigations (be polite)

# Realistic browser headers for requests-based downloads
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sanitize_filename(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(". ") or "download"


def filename_from_response(response: requests.Response, url: str) -> str:
    cd = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\'\s;]+)', cd, re.IGNORECASE)
    if match:
        return sanitize_filename(match.group(1))
    path = urlparse(url).path
    name = os.path.basename(path)
    return sanitize_filename(name) if name else f"file_{int(time.time())}"


# ── Phase 1: Discover all survey page URLs ─────────────────────────────────────

async def collect_survey_urls(playwright_browser, debug: bool) -> list[str]:
    """
    Navigate the search results pages and return every survey-detail URL found.

    Strategy (in order):
      1. Intercept JSON API responses — YouGov's SPA fetches results via XHR.
         If we can parse those, we get clean data without HTML scraping.
      2. Fall back to querying anchor tags in the rendered DOM.
    """
    browser = playwright_browser
    context = await browser.new_context(
        user_agent=HEADERS["User-Agent"],
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        # Accept all cookies so consent gates don't block us
        accept_downloads=True,
    )
    page = await context.new_page()

    survey_urls: list[str] = []
    captured_api: list[dict] = []

    # ── Intercept XHR/fetch responses that look like search API calls ──────────
    async def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        # Heuristic: YouGov API calls for search results
        if not any(kw in url for kw in ("search", "survey", "study", "api", "query")):
            return
        try:
            body = await response.json()
            captured_api.append({"url": url, "body": body})
        except Exception:
            pass

    page.on("response", on_response)

    log(f"Loading search page (this may take ~10 s)…")
    try:
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=45_000)
    except PlaywrightTimeout:
        log("networkidle timeout — continuing with what loaded so far")
        await page.wait_for_timeout(3_000)

    # Give lazy-loaded content a moment
    await page.wait_for_timeout(2_000)

    if debug:
        await page.screenshot(path="debug_page1.png", full_page=True)
        with open("debug_page1.html", "w", encoding="utf-8") as fh:
            fh.write(await page.content())
        log("Debug: saved debug_page1.png and debug_page1.html")

    # ── Try to extract URLs from captured API responses first ─────────────────
    api_survey_urls = _parse_survey_urls_from_api(captured_api)
    if api_survey_urls:
        log(f"API interception found {len(api_survey_urls)} URLs on page 1")
        survey_urls.extend(api_survey_urls)
    else:
        # Fall back to DOM scraping
        dom_urls = await _extract_urls_from_dom(page)
        log(f"DOM scraping found {len(dom_urls)} URLs on page 1")
        survey_urls.extend(dom_urls)

    # ── Paginate ───────────────────────────────────────────────────────────────
    page_num = 1
    while True:
        next_btn = await _find_next_button(page)
        if next_btn is None:
            log("No further pages detected")
            break

        page_num += 1
        log(f"Loading page {page_num}…")
        captured_api.clear()

        try:
            await next_btn.click()
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeout:
            await page.wait_for_timeout(2_000)

        await page.wait_for_timeout(1_500)

        new_api_urls = _parse_survey_urls_from_api(captured_api)
        if new_api_urls:
            log(f"  API interception: {len(new_api_urls)} URLs")
            survey_urls.extend(new_api_urls)
        else:
            new_dom_urls = await _extract_urls_from_dom(page)
            log(f"  DOM scraping: {len(new_dom_urls)} URLs")
            if not new_dom_urls:
                log("  No new results — stopping pagination")
                break
            survey_urls.extend(new_dom_urls)

        await asyncio.sleep(REQUEST_DELAY)

    await context.close()

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in survey_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    # Save API call metadata for debugging
    if captured_api:
        with open("debug_api_calls.json", "w") as fh:
            json.dump([{"url": c["url"]} for c in captured_api], fh, indent=2)

    return unique


def _parse_survey_urls_from_api(api_calls: list[dict]) -> list[str]:
    """
    Try to pull survey page URLs out of intercepted JSON API responses.
    YouGov's API shape isn't public, so we walk the JSON looking for
    fields that look like slugs / URLs.
    """
    urls: list[str] = []
    for call in api_calls:
        body = call.get("body", {})
        _walk_json_for_urls(body, urls)
    return urls


def _walk_json_for_urls(obj, results: list[str], depth: int = 0) -> None:
    """Recursively walk a JSON object looking for YouGov survey URL fragments."""
    if depth > 10:
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, str) and _looks_like_survey_url(val):
                full = urljoin(BASE_URL, val) if not val.startswith("http") else val
                if full not in results:
                    results.append(full)
            else:
                _walk_json_for_urls(val, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json_for_urls(item, results, depth + 1)


def _looks_like_survey_url(s: str) -> bool:
    """Heuristic: does this string look like a YouGov survey page path?"""
    patterns = [
        r"yougov\.com/.+/survey",
        r"yougov\.com/.+/studies",
        r"yougov\.com/.+/topics",
        r"^/en-[a-z]+/topics/",
        r"^/en-[a-z]+/survey",
        r"^/en-[a-z]+/studies",
    ]
    return any(re.search(p, s, re.IGNORECASE) for p in patterns)


async def _extract_urls_from_dom(page) -> list[str]:
    """Scrape survey-like anchor hrefs from the rendered DOM."""
    try:
        raw = await page.evaluate(r"""
            () => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                return links.map(a => a.href).filter(Boolean);
            }
        """)
    except Exception:
        return []

    urls = []
    for href in raw:
        if "yougov.com" in href and _looks_like_survey_url(href):
            # Normalise: strip query / fragment noise that isn't the survey ID
            parsed = urlparse(href)
            clean = parsed._replace(fragment="").geturl()
            if clean not in urls:
                urls.append(clean)

    return urls


async def _find_next_button(page):
    """Return a Playwright element handle for the 'next page' control, or None."""
    selectors = [
        "[aria-label='Next page']",
        "[aria-label='next page']",
        "[aria-label='Next']",
        "button:has-text('Next')",
        "a:has-text('Next')",
        "a[rel='next']",
        ".pagination-next:not([disabled])",
        "[data-testid='pagination-next']",
        "li.next > a",
        "nav a:has-text('›')",
        "nav a:has-text('»')",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el is None:
                continue
            disabled = await el.get_attribute("disabled")
            aria_disabled = await el.get_attribute("aria-disabled")
            if disabled is None and aria_disabled != "true":
                return el
        except Exception:
            pass
    return None


# ── Phase 2: Find the download link on each survey page ───────────────────────

async def find_download_url(context, survey_url: str) -> str | None:
    """
    Open a survey detail page and return the URL of the downloadable file,
    or None if none is found.

    YouGov cross-tab files are typically .xlsx; some older ones are .zip.
    """
    page = await context.new_page()
    download_url: str | None = None

    # Watch for direct file responses triggered by button clicks
    triggered_downloads: list[str] = []

    async def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        file_ct = any(k in ct for k in (
            "spreadsheet", "excel", "octet-stream", "zip", "csv", "pdf"
        ))
        file_ext = re.search(r"\.(xlsx?|csv|zip|pdf)(\?|$)", url, re.IGNORECASE)
        if file_ext or file_ct:
            triggered_downloads.append(url)

    page.on("response", on_response)

    try:
        await page.goto(survey_url, wait_until="domcontentloaded", timeout=25_000)
        await page.wait_for_timeout(2_000)

        # ── 1. Look for direct file links in the DOM ───────────────────────────
        href = await page.evaluate(r"""
            () => {
                const exts = /\.(xlsx?|csv|zip|pdf)(\?|$)/i;
                const keywords = /download|file|data|crosstab|topline/i;
                const candidates = Array.from(document.querySelectorAll('a[href]'));
                for (const a of candidates) {
                    const h = a.href || '';
                    if (exts.test(h) || (keywords.test(h) && keywords.test(a.textContent))) {
                        return h;
                    }
                }
                // Broaden: any link whose text looks like a download
                for (const a of candidates) {
                    if (keywords.test(a.textContent)) return a.href;
                }
                return null;
            }
        """)
        if href:
            download_url = href

        # ── 2. Try clicking a Download button and watch for navigations ────────
        if not download_url:
            btn_selectors = [
                "a[download]",
                "a:has-text('Download')",
                "button:has-text('Download')",
                "[data-testid*='download']",
                ".download-button",
                ".download-link",
                "a:has-text('Topline')",
                "a:has-text('Crosstab')",
                "a:has-text('Data')",
                "a:has-text('Tables')",
            ]
            for sel in btn_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el is None:
                        continue
                    async with page.expect_download(timeout=8_000) as dl_info:
                        await el.click()
                    dl = await dl_info.value
                    download_url = dl.url
                    await dl.cancel()   # we just want the URL, not to save via Playwright
                    break
                except Exception:
                    pass

        if not download_url and triggered_downloads:
            download_url = triggered_downloads[0]

    except PlaywrightTimeout:
        log(f"    Timeout on {survey_url}")
    except Exception as exc:
        log(f"    Error on {survey_url}: {exc}")
    finally:
        await page.close()

    return download_url


# ── Phase 3: Download files ────────────────────────────────────────────────────

def download_file(
    url: str,
    save_dir: Path,
    session: requests.Session,
) -> Path | None:
    """Download a file, skip if it already exists. Returns saved path or None."""
    try:
        head = session.head(url, allow_redirects=True, timeout=15)
        # Follow to final URL after redirects
        final_url = head.url
    except Exception:
        final_url = url

    # Derive a candidate filename from the URL before streaming
    tentative_name = sanitize_filename(os.path.basename(urlparse(final_url).path))
    tentative_path = save_dir / (tentative_name or "file")

    # Skip if already downloaded (exact filename)
    if tentative_path.exists() and tentative_path.stat().st_size > 0:
        log(f"    Already downloaded: {tentative_name}")
        return tentative_path

    try:
        response = session.get(final_url, stream=True, timeout=120)
        response.raise_for_status()
    except Exception as exc:
        log(f"    HTTP error: {exc}")
        return None

    filename = filename_from_response(response, final_url)
    save_path = save_dir / filename

    if save_path.exists() and save_path.stat().st_size > 0:
        log(f"    Already downloaded: {filename}")
        return save_path

    try:
        with open(save_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=65_536):
                fh.write(chunk)
        size_kb = save_path.stat().st_size / 1024
        log(f"    Saved: {filename}  ({size_kb:.0f} KB)")
        return save_path
    except Exception as exc:
        log(f"    Write error: {exc}")
        if save_path.exists():
            save_path.unlink()
        return None


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    download_dir = Path(args.out)
    download_dir.mkdir(parents=True, exist_ok=True)
    log(f"Output directory : {download_dir.resolve()}")
    log(f"Search URL       : {SEARCH_URL}")
    log(f"Headless         : {not args.debug}")

    # ── Phase 1 ────────────────────────────────────────────────────────────────
    log("\n── Phase 1: Collecting survey page URLs ──────────────────────────────")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.debug)

        survey_urls = await collect_survey_urls(browser, debug=args.debug)

        if not survey_urls:
            log(
                "No survey URLs found.\n"
                "  • Try running with --debug to see the browser window.\n"
                "  • The page structure may have changed; inspect debug_page1.html."
            )
            await browser.close()
            return

        with open("survey_urls.txt", "w") as fh:
            fh.write("\n".join(survey_urls) + "\n")
        log(f"Found {len(survey_urls)} unique survey URLs → saved to survey_urls.txt")

        # ── Phase 2 + 3 (interleaved per survey) ──────────────────────────────
        log("\n── Phase 2 + 3: Finding download links and saving files ──────────")
        session = requests.Session()
        session.headers.update(HEADERS)

        # Transfer Playwright cookies to requests so auth carries over (if any)
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])

        ok_count = 0
        no_link_count = 0
        fail_count = 0

        for idx, survey_url in enumerate(survey_urls, start=1):
            log(f"\n[{idx}/{len(survey_urls)}] {survey_url}")

            dl_url = await find_download_url(context, survey_url)
            if not dl_url:
                log("    No download link found — skipping")
                no_link_count += 1
                await asyncio.sleep(REQUEST_DELAY)
                continue

            log(f"    Download URL: {dl_url}")
            result = download_file(dl_url, download_dir, session)
            if result:
                ok_count += 1
            else:
                fail_count += 1

            await asyncio.sleep(REQUEST_DELAY)

        await context.close()
        await browser.close()

    log("\n── Summary ────────────────────────────────────────────────────────────")
    log(f"  Survey URLs found  : {len(survey_urls)}")
    log(f"  Files downloaded   : {ok_count}")
    log(f"  No link found      : {no_link_count}")
    log(f"  Download failures  : {fail_count}")
    log(f"  Saved to           : {download_dir.resolve()}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape and download YouGov Economist Tables")
    p.add_argument("--debug", action="store_true", help="Show browser window")
    p.add_argument("--out", default="downloads", metavar="DIR",
                   help="Directory to save downloaded files (default: ./downloads)")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
