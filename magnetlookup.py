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
    python3 magnetlookup.py --url "https://thepiratebay.org/search.php?q=Polymer+Materials"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INPUT FILES (defaults match urlhtml_ic.py):

    search_term.txt   One search term per line
                      ~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/search_term.txt

    urls.txt          One URL template per line (with placeholder search param)
                      ~/Documents/Development/urls.txt

OUTPUT:

    magnet_results.html   Browser-ready results grouped by search term
                          Sticky nav bar, Open Top 3 buttons, checkboxes, Open Checked
    magnet_results.csv    Full log of all results including zero-seed

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEPENDENCIES:

    pip3 install requests beautifulsoup4 lxml --break-system-packages
    pip3 install selenium --break-system-packages   # required (JS mode is default); selenium-manager handles chromedriver automatically
"""

import sys
import os
import csv
import re
import time
import argparse
import webbrowser
import sqlite3
import logging
from collections import OrderedDict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote_plus
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

import configparser as _cp
import re as _re

def _expand(value: str) -> str:
    """
    Expand ${VAR:-default} and $VAR patterns using os.environ.
    Uses a brace-aware parser so nested ${HOME} inside defaults works correctly.
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
            m = _re.match(r'[A-Za-z_][A-Za-z0-9_]*', value[i + 1:])
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
    'thepiratebay': {
        # Verified March 2026 — TPB now uses <ol>/<li> layout
        'row_selector':      'ol.view-single li.list-entry',
        'title_selector':    '.item-name.item-title a',
        'magnet_selector':   'a[href^="magnet:"]',
        'seed_selector':     '.item-seed',
        'leech_selector':    '.item-leech',
        'size_regex':        r'([\d.]+\s*[KMGT]iB)',
        'js_wait_selector':  'ol.view-single',
    },
    '1337x': {
        # Verified March 2026 — magnets on detail pages only
        'row_selector':      'table.table-list tbody tr',
        'title_selector':    'td.name a:nth-of-type(2)',
        'detail_page_magnet': True,
        'detail_base_url':   'https://www.1377x.to',
        'magnet_selector':   'a[href^="magnet:"]',
        'seed_selector':     'td.seeds',
        'leech_selector':    'td.leeches',
        'size_selector':     'td.size',
        'js_wait_selector':  'table.table-list',
    },
    '1377x': {
        # Mirror of 1337x — identical DOM
        'row_selector':      'table.table-list tbody tr',
        'title_selector':    'td.name a:nth-of-type(2)',
        'detail_page_magnet': True,
        'detail_base_url':   'https://www.1377x.to',
        'magnet_selector':   'a[href^="magnet:"]',
        'seed_selector':     'td.seeds',
        'leech_selector':    'td.leeches',
        'size_selector':     'td.size',
        'js_wait_selector':  'table.table-list',
    },
    'rarbg': {
        # Verified March 2026 — magnets on detail pages only
        # 'rarbg' matches rarbg.to; 'rargb' matches rargb.to (common mirror spelling)
        'row_selector':      'table.lista2t tr.lista2',
        'title_selector':    'td:nth-of-type(2) a',
        'detail_page_magnet': True,
        'detail_base_url':   'https://rargb.to',
        'magnet_selector':   'a[href^="magnet:"]',
        'seed_selector':     'td:nth-of-type(6)',
        'leech_selector':    'td:nth-of-type(7)',
        'size_selector':     'td:nth-of-type(5)',
        'js_wait_selector':  'table.lista2t',
    },
    'rargb': {
        # Mirror spelling of rarbg — rargb.to — identical DOM
        'row_selector':      'table.lista2t tr.lista2',
        'title_selector':    'td:nth-of-type(2) a',
        'detail_page_magnet': True,
        'detail_base_url':   'https://rargb.to',
        'magnet_selector':   'a[href^="magnet:"]',
        'seed_selector':     'td:nth-of-type(6)',
        'leech_selector':    'td:nth-of-type(7)',
        'size_selector':     'td:nth-of-type(5)',
        'js_wait_selector':  'table.lista2t',
    },
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
# URL builder  (from urlhtml_ic.py)
# ══════════════════════════════════════════════════════════════════════════

