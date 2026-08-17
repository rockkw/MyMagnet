#!/usr/bin/env python3
"""
magnetlookup.py

All-in-one torrent magnet link finder.
Reads search terms and URL templates, builds search URLs, scrapes each site
for magnet links, and outputs a browser-ready HTML file plus a CSV log.

Replaces both urlhtml_ic.py and magnet_parser.py — no intermediate files needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

COMMON USAGE (reads iCloud search_term.txt + urls.txt automatically):

    python3 magnetlookup.py               # headless Chrome on by default
    python3 magnetlookup.py --no-js      # plain HTTP requests (faster, often blocked)

ONE-OFF SEARCHES:

    python3 magnetlookup.py --term "Polymer Materials"
    python3 magnetlookup.py --term "Polymer Materials" --category Books
    python3 magnetlookup.py --url "https://archive.org/advancedsearch.php?q=Polymer+Materials&mediatype=texts&output=json&fl[]=identifier&fl[]=title"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INPUT FILES (defaults match urlhtml_ic.py):

    search_term.txt   One search term per line, with optional category headers:
                      [Music]
                      Pink Floyd
                      [Books]
                      Polymer Materials
                      ~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/search_term.txt

    urls.txt          One URL template per line (with placeholder search param)
                      ~/Documents/Development/urls.txt

OUTPUT:

    magnet_results_YYYY-MM-DD_HH-MM.html   Browser-ready results grouped by search term
                                            Sticky nav bar, Open Top 3 buttons, checkboxes
    magnet_results_YYYY-MM-DD_HH-MM.csv    Full log of all results including zero-seed

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEPENDENCIES:

    pip3 install requests beautifulsoup4 lxml --break-system-packages
    pip3 install selenium --break-system-packages   # required (JS mode is default)
                                                    # selenium-manager handles chromedriver
"""

import sys
import os
import csv
import re
import time
import argparse
import subprocess
import webbrowser
import sqlite3
import logging
import hashlib
import configparser as _cp
from collections import OrderedDict, defaultdict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote_plus, quote_plus
from datetime import datetime
from pathlib import Path

# ── Optional: requests + BeautifulSoup ─────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ── Optional: Selenium (JS rendering) ──────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

# ══════════════════════════════════════════════════════════════════════════
# Configuration — loaded from config.ini next to this script
# ══════════════════════════════════════════════════════════════════════════

def _expand(value: str) -> str:
    """
    Expand ${VAR:-default} and $VAR patterns using os.environ.
    Brace-aware so nested ${HOME} inside defaults works correctly.
    """
    result = []
    i = 0
    while i < len(value):
        if value[i] == '$' and i + 1 < len(value) and value[i + 1] == '{':
            depth, j = 0, i + 1
            while j < len(value):
                if value[j] == '{':
                    depth += 1
                elif value[j] == '}':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            var, _, default = value[i + 2:j].partition(':-')
            resolved = os.environ.get(var)
            result.append(resolved if resolved is not None else _expand(default))
            i = j + 1
        elif value[i] == '$' and i + 1 < len(value) and (value[i + 1].isalpha() or value[i + 1] == '_'):
            m = re.match(r'[A-Za-z_][A-Za-z0-9_]*', value[i + 1:])
            result.append(os.environ.get(m.group(), '') if m else '$')
            i += 1 + (len(m.group()) if m else 0)
        else:
            result.append(value[i])
            i += 1
    return ''.join(result)

_cfg = _cp.RawConfigParser()
_cfg_path = Path(__file__).with_name('config.ini')
if not _cfg.read(_cfg_path):
    print(f'[WARN] config.ini not found at {_cfg_path} — using built-in defaults')

def _get(section, key, fallback):
    raw = _cfg.get(section, key, fallback=None)
    if raw is None:
        return fallback
    return _expand(raw)

