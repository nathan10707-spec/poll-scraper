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
    python -m playwright install chromium
"""

import argparse
import asyncio
import json
import os
import re
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
REQUEST_DELAY = 1.5   # seconds between page navigations

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


# ── Browser setup ─────────────────────────────────────────────────────────────

async def make_stealth_context(browser):
    """
    Create a browser context that looks as little like a bot as possible.
    Sets realistic viewport, locale, timezone, and removes automation signals.
    """
    context = await browser.new_context(
        user_agent=HEADERS["User-Agent"],
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
        accept_downloads=True,
    )
    # Remove navigator.webdriver flag that headless Chromium sets
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)
    return context


# ── Cookie / consent banner handling ─────────────────────────────────────────

async def dismiss_banners(page) -> None:
    """
    Try to click through any cookie consent or other interstitial banners
    that might block the actual page content.
    """
    banner_selectors = [
        # Generic accept buttons
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept cookies')",
        "button:has-text('Accept Cookies')",
        "button:has-text('I Accept')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        # YouGov-specific patterns
        "[data-testid='accept-button']",
        "[data-testid='cookie-accept']",
        "#onetrust-accept-btn-handler",
        ".onetrust-accept-btn-handler",
        "#accept-all-cookies",
        ".accept-cookies",
        "[aria-label='Accept cookies']",
        "[class*='accept'][class*='cookie']",
        "[id*='accept'][id*='cookie']",
    ]
    for sel in banner_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                log(f"  Dismissed banner: {sel}")
                await page.wait_for_timeout(1_000)
                return
        except Exception:
            pass


# ── Phase 1: Discover all survey page URLs ────────────────────────────────────

async def collect_survey_urls(browser, debug: bool) -> list[str]:
    context = await make_stealth_context(browser)
    page = await context.new_page()

    survey_urls: list[str] = []
    captured_api: list[dict] = []

    # Intercept JSON API responses — YouGov's SPA fetches results via XHR
    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        url = response.url
        if not any(kw in url for kw in ("search", "survey", "study", "api", "query", "content")):
            return
        try:
            body = await response.json()
            captured_api.append({"url": url, "body": body})
            log(f"  [API] {url}")
        except Exception:
            pass

    page.on("response", on_response)

    log("Loading search page…")
    try:
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45_000)
    except PlaywrightTimeout:
        log("domcontentloaded timeout — continuing anyway")

    # Wait for JS to render results
    await page.wait_for_timeout(5_000)

    # Try to dismiss any cookie / consent banners
    await dismiss_banners(page)
    await page.wait_for_timeout(2_000)

    # Scroll down to trigger lazy loading
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    await page.wait_for_timeout(1_500)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1_500)

    # ── Always save debug artifacts ───────────────────────────────────────────
    title = await page.title()
    log(f"Page title: '{title}'")

    html_content = await page.content()
    with open("debug_page1.html", "w", encoding="utf-8") as fh:
        fh.write(html_content)

    try:
        await page.screenshot(path="debug_page1.png", full_page=True)
        log("Saved debug_page1.html and debug_page1.png")
    except Exception:
        log("Saved debug_page1.html (screenshot failed)")

    # Dump all links on the page for inspection
    all_links = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
             .map(a => a.href).filter(Boolean)
    """)
    with open("debug_all_links.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(all_links))
    log(f"Total links on page: {len(all_links)} → saved to debug_all_links.txt")

    # ── Try API interception first ────────────────────────────────────────────
    api_urls = _parse_survey_urls_from_api(captured_api)
    if api_urls:
        log(f"API interception: {len(api_urls)} survey URLs on page 1")
        survey_urls.extend(api_urls)
    else:
        dom_urls = _filter_survey_links(all_links)
        log(f"DOM scraping: {len(dom_urls)} survey URLs on page 1")
        if not dom_urls:
            log("  No matching survey URLs found in DOM links — check debug_all_links.txt")
        survey_urls.extend(dom_urls)

    # Save captured API calls
    if captured_api:
        with open("debug_api_calls.json", "w") as fh:
            json.dump([{"url": c["url"]} for c in captured_api], fh, indent=2)
        log(f"Saved {len(captured_api)} API calls to debug_api_calls.json")

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
            await page.wait_for_timeout(3_000)

        await page.wait_for_timeout(2_000)
        await dismiss_banners(page)

        page_links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]'))
                 .map(a => a.href).filter(Boolean)
        """)

        new_api_urls = _parse_survey_urls_from_api(captured_api)
        if new_api_urls:
            log(f"  API interception: {len(new_api_urls)} URLs")
            survey_urls.extend(new_api_urls)
        else:
            new_dom_urls = _filter_survey_links(page_links)
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
    return unique


def _parse_survey_urls_from_api(api_calls: list[dict]) -> list[str]:
    urls: list[str] = []
    for call in api_calls:
        _walk_json_for_urls(call.get("body", {}), urls)
    return urls


def _walk_json_for_urls(obj, results: list[str], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(obj, dict):
        for val in obj.values():
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
    patterns = [
        r"yougov\.com/.+/survey",
        r"yougov\.com/.+/studies",
        r"yougov\.com/.+/topics",
        r"yougov\.com/.+/economist",
        r"^/en-[a-z]+/topics/",
        r"^/en-[a-z]+/survey",
        r"^/en-[a-z]+/studies",
        r"^/en-[a-z]+/economist",
        r"/survey-results/",
        r"/poll/",
    ]
    return any(re.search(p, s, re.IGNORECASE) for p in patterns)


def _filter_survey_links(all_links: list[str]) -> list[str]:
    """Filter a raw link list down to survey-like YouGov URLs."""
    urls = []
    for href in all_links:
        if "yougov.com" not in href:
            continue
        if not _looks_like_survey_url(href):
            continue
        # Strip fragment, keep query params that are part of the survey ID
        parsed = urlparse(href)
        clean = parsed._replace(fragment="").geturl()
        if clean not in urls:
            urls.append(clean)
    return urls


async def _find_next_button(page):
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
            if await el.get_attribute("disabled") is not None:
                continue
            if await el.get_attribute("aria-disabled") == "true":
                continue
            return el
        except Exception:
            pass
    return None


# ── Phase 2: Find the download link on each survey page ───────────────────────

async def find_download_url(context, survey_url: str) -> str | None:
    page = await context.new_page()
    download_url: str | None = None
    triggered_downloads: list[str] = []

    async def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if any(k in ct for k in ("spreadsheet", "excel", "octet-stream", "zip", "csv", "pdf")):
            triggered_downloads.append(url)
            return
        if re.search(r"\.(xlsx?|csv|zip|pdf)(\?|$)", url, re.IGNORECASE):
            triggered_downloads.append(url)

    page.on("response", on_response)

    try:
        await page.goto(survey_url, wait_until="domcontentloaded", timeout=25_000)
        await page.wait_for_timeout(2_000)
        await dismiss_banners(page)

        # 1. Direct file hrefs in the DOM
        href = await page.evaluate(r"""
            () => {
                const exts = /\.(xlsx?|csv|zip|pdf)(\?|$)/i;
                const keywords = /download|file|data|crosstab|topline|tables/i;
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const a of links) {
                    const h = a.href || '';
                    if (exts.test(h)) return h;
                }
                for (const a of links) {
                    const h = a.href || '';
                    if (keywords.test(h) || keywords.test(a.textContent)) return h;
                }
                return null;
            }
        """)
        if href:
            download_url = href

        # 2. Click Download/Topline buttons and capture the triggered download
        if not download_url:
            for sel in [
                "a[download]",
                "a:has-text('Download')",
                "button:has-text('Download')",
                "[data-testid*='download']",
                ".download-button",
                ".download-link",
                "a:has-text('Topline')",
                "a:has-text('Crosstab')",
                "a:has-text('Tables')",
                "a:has-text('Data')",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el is None:
                        continue
                    async with page.expect_download(timeout=8_000) as dl_info:
                        await el.click()
                    dl = await dl_info.value
                    download_url = dl.url
                    await dl.cancel()
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

def download_file(url: str, save_dir: Path, session: requests.Session) -> Path | None:
    try:
        head = session.head(url, allow_redirects=True, timeout=15)
        final_url = head.url
    except Exception:
        final_url = url

    tentative_name = sanitize_filename(os.path.basename(urlparse(final_url).path))
    tentative_path = save_dir / (tentative_name or "file")
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
        log(f"    Saved: {filename}  ({save_path.stat().st_size / 1024:.0f} KB)")
        return save_path
    except Exception as exc:
        log(f"    Write error: {exc}")
        save_path.unlink(missing_ok=True)
        return None


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    download_dir = Path(args.out)
    download_dir.mkdir(parents=True, exist_ok=True)
    log(f"Output directory : {download_dir.resolve()}")
    log(f"Search URL       : {SEARCH_URL}")
    log(f"Headless         : {not args.debug}")

    log("\n── Phase 1: Collecting survey page URLs ──────────────────────────────")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not args.debug,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        survey_urls = await collect_survey_urls(browser, debug=args.debug)

        if not survey_urls:
            log(
                "\nNo survey URLs found after scraping.\n"
                "Debug files written to current directory:\n"
                "  debug_page1.html   — full page HTML (check for bot-detection page)\n"
                "  debug_page1.png    — screenshot of what the browser saw\n"
                "  debug_all_links.txt — every link found on the page\n"
                "  debug_api_calls.json — intercepted JSON API calls\n"
                "\nNext steps:\n"
                "  1. Open debug_page1.html in a browser to see what loaded.\n"
                "  2. Run with --debug to watch the browser live.\n"
                "  3. Check debug_all_links.txt — if YouGov links appear there,\n"
                "     update _looks_like_survey_url() to match them."
            )
            await browser.close()
            return

        with open("survey_urls.txt", "w") as fh:
            fh.write("\n".join(survey_urls) + "\n")
        log(f"Found {len(survey_urls)} unique survey URLs → saved to survey_urls.txt")

        log("\n── Phase 2 + 3: Finding download links and saving files ──────────")
        session = requests.Session()
        session.headers.update(HEADERS)
        context = await make_stealth_context(browser)

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