def inject_search_term(template_url: str, term: str) -> str:
    """
    Replace the search parameter in a URL template with the given term.
    Handles ?q=, ?search=, and falls back to adding ?search= if neither exists.
    Mirrors the logic in urlhtml_ic.py replace_search_parameter().
    """
    parsed = urlparse(template_url)
    params = parse_qs(parsed.query)

    if 'q' in params:
        params['q'] = [term]
    elif 'search' in params:
        params['search'] = [term]
    else:
        params['search'] = [term]

    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

def build_search_urls(term: str, urls_file: str) -> list[str]:
    """
    Read URL templates from urls_file and inject the search term into each.
    Returns a list of ready-to-fetch search URLs.
    """
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

def read_search_terms(path: str) -> list[str]:
    """Read non-empty lines from the iCloud search_term.txt file."""
    try:
        with open(path, 'r') as f:
            terms = [l.strip() for l in f if l.strip()]
        if not terms:
            print(f'[ERROR] No search terms found in {path}')
            sys.exit(1)
        print(f'[INFO] {len(terms)} search term(s) loaded from {path}')
        for t in terms:
            print(f'         • {t}')
        return terms
    except FileNotFoundError:
        print(f'[ERROR] Search term file not found: {path}')
        print( '        Add terms (one per line) to that file, or use --term')
        sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════
# Selenium headless driver
# ══════════════════════════════════════════════════════════════════════════
# Uses selenium-manager (built into Selenium 4.6+) to automatically locate
# or download a matching chromedriver. No manual chromedriver install needed.

def build_headless_driver() -> 'webdriver.Chrome':
    """
    Launch headless Chrome via selenium-manager.
    selenium-manager (Selenium 4.6+) auto-locates or downloads a chromedriver
    that matches the installed Chrome version — no manual driver management needed.
    """
    opts = ChromeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument(f'user-agent={HEADERS["User-Agent"]}')
    opts.add_experimental_option('excludeSwitches', ['enable-logging'])
    # Passing no Service() lets selenium-manager handle driver location/download
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver

def fetch_js(url: str, wait_selector: str | None = None,
             driver: 'webdriver.Chrome | None' = None) -> str | None:
    """
    Fetch a URL with headless Chrome and return fully-rendered HTML.
    If driver is provided, reuses it (no launch/quit overhead).
    If driver is None, creates a temporary one and quits it when done.
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
                      f'use --no-js to skip headless Chrome')
                return None
        return html
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403 and not js_mode:
            print(f'  [WARN] {urlparse(url).netloc} blocked the request (403) — '
                  f'use --no-js to skip headless Chrome')
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
    Uses the site profile if one exists, otherwise falls back to a
    raw regex scan for any magnet: URI present in the HTML.
    driver: shared Selenium driver passed through for detail page fetches,
            avoiding a new Chrome launch per row.
    """
    profile = get_profile(source_url)
    soup    = BeautifulSoup(html, 'lxml')
    results = []

    if profile:
        rows = soup.select(profile['row_selector'])
        for row in rows:
            title_tag = row.select_one(profile['title_selector'])
            # Fallback: if nth-of-type CSS selector returns no text (BS4 quirk),
            # walk tds directly and grab the first <a> with an href from td[1]
            if not title_tag or not title_tag.get_text(strip=True):
                tds = row.find_all('td', recursive=False)
                if len(tds) > 1:
                    title_tag = tds[1].find('a', href=True)
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)

            # Seeds / leeches
            s_tag = row.select_one(profile['seed_selector'])
            l_tag = row.select_one(profile['leech_selector'])
            seeds   = s_tag.get_text(strip=True) if s_tag else '?'
            leeches = l_tag.get_text(strip=True) if l_tag else '?'

            # Size
            size = '?'
            if profile.get('size_selector'):
                sz = row.select_one(profile['size_selector'])
                size = sz.get_text(strip=True) if sz else '?'
            elif profile.get('size_regex'):
                m = re.search(profile['size_regex'], row.get_text(' '))
                size = m.group(1) if m else '?'

            # Magnet — inline on page or via detail page
            if profile.get('detail_page_magnet'):
                href = title_tag.get('href', '')
                if not href:
                    continue
                base       = profile.get('detail_base_url', '')
                detail_url = base + href if href.startswith('/') else href
                print(f'    [detail] {title[:55]}')
                magnet = fetch_detail_magnet(
                    detail_url, profile['magnet_selector'], js_mode,
                    driver=driver   # reuse the shared Chrome instance
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
        # Generic fallback
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

def scrape(url: str, js_mode: bool = False) -> list[dict]:
    """
    Fetch one search URL and return its results.
    In JS mode, creates ONE shared Chrome driver for the search page AND all
    subsequent detail page fetches — avoids launching Chrome once per row.
    """
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
    """
    Configure logging to stdout and optionally a rotating daily log file.
    Returns the root logger. Call once at startup.
    """
    log = logging.getLogger('magnetlookup')
    log.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                             datefmt='%Y-%m-%d %H:%M:%S')

    # Always log to stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    # Optionally log to a dated file
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        date_str  = datetime.now().strftime('%Y-%m-%d')
        log_path  = Path(log_dir) / f'magnetlookup_{date_str}.log'
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fh.setFormatter(fmt)
        log.addHandler(fh)
        log.info(f'Logging to {log_path}')

    return log