DEFAULT_SEARCH_FILE = _get('paths', 'search_file',
    os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/search_term.txt'))
DEFAULT_SEARCH_FILE_2 = _get('paths', 'search_file_2', '')
DEFAULT_URLS_FILE   = _get('paths', 'urls_file',
    os.path.expanduser('~/Documents/Development/urls.txt'))
DEFAULT_DB_FILE     = _get('paths', 'db_file',
    os.path.expanduser('~/Documents/Development/magnet_library.db'))
DEFAULT_LOG_DIR     = _get('paths', 'log_dir',
    os.path.expanduser('~/Documents/Development/logs'))
DEFAULT_OUTPUT_DIR  = _get('paths', 'output_dir',
    os.path.expanduser('~/Documents/Development/magnet_results'))

JS_RENDER_TIMEOUT   = int(_get('scraper', 'js_render_timeout', '10'))
JS_SETTLE_PAUSE     = int(_get('scraper', 'js_settle_pause',   '2'))
REQUEST_DELAY       = float(_get('scraper', 'request_delay',   '2'))

# ══════════════════════════════════════════════════════════════════════════
# Site scraping profiles
# ══════════════════════════════════════════════════════════════════════════
# Each key is matched against the site hostname (substring match).
# detail_page_magnet: True  → magnet is on a per-torrent detail page, not
#                             the search results page. The scraper will
#                             visit each detail page individually.

SITE_PROFILES = {
    # LinuxTracker — legitimate BitTorrent tracker for Linux distros / open-
    # source ISOs. Legacy phpBB-style markup with no CSS classes on its
    # tables, so rows are located structurally via the torrent-details link
    # rather than by class name (soupsieve :has() support required — bundled
    # with modern BeautifulSoup/lxml). seed/leech column positions are a
    # best-effort guess (unverified against live markup from this sandbox —
    # WebFetch only returns a summarized/markdown view, not raw HTML) and
    # degrade harmlessly to '?' if wrong. Detail-page magnet extraction falls
    # back to a whole-page regex scan (see fetch_detail_magnet), so magnet
    # discovery itself does not depend on these guesses being right.
    'linuxtracker': {
        'row_selector':       'tr:has(a[href*="page=torrent-details"])',
        'title_selector':     'a[href*="page=torrent-details"]',
        'detail_page_magnet': True,
        'detail_base_url':    'https://linuxtracker.org/',
        'magnet_selector':    'a[href^="magnet:"]',
        'seed_selector':      'td:nth-of-type(6)',   # unverified — adjust if wrong
        'leech_selector':     'td:nth-of-type(7)',   # unverified — adjust if wrong
        'size_regex':         r'([\d.]+\s*[KMGT]i?B)',
        'js_wait_selector':   None,
    },
}

# ── Archive.org — legitimate, API-driven (no HTML scraping) ────────────────
# Internet Archive auto-generates a BitTorrent file for most items. There's
# no magnet link published anywhere on the site, so one is built by
# downloading that .torrent file and computing its BTIH (SHA1 of the raw
# bencoded 'info' dict — see torrent_info_hash() below).
ARCHIVE_ORG_MEDIATYPE_ALIASES = {
    'movies': 'movies', 'movie': 'movies', 'video': 'movies',
    'music': 'audio', 'audio': 'audio',
    'books': 'texts', 'book': 'texts', 'texts': 'texts',
    'software': 'software',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/123.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# ══════════════════════════════════════════════════════════════════════════
# Shared helpers — used across scrape, deduplicate, output, and DB layers
# ══════════════════════════════════════════════════════════════════════════

def seed_int(r: dict) -> int:
    try:
        return int(str(r.get('seeds', '0')).strip())
    except ValueError:
        return 0

def seeded(r: dict) -> bool:
    s = str(r.get('seeds', '0')).strip()
    try:
        return int(s) > 0
    except ValueError:
        return s not in ('0', '?', '')

def deduplicate(results: list[dict]) -> tuple[list[dict], int]:
    """
    Deduplicate a result list by info_hash, keeping the highest-seeded copy.
    Annotates survivors with found_on (list of sites) and dup_count.
    Returns (deduped_list, number_of_duplicates_removed).
    """
    shown = [r for r in results if seeded(r)]
    hash_copies: dict[str, list[dict]] = defaultdict(list)
    for r in shown:
        ih = r.get('info_hash', '') or str(id(r))
        hash_copies[ih].append(r)

    deduped: list[dict] = []
    for ih, copies in hash_copies.items():
        best = dict(max(copies, key=seed_int))
        best['found_on'] = sorted({urlparse(c['source_url']).netloc for c in copies})
        best['dup_count'] = len(copies)
        deduped.append(best)

    return deduped, len(shown) - len(deduped)

# ══════════════════════════════════════════════════════════════════════════
# Dependency check
# ══════════════════════════════════════════════════════════════════════════

def check_dependencies(js_mode: bool = False):
    missing = []
    if not REQUESTS_OK:
        missing.append('pip3 install requests beautifulsoup4 lxml --break-system-packages')
    if js_mode and not SELENIUM_OK:
        missing.append('pip3 install selenium --break-system-packages')
    if missing:
        print('━' * 62)
        print('  Missing dependencies — install with:')
        for cmd in missing:
            print(f'    {cmd}')
        print('━' * 62)
        sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════
# URL builder
# ══════════════════════════════════════════════════════════════════════════

def inject_search_term(template_url: str, term: str) -> str:
    """Replace the search parameter in a URL template with the given term."""
    parsed = urlparse(template_url)
    # keep_blank_values=True: a template written as "...?q=" (empty placeholder,
    # e.g. the archive.org advancedsearch.php template) must still be recognised
    # as having a 'q' key — parse_qs() drops blank-valued keys by default, which
    # would otherwise fall through to the 'else' branch and add a spurious
    # second 'search=' param instead of filling in 'q'.
    params = parse_qs(parsed.query, keep_blank_values=True)

    if 'q' in params:
        params['q'] = [term]
    elif 'search' in params:
        params['search'] = [term]
    else:
        params['search'] = [term]

    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

def build_search_urls(term: str, urls_file: str) -> list[str]:
    """Read URL templates from urls_file and inject the search term into each."""
    try:
        with open(urls_file, 'r') as f:
            templates = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        print(f'[ERROR] URLs file not found: {urls_file}')
        sys.exit(1)

    if not templates:
        print(f'[ERROR] No URLs found in {urls_file}')
        sys.exit(1)

    return [inject_search_term(tmpl, term) for tmpl in templates]

# ══════════════════════════════════════════════════════════════════════════
# Input readers
# ══════════════════════════════════════════════════════════════════════════

def normalise_category(raw: str) -> str:
    return raw.strip().title()

def _parse_search_term_lines(lines: list[str]) -> list[tuple[str, str]]:
    """Parse search_term.txt-format lines into (term, category) tuples."""
    results: list[tuple[str, str]] = []
    current_category = 'Default'

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current_category = normalise_category(line[1:-1])
        else:
            results.append((line, current_category))

    return results

def read_search_terms(path: str, path_2: str = '') -> list[tuple[str, str]]:
    """
    Read search_term.txt (and optionally a 2nd search-term file, e.g. from
    Google Drive) and return a combined, de-duplicated list of
    (term, category) tuples.

    File format:
        [Music]             ← sets current category (title-cased on read)
        Pink Floyd          ← ('Pink Floyd', 'Music')
        Aphex Twin
        # commented out     ← ignored
        [Books]
        Polymer Materials   ← ('Polymer Materials', 'Books')

    Lines before any header get category 'Default'.
    A file with no headers works unchanged — all terms get category 'Default'.

    path_2 is optional; if blank or the file doesn't exist it's skipped
    silently (only the primary path is required to exist).
    """
    try:
        with open(path, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f'[ERROR] Search term file not found: {path}')
        print( '        Add terms (one per line) to that file, or use --term')
        sys.exit(1)

    results = _parse_search_term_lines(lines)
    sources = [path]

    if path_2:
        try:
            with open(path_2, 'r') as f:
                lines_2 = f.readlines()
            results.extend(_parse_search_term_lines(lines_2))
            sources.append(path_2)
        except FileNotFoundError:
            print(f'[WARN] 2nd search term file not found, skipping: {path_2}')

    # De-dupe (term, category) pairs while preserving first-seen order —
    # the same term can legitimately appear under different categories.
    seen = set()
    deduped: list[tuple[str, str]] = []
    for term, cat in results:
        key = (term, cat)
        if key not in seen:
            seen.add(key)
            deduped.append((term, cat))
    results = deduped

    if not results:
        print(f'[ERROR] No search terms found in {" or ".join(sources)}')
        sys.exit(1)

    by_cat: dict[str, list[str]] = defaultdict(list)
    for term, cat in results:
        by_cat[cat].append(term)

    print(f'[INFO] {len(results)} search term(s) loaded from {" + ".join(sources)}')
    for cat, terms in by_cat.items():
        print(f'         [{cat}]')
        for t in terms:
            print(f'           • {t}')

    return results

# ══════════════════════════════════════════════════════════════════════════
# Selenium headless driver
# ══════════════════════════════════════════════════════════════════════════
# Uses selenium-manager (built into Selenium 4.6+) to automatically locate
# or download a matching chromedriver. No manual driver install needed.

def build_headless_driver() -> 'webdriver.Chrome':
    opts = ChromeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument(f'user-agent={HEADERS["User-Agent"]}')
    opts.add_experimental_option('excludeSwitches', ['enable-logging'])
    # No Service() argument — selenium-manager handles driver location/download
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver

def fetch_js(url: str, wait_selector: str | None = None,
             driver: 'webdriver.Chrome | None' = None) -> str | None:
    """
    Fetch a URL with headless Chrome and return fully-rendered HTML.
    Reuses driver if provided; otherwise creates a temporary one and quits it.
    """
    owns_driver = driver is None
    try:
        if owns_driver:
            print(f'  [JS] Launching headless Chrome…')
            driver = build_headless_driver()
        driver.get(url)
        if wait_selector:
            try:
                WebDriverWait(driver, JS_RENDER_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
                )
                if owns_driver:
                    print(f'  [JS] Element ready: {wait_selector}')
            except Exception:
                if owns_driver:
                    print(f'  [JS] Timed out waiting for {wait_selector} — scraping anyway')
        time.sleep(JS_SETTLE_PAUSE)
        return driver.page_source
    except Exception as e:
        print(f'  [WARN] Selenium failed for {url}: {e}')
        return None
    finally:
        if owns_driver and driver:
            driver.quit()

# ══════════════════════════════════════════════════════════════════════════
# Static HTTP fetch
# ══════════════════════════════════════════════════════════════════════════

def fetch_static(url: str, js_mode: bool = False) -> str | None:
    """Fetch a URL with requests and return HTML, or None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        if resp.encoding is None or resp.encoding.lower() == 'iso-8859-1':
            resp.encoding = resp.apparent_encoding
        html = resp.text
        # Detect JS-wall responses and warn the user
        if not js_mode:
            cf_challenge = (
                '<meta http-equiv="refresh"' in html and 'CF$cv$params' in html
            )
            js_required = 'Enable JS in your browser' in html
            if cf_challenge or js_required:
                reason = 'Cloudflare JS challenge' if cf_challenge else 'JS required'
                print(f'  [WARN] {urlparse(url).netloc} returned a {reason} — '
                      f'use --no-js to see plain-HTTP output anyway')
                return None
        return html
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403 and not js_mode:
            print(f'  [WARN] {urlparse(url).netloc} blocked the request (403) — '
                  f'try without --no-js to use headless Chrome')
        else:
            print(f'  [WARN] Could not fetch {url}: {e}')
        return None
    except requests.RequestException as e:
        print(f'  [WARN] Could not fetch {url}: {e}')
        return None

def fetch_page(url: str, js_mode: bool,
               wait_selector: str | None = None) -> str | None:
    """Fetch a page either statically or via headless Chrome."""
    return fetch_js(url, wait_selector) if js_mode else fetch_static(url, js_mode=False)

# ══════════════════════════════════════════════════════════════════════════
# Magnet utilities
# ══════════════════════════════════════════════════════════════════════════

def extract_name(magnet: str) -> str:
    m = re.search(r'[?&]dn=([^&]+)', magnet)
    return unquote_plus(m.group(1)) if m else '(no name)'

def extract_hash(magnet: str) -> str:
    m = re.search(r'xt=urn:btih:([a-fA-F0-9]{40}|[A-Z2-7]{32})', magnet)
    return m.group(1).upper() if m else ''

def get_profile(url: str) -> dict | None:
    hostname = urlparse(url).netloc.lower()
    for key, profile in SITE_PROFILES.items():
        if key in hostname:
            return profile
    return None

# ══════════════════════════════════════════════════════════════════════════
# Scraper
# ══════════════════════════════════════════════════════════════════════════

def fetch_detail_magnet(detail_url: str, selector: str,
                        js_mode: bool,
                        driver: 'webdriver.Chrome | None' = None) -> str:
    """
    Visit a torrent detail page and extract its magnet link.
    Accepts an optional shared Selenium driver to avoid repeated Chrome launches.
    """
    if js_mode:
        html = fetch_js(detail_url, wait_selector=None, driver=driver)
    else:
        html = fetch_static(detail_url, js_mode=False)
    if not html:
        return ''
    soup = BeautifulSoup(html, 'lxml')
    tag  = soup.select_one(selector)
    if tag:
        href = tag.get('href', '')
        if href.startswith('magnet:'):
            return href
    found = re.findall(r'magnet:\?[^\s"\'<>]+', html)
    return found[0] if found else ''

def parse_results(html: str, source_url: str,
                  js_mode: bool = False,
                  driver: 'webdriver.Chrome | None' = None) -> list[dict]:
    """
    Parse a search results page and return a list of result dicts.
    Uses the site profile if one exists; falls back to a raw regex magnet scan.
    driver: shared Selenium driver for detail page fetches (avoids re-launching Chrome).
    """
    profile = get_profile(source_url)
    soup    = BeautifulSoup(html, 'lxml')
    results = []

    if profile:
        rows = soup.select(profile['row_selector'])
        for row in rows:
            title_tag = row.select_one(profile['title_selector'])
            # Fallback: BS4 quirk with nth-of-type — walk tds directly
            if not title_tag or not title_tag.get_text(strip=True):
                tds = row.find_all('td', recursive=False)
                if len(tds) > 1:
                    title_tag = tds[1].find('a', href=True)
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)

            s_tag = row.select_one(profile['seed_selector'])
            l_tag = row.select_one(profile['leech_selector'])
            seeds   = s_tag.get_text(strip=True) if s_tag else '?'
            leeches = l_tag.get_text(strip=True) if l_tag else '?'

            size = '?'
            if profile.get('size_selector'):
                sz = row.select_one(profile['size_selector'])
                size = sz.get_text(strip=True) if sz else '?'
            elif profile.get('size_regex'):
                m = re.search(profile['size_regex'], row.get_text(' '))
                size = m.group(1) if m else '?'

            if profile.get('detail_page_magnet'):
                href = title_tag.get('href', '')
                if not href:
                    continue
                base       = profile.get('detail_base_url', '')
                detail_url = base + href if href.startswith('/') else href
                print(f'    [detail] {title[:55]}')
                magnet = fetch_detail_magnet(
                    detail_url, profile['magnet_selector'], js_mode, driver=driver
                )
                time.sleep(1)
            else:
                tag = row.select_one(profile['magnet_selector'])
                if not tag:
                    continue
                magnet = tag.get('href', '')

            if not magnet.startswith('magnet:'):
                continue

            results.append({
                'title':      title,
                'magnet':     magnet,
                'info_hash':  extract_hash(magnet),
                'seeds':      seeds,
                'leeches':    leeches,
                'size':       size,
                'source_url': source_url,
            })

    else:
        print(f'  [INFO] No profile for {urlparse(source_url).netloc}'
              f' — generic magnet scan')
        seen = set()
        for magnet in re.findall(r'magnet:\?[^\s"\'<>]+', html):
            ih = extract_hash(magnet)
            if ih in seen:
                continue
            seen.add(ih)
            results.append({
                'title':      extract_name(magnet),
                'magnet':     magnet,
                'info_hash':  ih,
                'seeds':      '?',
                'leeches':    '?',
                'size':       '?',
                'source_url': source_url,
            })

    return results

# ══════════════════════════════════════════════════════════════════════════
# Archive.org — API + bencode, no HTML scraping
# ══════════════════════════════════════════════════════════════════════════

def _bencode_value_end(data: bytes, i: int) -> int:
    """Return the index one past the bencoded value that starts at i."""
    c = data[i:i + 1]
    if c == b'i':
        return data.index(b'e', i) + 1
    if c == b'l' or c == b'd':
        j = i + 1
        while data[j:j + 1] != b'e':
            j = _bencode_value_end(data, j)          # key (or list item)
            if c == b'd':
                j = _bencode_value_end(data, j)       # value, for dicts
        return j + 1
    if c.isdigit():
        colon = data.index(b':', i)
        length = int(data[i:colon])
        return colon + 1 + length
    raise ValueError(f'invalid bencode at offset {i}')

def torrent_info_hash(torrent_bytes: bytes) -> str:
    """
    Parse a .torrent file's top-level dict just far enough to find the raw
    byte span of its 'info' value, and SHA1-hash those exact bytes — that
    hash is the BTIH used in magnet links (BEP 3). Re-encoding the decoded
    dict instead of hashing the original bytes can produce a different
    (wrong) hash if the source encoder ever deviates from canonical bencode,
    so this hashes the untouched slice rather than round-tripping it.
    """
    if torrent_bytes[0:1] != b'd':
        raise ValueError('not a bencoded dict')
    i = 1
    while torrent_bytes[i:i + 1] != b'e':
        colon = torrent_bytes.index(b':', i)
        klen  = int(torrent_bytes[i:colon])
        kstart = colon + 1
        key    = torrent_bytes[kstart:kstart + klen]
        vstart = kstart + klen
        vend   = _bencode_value_end(torrent_bytes, vstart)
        if key == b'info':
            return hashlib.sha1(torrent_bytes[vstart:vend]).hexdigest().upper()
        i = vend
    raise ValueError("no top-level 'info' key in torrent file")

ARCHIVE_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://tracker.openbittorrent.com:6969/announce',
]

def scrape_archive_org(url: str, max_results: int = 15) -> list[dict]:
    """
    Handle an archive.org advancedsearch.php URL: run the search via the
    JSON API, then for each hit fetch Internet Archive's auto-generated
    <identifier>_archive.torrent and derive a magnet link from its BTIH.
    No HTML scraping / CSS selectors involved, so this isn't sensitive to
    site layout changes the way the SITE_PROFILES scrapers are.
    """
    parsed = urlparse(url)
    q_params = parse_qs(parsed.query)
    term      = (q_params.get('q') or [''])[0]
    mediatype = (q_params.get('mediatype') or [''])[0].strip().lower()
    mediatype = ARCHIVE_ORG_MEDIATYPE_ALIASES.get(mediatype, mediatype)
    if not term:
        print('  [WARN] archive.org URL has no q= search term — skipping')
        return []

    query = term if not mediatype else f'{term} AND mediatype:{mediatype}'
    try:
        resp = requests.get(
            'https://archive.org/advancedsearch.php',
            params={
                'q': query,
                'fl[]': ['identifier', 'title', 'mediatype'],
                'rows': str(max_results),
                'page': '1',
                'output': 'json',
            },
            headers=HEADERS, timeout=20,
        )
        resp.raise_for_status()
        docs = resp.json().get('response', {}).get('docs', [])
    except Exception as e:
        print(f'  [WARN] archive.org search failed: {e}')
        return []

    results = []
    for doc in docs:
        identifier = doc.get('identifier')
        if not identifier:
            continue
        title = doc.get('title') or identifier
        torrent_name = f'{identifier}_archive.torrent'

        try:
            meta = requests.get(f'https://archive.org/metadata/{identifier}',
                                 headers=HEADERS, timeout=20).json()
        except Exception as e:
            print(f'  [WARN] archive.org metadata failed for {identifier}: {e}')
            continue

        files = meta.get('files', []) if isinstance(meta, dict) else []
        file_entry = next((f for f in files if f.get('name') == torrent_name), None)
        if not file_entry:
            continue  # item has no auto-generated torrent (e.g. tiny items)

        try:
            tbytes = requests.get(f'https://archive.org/download/{identifier}/{torrent_name}',
                                   headers=HEADERS, timeout=30).content
            info_hash = torrent_info_hash(tbytes)
        except Exception as e:
            print(f'  [WARN] archive.org torrent parse failed for {identifier}: {e}')
            continue

        magnet = f'magnet:?xt=urn:btih:{info_hash}&dn={quote_plus(title)}'
        for tr in ARCHIVE_TRACKERS:
            magnet += f'&tr={quote_plus(tr)}'

        size = file_entry.get('size', '?')
        if isinstance(size, str) and size.isdigit():
            n = int(size)
            for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
                if n < 1024 or unit == 'TiB':
                    size = f'{n:.2f} {unit}' if unit != 'B' else f'{n} B'
                    break
                n /= 1024

        results.append({
            'title':      title,
            'magnet':     magnet,
            'info_hash':  info_hash,
            'seeds':      '?',      # archive.org doesn't expose swarm stats
            'leeches':    '?',
            'size':       size,
            'source_url': f'https://archive.org/details/{identifier}',
        })
        time.sleep(REQUEST_DELAY)

    print(f'  Found {len(results)} magnet(s)')
    return results

def scrape(url: str, js_mode: bool = False) -> list[dict]:
    """
    Fetch one search URL and return its results.
    In JS mode, creates ONE shared Chrome driver for the search page AND all
    subsequent detail page fetches — avoids launching Chrome once per row.
    """
    if 'archive.org' in urlparse(url).netloc.lower():
        print(f'  Fetching (API): {url}')
        return scrape_archive_org(url)

    print(f'  Fetching ({"JS" if js_mode else "static"}): {url}')
    profile  = get_profile(url)
    wait_sel = profile.get('js_wait_selector') if profile else None

    shared_driver = None
    try:
        if js_mode:
            print(f'  [JS] Launching headless Chrome…')
            shared_driver = build_headless_driver()
        html = fetch_js(url, wait_sel, driver=shared_driver) if js_mode \
               else fetch_static(url)
        if not html:
            return []
        results = parse_results(html, url, js_mode, driver=shared_driver)
        print(f'  Found {len(results)} magnet(s)')
        return results
    finally:
        if shared_driver:
            shared_driver.quit()

# ══════════════════════════════════════════════════════════════════════════
# Logging setup
# ══════════════════════════════════════════════════════════════════════════

def setup_logging(log_dir: str | None = None) -> logging.Logger:
    log = logging.getLogger('magnetlookup')
    log.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                             datefmt='%Y-%m-%d %H:%M:%S')

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        date_str  = datetime.now().strftime('%Y-%m-%d')
        log_path  = Path(log_dir) / f'magnetlookup_{date_str}.log'
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fh.setFormatter(fmt)
        log.addHandler(fh)
        log.info(f'Logging to {log_path}')

    return log

log = logging.getLogger('magnetlookup')

# ══════════════════════════════════════════════════════════════════════════
# SQLite database
# ══════════════════════════════════════════════════════════════════════════

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT    NOT NULL,
    terms       TEXT    NOT NULL,
    js_mode     INTEGER NOT NULL DEFAULT 0,
    total_found INTEGER NOT NULL DEFAULT 0,
    dupes_removed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS torrents (
    info_hash   TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    size        TEXT,
    best_seeds  INTEGER NOT NULL DEFAULT 0,
    best_leeches INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    magnet      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS torrent_sites (
    info_hash   TEXT NOT NULL REFERENCES torrents(info_hash),
    site        TEXT NOT NULL,
    PRIMARY KEY (info_hash, site)
);

CREATE TABLE IF NOT EXISTS search_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id   INTEGER NOT NULL REFERENCES searches(id),
    info_hash   TEXT    NOT NULL REFERENCES torrents(info_hash),
    search_term TEXT    NOT NULL,
    seeds       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS search_terms (
    term        TEXT PRIMARY KEY,
    category    TEXT NOT NULL DEFAULT 'Default',
    first_used  TEXT NOT NULL,
    last_used   TEXT NOT NULL,
    use_count   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sr_search   ON search_results(search_id);
CREATE INDEX IF NOT EXISTS idx_sr_term     ON search_results(search_term);
CREATE INDEX IF NOT EXISTS idx_sr_hash     ON search_results(info_hash);
CREATE INDEX IF NOT EXISTS idx_t_seeds     ON torrents(best_seeds DESC);
CREATE INDEX IF NOT EXISTS idx_t_seen      ON torrents(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_terms_used  ON search_terms(last_used DESC);
CREATE INDEX IF NOT EXISTS idx_terms_cat   ON search_terms(category);
"""

def open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    conn.commit()
    log.info(f'Database: {db_path}')
    return conn

def write_db_term(conn: sqlite3.Connection,
                  term_results: list[dict],
                  label: str,
                  category: str,
                  js_mode: bool):
    """
    Persist results for a single search term to SQLite and commit immediately.
    Called once per term so progress is saved even if a later term fails.
    """
    now = datetime.now().isoformat(timespec='seconds')

    deduped, deduped_count = deduplicate(term_results)

    cur = conn.execute(
        "INSERT INTO searches (run_at, terms, js_mode, total_found, dupes_removed) "
        "VALUES (?, ?, ?, ?, ?)",
        (now, label, int(js_mode), len(deduped), deduped_count)
    )
    search_id = cur.lastrowid

    conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))

    for r in deduped:
        ih = r.get('info_hash', '')
        if not ih:
            continue

        seeds = 0
        try:
            seeds = int(str(r.get('seeds', '0')).strip())
        except ValueError:
            pass

        leeches = 0
        try:
            leeches = int(str(r.get('leeches', '0')).strip())
        except ValueError:
            pass

        conn.execute("""
            INSERT INTO torrents (info_hash, title, size, best_seeds,
                                  best_leeches, first_seen, last_seen, magnet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(info_hash) DO UPDATE SET
                best_seeds   = MAX(best_seeds, excluded.best_seeds),
                best_leeches = MAX(best_leeches, excluded.best_leeches),
                last_seen    = excluded.last_seen,
                magnet       = CASE WHEN excluded.best_seeds >= best_seeds
                                    THEN excluded.magnet ELSE magnet END
        """, (ih, r['title'], r.get('size', '?'), seeds, leeches,
              now, now, r['magnet']))

        for site in r.get('found_on', [urlparse(r['source_url']).netloc]):
            conn.execute(
                "INSERT OR IGNORE INTO torrent_sites (info_hash, site) VALUES (?, ?)",
                (ih, site)
            )

        conn.execute(
            "INSERT INTO search_results (search_id, info_hash, search_term, seeds) "
            "VALUES (?, ?, ?, ?)",
            (search_id, ih, label, seeds)
        )

    conn.execute(
        "INSERT INTO search_terms "
        "    (term, category, first_used, last_used, use_count) "
        "VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(term) DO UPDATE SET "
        "    category  = excluded.category, "
        "    last_used = excluded.last_used, "
        "    use_count = use_count + 1",
        (label, category, now, now)
    )

    conn.commit()
    log.info(f'DB [{label}]: {len(deduped)} torrents committed (run id={search_id})')
    return search_id

# ══════════════════════════════════════════════════════════════════════════
# Output — CSV
# ══════════════════════════════════════════════════════════════════════════

def write_csv(all_results: list[dict], filename: str):
    fields = ['search_term', 'category', 'title', 'size', 'seeds', 'leeches',
              'info_hash', 'magnet', 'source_url']
    with open(filename, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_results)
    print(f'[✓] CSV written  → {filename}')

# ══════════════════════════════════════════════════════════════════════════
# Output — terminal summary
# ══════════════════════════════════════════════════════════════════════════

def print_summary(all_results: list[dict]):
    shown  = [r for r in all_results if seeded(r)]
    hidden = len(all_results) - len(shown)

    if not shown:
        print('\n  No seeded magnet links found.\n')
        return

    hash_copies: dict[str, list[dict]] = defaultdict(list)
    for r in shown:
        ih = r.get('info_hash', '') or str(id(r))
        hash_copies[ih].append(r)

    deduped    = [max(copies, key=seed_int) for copies in hash_copies.values()]
    dup_removed = len(shown) - len(deduped)

    print(f"\n{'─'*80}")
    print(f"  {'TERM':<20} {'TITLE':<35} {'SIZE':>8}  {'S':>5}  {'L':>5}")
    print(f"{'─'*80}")
    for r in sorted(deduped, key=lambda x: x.get('search_term', '')):
        term  = r.get('search_term', '')[:19]
        title = r['title'][:34]
        print(f"  {term:<20} {title:<35} {r['size']:>8}"
              f"  {r['seeds']:>5}  {r['leeches']:>5}")
    print(f"{'─'*80}")
    print(f"  {len(deduped)} unique result(s)", end='')
    if hidden:
        print(f'  |  {hidden} zero-seed hidden', end='')
    if dup_removed:
        print(f'  |  {dup_removed} duplicate(s) removed', end='')
    print('\n')