# Module-level logger — replaced by setup_logging() at startup
log = logging.getLogger('magnetlookup')

# ══════════════════════════════════════════════════════════════════════════
# SQLite database
# ══════════════════════════════════════════════════════════════════════════

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT    NOT NULL,          -- ISO timestamp of this scrape run
    terms       TEXT    NOT NULL,          -- comma-separated search terms
    js_mode     INTEGER NOT NULL DEFAULT 0,
    total_found INTEGER NOT NULL DEFAULT 0,
    dupes_removed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS torrents (
    info_hash   TEXT PRIMARY KEY,          -- SHA1 from magnet xt= param
    title       TEXT NOT NULL,
    size        TEXT,
    best_seeds  INTEGER NOT NULL DEFAULT 0,
    best_leeches INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,             -- ISO timestamp
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

CREATE TABLE IF NOT EXISTS search_terms (
    term        TEXT PRIMARY KEY,
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
"""

def open_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database, apply schema, return connection."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    conn.commit()
    log.info(f'Database: {db_path}')
    return conn

def write_db(conn: sqlite3.Connection,
             all_results: list[dict],
             deduped: list[dict],
             jobs: list[tuple[str, list[str]]],
             js_mode: bool,
             deduped_count: int):
    """
    Persist scrape results to SQLite.
    - Upserts each unique torrent (updates seed count if higher).
    - Records which sites carried each torrent.
    - Logs the scrape run and links results to it.
    """
    now       = datetime.now().isoformat(timespec='seconds')
    terms_str = ', '.join(label for label, _ in jobs)

    # ── Insert scrape run ─────────────────────────────────────────────────
    cur = conn.execute(
        "INSERT INTO searches (run_at, terms, js_mode, total_found, dupes_removed) "
        "VALUES (?, ?, ?, ?, ?)",
        (now, terms_str, int(js_mode), len(deduped), deduped_count)
    )
    search_id = cur.lastrowid

    # ── Upsert each unique torrent ────────────────────────────────────────
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

        # Insert or update — if hash exists, update seed count if higher
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

        # Record which sites carried this torrent
        for site in r.get('found_on', [urlparse(r['source_url']).netloc]):
            conn.execute(
                "INSERT OR IGNORE INTO torrent_sites (info_hash, site) VALUES (?, ?)",
                (ih, site)
            )

        # Link to this search run
        conn.execute(
            "INSERT INTO search_results (search_id, info_hash, search_term, seeds) "
            "VALUES (?, ?, ?, ?)",
            (search_id, ih, r.get('search_term', ''), seeds)
        )

    # ── Upsert each unique search term used in this run ───────────────────
    for label, _ in jobs:
        conn.execute(
            "INSERT INTO search_terms (term, first_used, last_used, use_count) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(term) DO UPDATE SET "
            "    last_used = excluded.last_used, "
            "    use_count = use_count + 1",
            (label, now, now)
        )

    conn.commit()
    log.info(f'DB: {len(deduped)} torrents written (run id={search_id})')
    return search_id


# ══════════════════════════════════════════════════════════════════════════
# Output — CSV
# ══════════════════════════════════════════════════════════════════════════

def write_csv(all_results: list[dict], filename: str):
    fields = ['search_term', 'title', 'size', 'seeds', 'leeches',
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
    def seeded(r):
        s = str(r.get('seeds', '0')).strip()
        try:
            return int(s) > 0
        except ValueError:
            return s not in ('0', '?', '')

    shown  = [r for r in all_results if seeded(r)]
    hidden = len(all_results) - len(shown)

    if not shown:
        print('\n  No seeded magnet links found.\n')
        return

    # Deduplicate by info_hash — keep highest-seeded copy per hash
    def seed_int(r: dict) -> int:
        try:
            return int(str(r.get('seeds', '0')).strip())
        except ValueError:
            return 0

    from collections import defaultdict
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
               filename: str = 'magnet_results.html',
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

    def seeded(r: dict) -> bool:
        s = str(r.get('seeds', '0')).strip()
        try:
            return int(s) > 0
        except ValueError:
            return s not in ('0', '?', '')

    shown    = [r for r in all_results if seeded(r)]
    filtered = len(all_results) - len(shown)

    # Group by search term
    groups: dict[str, list[dict]] = OrderedDict()
    for r in shown:
        term = r.get('search_term', search_label)
        groups.setdefault(term, []).append(r)

    def slugify(text: str) -> str:
        return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

    # ── Nav items ─────────────────────────────────────────────────────────
    nav_items = ''
    for term, rows in groups.items():
        slug = slugify(term)
        nav_items += (
            f'<a href="#{slug}">{term} '
            f'<span class="nav-count">({len(rows)})</span></a>\n'
        )

    # ── Global Open All Top 3s button ─────────────────────────────────────
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

    # ── Deduplicate by info_hash across all sites ────────────────────────
    # When the same torrent appears on multiple sites, keep only the copy
    # with the highest seed count. Collect the site names that were merged
    # so they can be shown as a tooltip on the surviving row.
    def seed_int(r: dict) -> int:
        try:
            return int(str(r.get('seeds', '0')).strip())
        except ValueError:
            return 0

    # Group all copies of each hash, track which sites carried it
    from collections import defaultdict
    hash_copies: dict[str, list[dict]] = defaultdict(list)
    for r in shown:
        ih = r.get('info_hash', '')
        hash_copies[ih if ih else id(r)].append(r)

    deduped: list[dict] = []
    for ih, copies in hash_copies.items():
        best = max(copies, key=seed_int)
        # Annotate with all sites that had this torrent
        all_sites = sorted({urlparse(c['source_url']).netloc for c in copies})
        best = dict(best)   # shallow copy so we don't mutate shared state
        best['found_on'] = all_sites
        best['dup_count'] = len(copies)
        deduped.append(best)

    deduped_count = len(shown) - len(deduped)

    # Rebuild groups from deduped list, preserving original term order
    groups = OrderedDict()
    for r in deduped:
        term = r.get('search_term', search_label)
        groups.setdefault(term, []).append(r)

    # ── Sections ──────────────────────────────────────────────────────────
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

        # Build search URL links for this term
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

    // Fire magnets in sequence with 800 ms gaps. Returns a Promise.
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

    // Disable a button, show progress, re-enable when done.
    function withFeedback(btn, origLabel, magnets) {{
      btn.disabled    = true;
      btn.textContent = `⏳ Opening ${{magnets.length}}…`;
      fireSequence(magnets).then(() => {{
        btn.disabled    = false;
        btn.textContent = origLabel;
      }});
    }}

    // ── Top-3 buttons ────────────────────────────────────────────────────
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

    // ── Checkbox logic ───────────────────────────────────────────────────
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

    // ── Open Checked button ──────────────────────────────────────────────
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

    # Input mode — all optional; default is --search-file
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
        '--urls-file', metavar='PATH',
        default=DEFAULT_URLS_FILE,
        help=f'Path to urls.txt (default: {DEFAULT_URLS_FILE})'
    )
    p.add_argument(
        '--no-js', action='store_true',
        help='Disable headless Chrome and use plain HTTP requests instead'
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
    return p.parse_args()

# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    # ── Logging ───────────────────────────────────────────────────────────
    log_dir = args.log_dir or (DEFAULT_LOG_DIR if args.cron else None)
    global log
    log = setup_logging(log_dir)

    # --cron implies --no-browser
    no_browser = args.no_browser or args.cron

    js_mode = not args.no_js

    check_dependencies(js_mode=js_mode)

    if js_mode:
        log.info('JS mode — headless Chrome active')
    if args.cron:
        log.info('Cron mode active')

    # ── Build scrape jobs: list of (label, [urls]) ────────────────────────
    jobs: list[tuple[str, list[str]]] = []

    if args.url:
        jobs = [(args.url, [args.url])]
    elif args.term:
        urls = build_search_urls(args.term, args.urls_file)
        jobs = [(args.term, urls)]
    else:
        terms = read_search_terms(args.search_file)
        for term in terms:
            urls = build_search_urls(term, args.urls_file)
            jobs.append((term, urls))

    # ── Open DB once upfront ─────────────────────────────────────────────
    conn = None
    if not args.no_db:
        try:
            conn = open_db(args.db)
        except Exception as e:
            log.error(f'DB open failed: {e}')

    # ── Shared helpers ────────────────────────────────────────────────────
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

    # ── Scrape ────────────────────────────────────────────────────────────
    all_results: list[dict] = []
    had_error = False

    for idx, (label, urls) in enumerate(jobs):
        if len(jobs) > 1:
            log.info(f'[{idx+1}/{len(jobs)}] Searching: {label}')

        term_results: list[dict] = []
        for i, url in enumerate(urls):
            try:
                results = scrape(url, js_mode=js_mode)
                for r in results:
                    r['search_term'] = label
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

        # ── Write this term's results to DB immediately ───────────────────
        if conn and term_results:
            try:
                from collections import defaultdict
                shown_t = [r for r in term_results if seeded(r)]
                hc: dict[str, list[dict]] = defaultdict(list)
                for r in shown_t:
                    hc[r.get('info_hash', '') or str(id(r))].append(r)
                deduped_t: list[dict] = []
                for ih, copies in hc.items():
                    best = dict(max(copies, key=seed_int))
                    best['found_on'] = sorted(
                        {urlparse(c['source_url']).netloc for c in copies})
                    best['dup_count'] = len(copies)
                    deduped_t.append(best)
                dup_count_t = len(shown_t) - len(deduped_t)
                write_db(conn, term_results, deduped_t,
                         [(label, urls)], js_mode, dup_count_t)
                log.info(f'DB: saved {len(deduped_t)} torrent(s) for "{label}"')
            except Exception as e:
                log.error(f'DB write failed for "{label}": {e}')
                had_error = True

    if conn:
        conn.close()

    # ── Deduplicate full result set for output ────────────────────────────
    from collections import defaultdict
    shown = [r for r in all_results if seeded(r)]
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

    deduped_count = len(shown) - len(deduped)

    # ── Output ────────────────────────────────────────────────────────────
    page_label = jobs[0][0] if len(jobs) == 1 else f'{len(jobs)} terms'
    term_urls  = {label: urls for label, urls in jobs}

    out_dir = Path(DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts      = datetime.now().strftime('%Y-%m-%d_%H-%M')
    ts_html = out_dir / f'magnet_results_{ts}.html'
    ts_csv  = out_dir / f'magnet_results_{ts}.csv'

    print_summary(all_results)
    write_html(all_results, page_label, filename=str(ts_html), term_urls=term_urls)
    write_csv(all_results, str(ts_csv))

    if not no_browser and all_results:
        webbrowser.open(f'file://{ts_html.resolve()}')

    # Cron: exit 1 if any site returned 0 results, so cron can alert
    if args.cron and had_error:
        sys.exit(1)

if __name__ == '__main__':
    main()