# ══════════════════════════════════════════════════════════════════════════
# Output — HTML
# ══════════════════════════════════════════════════════════════════════════

def write_html(all_results: list[dict], search_label: str,
               filename: str,
               term_urls: dict | None = None):
    """
    Write magnet_results.html — grouped by search term with:
    • Sticky nav bar with jump links + global Open All Top 3s button
    • Per-section search URL links (collapsible, from urls.txt)
    • Per-section Open Top 3 button
    • Per-row checkboxes + Open Checked button
    • Zero-seed results filtered out
    term_urls: mapping of search term → list of search URLs built from urls.txt
    """
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')

    shown    = [r for r in all_results if seeded(r)]
    filtered = len(all_results) - len(shown)

    groups: dict[str, list[dict]] = OrderedDict()
    for r in shown:
        term = r.get('search_term', search_label)
        groups.setdefault(term, []).append(r)

    def slugify(text: str) -> str:
        return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

    nav_items = ''
    for term, rows in groups.items():
        slug = slugify(term)
        nav_items += (
            f'<a href="#{slug}">{term} '
            f'<span class="nav-count">({len(rows)})</span></a>\n'
        )

    all_top3 = []
    for rows in groups.values():
        all_top3.extend(r['magnet'] for r in rows[:3])
    all_top3_attrs = ' '.join(
        f'data-m{i+1}="{m}"' for i, m in enumerate(all_top3)
    )

    nav_html = ''
    if len(groups) >= 1:
        nav_html = f"""
  <nav id="topnav">
    <span class="nav-label">Jump to:</span>
    {nav_items}
    <button class="open-all-top3" {all_top3_attrs}
            data-total="{len(all_top3)}"
            onclick="openAllTop3(this)"
            title="Open top 3 magnets from every section">
      🧲 Open All Top 3s ({len(all_top3)})
    </button>
  </nav>"""

    deduped, deduped_count = deduplicate(all_results)

    groups = OrderedDict()
    for r in deduped:
        term = r.get('search_term', search_label)
        groups.setdefault(term, []).append(r)

    sections_html = ''
    for term, rows in groups.items():
        slug = slugify(term)

        hidden_in_term = sum(
            1 for r in all_results
            if r.get('search_term', search_label) == term and not seeded(r)
        )
        filter_note = (
            f' <span class="filter-note">({hidden_in_term} zero-seed hidden)</span>'
            if hidden_in_term else ''
        )

        top3       = rows[:3]
        top3_attrs = ' '.join(
            f'data-m{i+1}="{top3[i]["magnet"]}"' for i in range(len(top3))
        )
        top3_label = f'Open Top {len(top3)}' if len(top3) < 3 else 'Open Top 3'

        rows_html = ''
        for r in rows:
            safe_magnet = r['magnet'].replace('"', '&quot;')
            found_on    = r.get('found_on', [urlparse(r['source_url']).netloc])
            sites_str   = ', '.join(found_on)
            multi_badge = (
                f' <span class="multi-badge" title="Found on {len(found_on)} sites: {sites_str}">'
                f'×{len(found_on)}</span>'
                if len(found_on) > 1 else ''
            )
            rows_html += f"""
          <tr>
            <td class="cb-cell">
              <input type="checkbox" class="row-cb"
                     data-magnet="{safe_magnet}"
                     onchange="updateSectionBtn(this)">
            </td>
            <td><button class="mag-btn" title="Open in Transmission"
                onclick="fireMagnet('{safe_magnet}')">🧲</button></td>
            <td><a href="{r['magnet']}">{r['title']}{multi_badge}</a></td>
            <td>{r['size']}</td>
            <td class="seeds">{r['seeds']}</td>
            <td class="leeches">{r['leeches']}</td>
            <td><small>{sites_str}</small></td>
            <td><code class="hash">{r['info_hash'][:12]}…</code></td>
          </tr>"""

        url_links_html = ''
        if term_urls and term in term_urls:
            link_items = ''
            for su in term_urls[term]:
                hostname = urlparse(su).netloc
                link_items += f'<li><a href="{su}" target="_blank">{hostname}</a></li>\n'
            url_links_html = f"""
    <details class="search-links">
      <summary>🔍 Search URLs ({len(term_urls[term])})</summary>
      <ul class="search-url-list">{link_items}</ul>
    </details>"""

        sections_html += f"""
  <section id="{slug}">
    <div class="section-header">
      <h2>{term} <span class="section-count">{len(rows)} result(s){filter_note}</span></h2>
      <div class="section-btns">
        <button class="open-top3" {top3_attrs} onclick="openTop3(this)"
                title="Open top {len(top3)} magnet(s) in Transmission">
          🧲 {top3_label}
        </button>
        <button class="open-checked" onclick="openChecked(this)" disabled
                title="Open all checked magnets in this section">
          ☑ Open Checked (0)
        </button>
      </div>
    </div>{url_links_html}
    <table>
      <thead>
        <tr>
          <th class="cb-cell">
            <input type="checkbox" class="select-all"
                   title="Select / deselect all in this section"
                   onchange="toggleAll(this)">
          </th>
          <th>🧲</th><th>Title</th><th>Size</th>
          <th>Seeds</th><th>Leech</th><th>Site</th><th>Hash</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>
    <a class="back-top" href="#topnav">↑ Back to top</a>
  </section>"""

    filter_summary = f' ({filtered} zero-seed hidden)' if filtered else ''

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Magnet Results — {search_label}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body        {{ font-family: system-ui, sans-serif; margin: 0; padding: 0;
                   background: #111; color: #eee; }}

    /* ── Sticky nav ── */
    #topnav     {{ position: sticky; top: 0; z-index: 100;
                   background: #1a1a1a; border-bottom: 2px solid #f90;
                   padding: 0.55rem 1.5rem; display: flex; flex-wrap: wrap;
                   gap: 0.5rem; align-items: center; }}
    .nav-label  {{ color: #888; font-size: 0.8rem; margin-right: 0.4rem;
                   white-space: nowrap; }}
    #topnav a   {{ color: #f90; text-decoration: none; font-size: 0.85rem;
                   background: #222; border: 1px solid #444;
                   padding: 0.25rem 0.65rem; border-radius: 4px;
                   white-space: nowrap; transition: background 0.15s; }}
    #topnav a:hover {{ background: #f90; color: #111; }}
    .nav-count  {{ font-size: 0.75rem; opacity: 0.75; }}

    /* ── Page chrome ── */
    .page-wrap  {{ padding: 1.2rem 2rem 3rem; }}
    h1          {{ color: #f90; margin-bottom: 0.2rem; }}
    .meta       {{ font-size: 0.8rem; color: #888; margin-bottom: 1.5rem; }}

    /* ── Sections ── */
    section     {{ margin-bottom: 3rem; scroll-margin-top: 52px; }}
    h2          {{ color: #f90; border-bottom: 1px solid #333;
                   padding-bottom: 0.3rem; margin-bottom: 0.6rem; }}
    .section-count  {{ font-size: 0.75rem; color: #888; font-weight: normal; }}
    .filter-note    {{ color: #666; }}
    .section-header {{ display: flex; align-items: center; gap: 1rem;
                        flex-wrap: wrap; margin-bottom: 0.4rem; }}
    .section-header h2 {{ margin: 0; }}
    .section-btns   {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}

    /* ── Table ── */
    table       {{ border-collapse: collapse; width: 100%; margin-top: 0.5rem; }}
    th          {{ background: #222; color: #f90; padding: 7px 10px;
                   text-align: left; }}
    td          {{ padding: 5px 10px; border-bottom: 1px solid #2a2a2a;
                   vertical-align: top; }}
    tr:hover td {{ background: #1a1a1a; }}
    a           {{ color: #4af; text-decoration: none; }}
    a:hover     {{ text-decoration: underline; }}
    .seeds      {{ color: #4c4; }}
    .leeches    {{ color: #c44; }}
    .hash       {{ font-size: 0.7rem; color: #666; }}

    /* ── Multi-site badge ── */
    .multi-badge {{ font-size: 0.7rem; cursor: help;
                    background: #1a3a1a; color: #4c4;
                    border: 1px solid #4c4; border-radius: 3px;
                    padding: 0 0.3rem; margin-left: 0.4rem;
                    vertical-align: middle; }}

    /* ── Checkboxes ── */
    .cb-cell        {{ width: 32px; text-align: center; padding: 4px 6px; }}
    .row-cb, .select-all {{ cursor: pointer; width: 15px; height: 15px;
                              accent-color: #f90; }}
    tr.checked-row td {{ background: #1e1e00 !important; }}

    /* ── Buttons ── */
    .open-top3, .open-all-top3, .open-checked {{
      cursor: pointer; border: none; border-radius: 4px;
      font-size: 0.8rem; font-weight: 600; padding: 0.3rem 0.75rem;
      transition: background 0.15s, transform 0.1s; white-space: nowrap;
    }}
    .mag-btn {{
      cursor: pointer; background: none; border: none;
      font-size: 1rem; padding: 0;
    }}
    .mag-btn:hover {{ filter: brightness(1.4); }}

    .open-top3 {{
      background: #1a3a1a; color: #4c4; border: 1px solid #4c4;
    }}
    .open-top3:hover  {{ background: #4c4; color: #111; transform: scale(1.03); }}
    .open-top3:active {{ transform: scale(0.97); }}

    .open-checked {{
      background: #2a1a3a; color: #c8f; border: 1px solid #c8f;
    }}
    .open-checked:hover:not(:disabled)  {{ background: #c8f; color: #111;
                                            transform: scale(1.03); }}
    .open-checked:active:not(:disabled) {{ transform: scale(0.97); }}
    .open-checked:disabled {{ opacity: 0.35; cursor: default; }}

    .open-all-top3 {{
      background: #1a1a3a; color: #f90; border: 1px solid #f90;
      margin-left: auto;
    }}
    .open-all-top3:hover  {{ background: #f90; color: #111;
                               transform: scale(1.03); }}
    .open-all-top3:active {{ transform: scale(0.97); }}

    /* ── Back to top ── */
    .back-top       {{ display: inline-block; margin-top: 0.6rem;
                        font-size: 0.8rem; color: #666; }}
    .back-top:hover {{ color: #f90; }}

    /* ── Search URL links (collapsible) ── */
    .search-links   {{ margin: 0.4rem 0 0.6rem; }}
    .search-links summary {{
      cursor: pointer; font-size: 0.8rem; color: #888;
      list-style: none; display: inline-flex; align-items: center; gap: 0.3rem;
      user-select: none;
    }}
    .search-links summary::-webkit-details-marker {{ display: none; }}
    .search-links[open] summary {{ color: #f90; }}
    .search-url-list {{
      margin: 0.4rem 0 0 1rem; padding: 0;
      list-style: none; display: flex; flex-wrap: wrap; gap: 0.4rem;
    }}
    .search-url-list li a {{
      font-size: 0.78rem; color: #4af;
      background: #1a1a2a; border: 1px solid #333;
      padding: 0.2rem 0.55rem; border-radius: 4px;
      white-space: nowrap; transition: background 0.15s;
    }}
    .search-url-list li a:hover {{ background: #4af; color: #111; }}
  </style>

  <script>
    // Fire a magnet URI using a hidden <a> click so the page does not navigate.
    // window.location.href cancels all pending timeouts on the first magnet —
    // this approach hands each URI to the OS without touching the page state.
    function fireMagnet(uri) {{
      const a = document.createElement('a');
      a.href  = uri;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }}

    function fireSequence(magnets) {{
      return new Promise(resolve => {{
        magnets.forEach((m, idx) => {{
          setTimeout(() => {{
            fireMagnet(m);
            if (idx === magnets.length - 1) setTimeout(resolve, 800);
          }}, idx * 800);
        }});
      }});
    }}

    function withFeedback(btn, origLabel, magnets) {{
      btn.disabled    = true;
      btn.textContent = `⏳ Opening ${{magnets.length}}…`;
      fireSequence(magnets).then(() => {{
        btn.disabled    = false;
        btn.textContent = origLabel;
      }});
    }}

    function openTop3(btn) {{
      const magnets = [];
      let i = 1;
      while (btn.dataset['m' + i]) {{ magnets.push(btn.dataset['m' + i]); i++; }}
      if (!magnets.length) {{ alert('No magnet links on this button.'); return; }}
      withFeedback(btn, btn.textContent.trim(), magnets);
    }}

    function openAllTop3(btn) {{
      const total = btn.dataset.total || '?';
      if (!confirm(
        `Open ${{total}} magnet link(s) — top 3 from every section?\\n` +
        `Transmission will be triggered for each.`
      )) return;
      const magnets = [];
      let i = 1;
      while (btn.dataset['m' + i]) {{ magnets.push(btn.dataset['m' + i]); i++; }}
      withFeedback(btn, btn.textContent.trim(), magnets);
    }}

    function toggleAll(selectAllCb) {{
      const section = selectAllCb.closest('section');
      section.querySelectorAll('.row-cb').forEach(cb => {{
        cb.checked = selectAllCb.checked;
        cb.closest('tr').classList.toggle('checked-row', cb.checked);
      }});
      refreshCheckedBtn(section);
    }}

    function updateSectionBtn(cb) {{
      const section   = cb.closest('section');
      const allCbs    = section.querySelectorAll('.row-cb');
      const selectAll = section.querySelector('.select-all');
      const checked   = section.querySelectorAll('.row-cb:checked');
      cb.closest('tr').classList.toggle('checked-row', cb.checked);
      selectAll.checked       = checked.length === allCbs.length;
      selectAll.indeterminate = checked.length > 0 &&
                                checked.length < allCbs.length;
      refreshCheckedBtn(section);
    }}

    function refreshCheckedBtn(section) {{
      const btn     = section.querySelector('.open-checked');
      const checked = section.querySelectorAll('.row-cb:checked');
      btn.disabled    = checked.length === 0;
      btn.textContent = `☑ Open Checked (${{checked.length}})`;
    }}

    function openChecked(btn) {{
      const section = btn.closest('section');
      const checked = section.querySelectorAll('.row-cb:checked');
      if (!checked.length) return;
      const magnets = Array.from(checked).map(cb => cb.dataset.magnet);
      withFeedback(btn, btn.textContent.trim(), magnets);
    }}
  </script>
</head>
<body>
{nav_html}
  <div class="page-wrap">
    <h1>🧲 Magnet Results</h1>
    <p class="meta">
      {len(deduped)} unique torrent(s){filter_summary}
      {f'({deduped_count} duplicate(s) removed) ' if deduped_count else ''}
      across {len(groups)} search term(s)
      &nbsp;|&nbsp; Generated: {ts}
    </p>
    {sections_html}
  </div>
</body>
</html>"""

    with open(filename, 'w') as f:
        f.write(html)
    print(f'\n[✓] HTML written → {filename}')

# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description='Torrent magnet link finder — reads search_term.txt and '
                    'urls.txt, scrapes each site, outputs magnet_results.html.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    group = p.add_mutually_exclusive_group()
    group.add_argument(
        '--term', metavar='TERM',
        help='Single search term (skips search_term.txt)'
    )
    group.add_argument(
        '--url', metavar='URL',
        help='Single fully-formed search URL to scrape directly'
    )

    p.add_argument(
        '--search-file', metavar='PATH',
        nargs='?', const=DEFAULT_SEARCH_FILE,
        default=DEFAULT_SEARCH_FILE,
        help=f'Path to search_term.txt (default: {DEFAULT_SEARCH_FILE})'
    )
    p.add_argument(
        '--search-file-2', metavar='PATH',
        default=DEFAULT_SEARCH_FILE_2,
        help='Path to a 2nd search_term.txt (e.g. Google Drive) whose terms '
             'are concatenated with --search-file before searching. '
             f'Default from config.ini: {DEFAULT_SEARCH_FILE_2 or "(none)"}'
    )
    p.add_argument(
        '--urls-file', metavar='PATH',
        default=DEFAULT_URLS_FILE,
        help=f'Path to urls.txt (default: {DEFAULT_URLS_FILE})'
    )
    p.add_argument(
        '--no-js', action='store_true',
        help='Disable headless Chrome and use plain HTTP requests instead'
    )
    p.add_argument(
        '--category', metavar='CATEGORY', default=None,
        help='Category for --term mode (e.g. Music, Books). '
             'Ignored when using --search-file (categories come from file headers).'
    )
    p.add_argument(
        '--no-browser', action='store_true',
        help='Do not open magnet_results.html in browser when done'
    )
    p.add_argument(
        '--delay', type=float, default=REQUEST_DELAY, metavar='SECS',
        help=f'Seconds between site requests (default: {REQUEST_DELAY})'
    )
    p.add_argument(
        '--db', metavar='PATH', default=DEFAULT_DB_FILE,
        help=f'SQLite database path (default: {DEFAULT_DB_FILE})'
    )
    p.add_argument(
        '--no-db', action='store_true',
        help='Skip writing to the SQLite database (HTML/CSV only)'
    )
    p.add_argument(
        '--log-dir', metavar='PATH', default=None,
        help=f'Directory for log files. If omitted logs go to stdout only. '
             f'Cron default: {DEFAULT_LOG_DIR}'
    )
    p.add_argument(
        '--cron', action='store_true',
        help='Cron/headless mode: implies --no-browser, enables file logging '
             f'to {DEFAULT_LOG_DIR}, exits with code 1 on any scrape error'
    )
    p.add_argument(
        '--send-to-transmission', action='store_true',
        help='Send magnet links via transmission-remote to localhost:9091 '
             'instead of opening them in the browser'
    )
    p.add_argument(
        '--download-dir', metavar='PATH', default='~/Movies',
        help='Download directory passed to Transmission (default: ~/Movies). '
             'Only used with --send-to-transmission.'
    )
    return p.parse_args()

# ══════════════════════════════════════════════════════════════════════════
# Transmission
# ══════════════════════════════════════════════════════════════════════════

def send_to_transmission(magnets: list[str], download_dir: str):
    """Send each magnet to Transmission via transmission-remote."""
    dest = os.path.expanduser(download_dir)
    ok = fail = 0
    for magnet in magnets:
        try:
            result = subprocess.run(
                ['transmission-remote', 'localhost:9091',
                 '--download-dir', dest, '--add', magnet],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                log.info(f'[Transmission] Added: {extract_name(magnet)}')
                ok += 1
            else:
                log.error(f'[Transmission] Failed ({result.returncode}): '
                          f'{result.stderr.strip() or result.stdout.strip()}')
                fail += 1
        except FileNotFoundError:
            log.error('[Transmission] transmission-remote not found — '
                      'install it with: brew install transmission-cli')
            return
    log.info(f'[Transmission] {ok} added, {fail} failed → {dest}')

# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    log_dir = args.log_dir or (DEFAULT_LOG_DIR if args.cron else None)
    global log
    log = setup_logging(log_dir)

    no_browser = args.no_browser or args.cron
    js_mode    = not args.no_js

    check_dependencies(js_mode=js_mode)

    if js_mode:
        log.info('JS mode — headless Chrome active')
    if args.cron:
        log.info('Cron mode active')

    # ── Build scrape jobs: list of (label, category, [urls]) ──────────────
    jobs: list[tuple[str, str, list[str]]] = []

    if args.url:
        jobs = [(args.url, 'Default', [args.url])]
    elif args.term:
        cat  = normalise_category(args.category) if args.category else 'Default'
        urls = build_search_urls(args.term, args.urls_file)
        jobs = [(args.term, cat, urls)]
    else:
        term_cats = read_search_terms(args.search_file, args.search_file_2)
        for term, category in term_cats:
            urls = build_search_urls(term, args.urls_file)
            jobs.append((term, category, urls))

    # ── Database connection ────────────────────────────────────────────────
    conn = None
    if not args.no_db:
        try:
            conn = open_db(args.db)
        except Exception as e:
            log.error(f'DB open failed: {e}')

    # ── Scrape ────────────────────────────────────────────────────────────
    all_results: list[dict] = []
    had_error = False

    for idx, (label, category, urls) in enumerate(jobs):
        if len(jobs) > 1:
            log.info(f'[{idx+1}/{len(jobs)}] [{category}] {label}')

        term_results: list[dict] = []
        for i, url in enumerate(urls):
            try:
                results = scrape(url, js_mode=js_mode)
                for r in results:
                    r['search_term'] = label
                    r['category']    = category
                term_results.extend(results)
                if not results:
                    log.warning(f'0 results from {urlparse(url).netloc}')
                    had_error = True
            except Exception as e:
                log.error(f'Scrape failed for {url}: {e}')
                had_error = True
            if i < len(urls) - 1:
                time.sleep(args.delay)

        all_results.extend(term_results)

        if conn is not None:
            try:
                write_db_term(conn, term_results, label, category, js_mode)
            except Exception as e:
                log.error(f'DB write failed for [{label}]: {e}')
                had_error = True

    if conn is not None:
        conn.close()

    # ── Output ────────────────────────────────────────────────────────────
    out_dir = Path(DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now().strftime('%Y-%m-%d_%H-%M')
    ts_html = str(out_dir / f'magnet_results_{ts}.html')
    ts_csv  = str(out_dir / f'magnet_results_{ts}.csv')

    page_label = jobs[0][0] if len(jobs) == 1 else f'{len(jobs)} terms'
    term_urls  = {label: urls for label, _, urls in jobs}

    print_summary(all_results)
    write_html(all_results, page_label, filename=ts_html, term_urls=term_urls)
    write_csv(all_results, ts_csv)

    if args.send_to_transmission:
        deduped_top, _ = deduplicate(all_results)
        magnets = [r['magnet'] for r in deduped_top if seeded(r)]
        if magnets:
            send_to_transmission(magnets, args.download_dir)
        else:
            log.warning('No seeded magnets to send to Transmission.')
    elif not no_browser and all_results:
        webbrowser.open(f'file://{os.path.abspath(ts_html)}')

    if args.cron and had_error:
        sys.exit(1)

if __name__ == '__main__':
    main()
