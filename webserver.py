#!/usr/bin/env python3
"""
webserver.py — Magnet Library Web Interface

Serves a dynamic web UI over the magnet_library.db SQLite database.
No external dependencies — uses only Python stdlib.

Usage:
    python3 webserver.py
    python3 webserver.py --port 8080 --db /path/to/magnet_library.db

Then open:  http://localhost:8080

Views:
    /               Library — all unique torrents, searchable + sortable
    /history        Search history — every scrape run with results
    /search?q=term  Filter library by search term

API (JSON):
    /api/torrents               All torrents (supports ?q=, ?sort=, ?order=, ?limit=)
    /api/torrents/<hash>        Single torrent detail + sites
    /api/searches               All scrape runs
    /api/searches/<id>          Single run + its results
    /api/stats                  Counts for dashboard header
"""

import sys
import os
import json
import sqlite3
import argparse
import webbrowser
import configparser
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

# ── Defaults (read from config.ini if present, else fall back) ───────────────
def _load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg_path = Path(__file__).parent / 'config.ini'
    if cfg_path.exists():
        import re as _re
        raw = cfg_path.read_text()
        def _expand_plain(m):
            return os.environ.get(m.group(1), m.group(0))
        raw = _re.sub(r'\$\{([^}:]+)\}', _expand_plain, raw)
        raw = os.path.expandvars(raw)
        def _expand_default(m):
            var, _, default = m.group(1).partition(':-')
            return os.environ.get(var.strip(), default.strip())
        raw = _re.sub(r'\$\{([^}]+)\}', _expand_default, raw)
        cfg.read_string(raw)
    return cfg

_cfg = _load_config()

def _cfg_get(section, key, fallback):
    try:
        return os.path.expanduser(_cfg.get(section, key))
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback

def _cfg_int(section, key, fallback):
    try:
        return _cfg.getint(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
        return fallback

DEFAULT_DB   = _cfg_get('paths', 'db_file',
    str(Path.home() / 'Documents/Development/magnet_library.db'))
DEFAULT_PORT = _cfg_int('server', 'port', 8080)

# ══════════════════════════════════════════════════════════════════════════
# Database helpers
# ══════════════════════════════════════════════════════════════════════════

_DB_SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_sr_search  ON search_results(search_id);
CREATE INDEX IF NOT EXISTS idx_sr_term    ON search_results(search_term);
CREATE INDEX IF NOT EXISTS idx_sr_hash    ON search_results(info_hash);
CREATE INDEX IF NOT EXISTS idx_t_seeds    ON torrents(best_seeds DESC);
CREATE INDEX IF NOT EXISTS idx_t_seen     ON torrents(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_terms_used ON search_terms(last_used DESC);
CREATE INDEX IF NOT EXISTS idx_terms_cat  ON search_terms(category);
"""

def get_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DB_SCHEMA)
    return conn


def query(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(conn, sql: str, params=()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None

# ══════════════════════════════════════════════════════════════════════════
# API handlers
# ══════════════════════════════════════════════════════════════════════════

def api_stats(conn) -> dict:
    total    = query_one(conn, "SELECT COUNT(*) AS n FROM torrents")['n']
    searches = query_one(conn, "SELECT COUNT(*) AS n FROM searches")['n']
    sites    = query_one(conn,
        "SELECT COUNT(DISTINCT site) AS n FROM torrent_sites")['n']
    last_run = query_one(conn,
        "SELECT run_at FROM searches ORDER BY run_at DESC LIMIT 1")
    return {
        'total_torrents': total,
        'total_searches': searches,
        'total_sites':    sites,
        'last_run':       last_run['run_at'] if last_run else None,
    }


def api_categories(conn) -> list[dict]:
    """Return all categories that have at least one search term."""
    return query(conn,
        "SELECT DISTINCT category, COUNT(*) AS term_count "
        "FROM search_terms "
        "GROUP BY category "
        "ORDER BY category ASC")


def api_terms(conn, category: str = '') -> list[dict]:
    """
    Return all stored search terms ordered by most recently used.
    Optionally filter by category.
    """
    if category:
        return query(conn,
            "SELECT term, category, use_count, last_used FROM search_terms "
            "WHERE category = ? "
            "ORDER BY last_used DESC", (category,))
    return query(conn,
        "SELECT term, category, use_count, last_used FROM search_terms "
        "ORDER BY last_used DESC")


def api_purge(conn) -> dict:
    """
    Delete all rows from every table and reset autoincrement sequences.
    Schema (tables + indexes) is preserved — only data is removed.
    """
    tables = ['search_results', 'torrent_sites', 'torrents',
              'searches', 'search_terms', 'categories']
    for t in tables:
        conn.execute(f'DELETE FROM {t}')
    # Reset SQLite autoincrement counters
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                 "('searches','search_results')")
    conn.commit()
    return {'purged': True, 'tables': tables}


def api_torrents(conn, q='', sort='best_seeds', order='desc',
                 limit=200, offset=0, term='', category='') -> list[dict]:
    allowed_sort = {'best_seeds', 'best_leeches', 'title', 'last_seen',
                    'first_seen', 'size'}
    sort  = sort  if sort  in allowed_sort else 'best_seeds'
    order = 'DESC' if order.upper() == 'DESC' else 'ASC'

    params = []
    where  = ['1=1']

    if q:
        where.append("t.title LIKE ?")
        params.append(f'%{q}%')

    if term:
        where.append("""t.info_hash IN (
            SELECT DISTINCT info_hash FROM search_results
            WHERE search_term LIKE ?)""")
        params.append(f'%{term}%')

    if category:
        where.append("""t.info_hash IN (
            SELECT DISTINCT sr.info_hash FROM search_results sr
            JOIN search_terms st ON st.term = sr.search_term
            WHERE st.category = ?)""")
        params.append(category)

    sql = f"""
        SELECT t.*,
               GROUP_CONCAT(DISTINCT ts.site) AS sites
        FROM   torrents t
        LEFT JOIN torrent_sites ts ON ts.info_hash = t.info_hash
        WHERE  {' AND '.join(where)}
        GROUP  BY t.info_hash
        ORDER  BY t.{sort} {order}
        LIMIT  ? OFFSET ?
    """
    params += [limit, offset]
    rows = query(conn, sql, params)
    for r in rows:
        r['sites'] = r['sites'].split(',') if r['sites'] else []
    return rows


def api_torrent(conn, info_hash: str) -> dict | None:
    t = query_one(conn,
        "SELECT * FROM torrents WHERE info_hash = ?", (info_hash,))
    if not t:
        return None
    t['sites'] = [r['site'] for r in query(conn,
        "SELECT site FROM torrent_sites WHERE info_hash = ?", (info_hash,))]
    t['appearances'] = query(conn, """
        SELECT sr.search_term, sr.seeds, s.run_at
        FROM   search_results sr
        JOIN   searches s ON s.id = sr.search_id
        WHERE  sr.info_hash = ?
        ORDER  BY s.run_at DESC
    """, (info_hash,))
    return t


def api_searches(conn) -> list[dict]:
    return query(conn,
        "SELECT * FROM searches ORDER BY run_at DESC LIMIT 100")


def api_search(conn, search_id: int) -> dict | None:
    s = query_one(conn, "SELECT * FROM searches WHERE id = ?", (search_id,))
    if not s:
        return None
    s['results'] = query(conn, """
        SELECT t.title, t.size, t.best_seeds, t.magnet,
               sr.search_term, sr.seeds,
               GROUP_CONCAT(DISTINCT ts.site) AS sites
        FROM   search_results sr
        JOIN   torrents t  ON t.info_hash  = sr.info_hash
        LEFT JOIN torrent_sites ts ON ts.info_hash = sr.info_hash
        WHERE  sr.search_id = ?
        GROUP  BY sr.info_hash
        ORDER  BY sr.seeds DESC
    """, (search_id,))
    for r in s['results']:
        r['sites'] = r['sites'].split(',') if r['sites'] else []
    return s

# ══════════════════════════════════════════════════════════════════════════
# HTML page (single-page app served from Python)
# ══════════════════════════════════════════════════════════════════════════

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🧲 Magnet Library</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body   { font-family: system-ui, sans-serif; margin: 0;
           background: #111; color: #eee; }

  /* ── Top nav ── */
  #navbar { position: sticky; top: 0; z-index: 100;
            background: #1a1a1a; border-bottom: 2px solid #f90;
            padding: 0.5rem 1.5rem; display: flex; align-items: center;
            gap: 1rem; flex-wrap: wrap; }
  #navbar h1 { margin: 0; font-size: 1.1rem; color: #f90; white-space: nowrap; }
  .nav-link  { color: #ccc; text-decoration: none; font-size: 0.85rem;
               padding: 0.25rem 0.6rem; border-radius: 4px;
               border: 1px solid #444; transition: all 0.15s; }
  .nav-link:hover, .nav-link.active { background: #f90; color: #111;
                                       border-color: #f90; }
  #stats-bar { margin-left: auto; font-size: 0.75rem; color: #888;
               display: flex; gap: 1rem; }

  /* ── Page wrap ── */
  .page { padding: 1.2rem 2rem 3rem; display: none; }
  .page.active { display: block; }

  /* ── Search / filter bar ── */
  .toolbar { display: flex; gap: 0.6rem; flex-wrap: wrap;
             margin-bottom: 1rem; align-items: center; }
  .toolbar input, .toolbar select {
    background: #1a1a1a; border: 1px solid #444; color: #eee;
    padding: 0.35rem 0.7rem; border-radius: 4px; font-size: 0.85rem;
  }
  .toolbar input { flex: 1; min-width: 200px; }
  .toolbar input:focus, .toolbar select:focus {
    outline: none; border-color: #f90; }

  /* ── Table ── */
  table  { border-collapse: collapse; width: 100%; margin-top: 0.5rem; }
  th     { background: #222; color: #f90; padding: 7px 10px;
           text-align: left; cursor: pointer; user-select: none;
           white-space: nowrap; }
  th:hover { background: #2a2a2a; }
  th.sorted-asc::after  { content: ' ▲'; font-size: 0.7rem; }
  th.sorted-desc::after { content: ' ▼'; font-size: 0.7rem; }
  td     { padding: 5px 10px; border-bottom: 1px solid #222;
           vertical-align: top; font-size: 0.85rem; }
  tr:hover td { background: #1a1a1a; }
  a      { color: #4af; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .seeds   { color: #4c4; font-weight: 600; }
  .leeches { color: #c44; }
  .hash    { font-size: 0.7rem; color: #666; font-family: monospace; }
  .sites   { font-size: 0.75rem; color: #888; }
  .multi   { font-size: 0.7rem; background: #1a3a1a; color: #4c4;
             border: 1px solid #4c4; border-radius: 3px;
             padding: 0 0.3rem; margin-left: 0.3rem; }

  /* ── Magnet button ── */
  .mag-btn { cursor: pointer; background: none; border: none;
             font-size: 1rem; padding: 0; }
  .mag-btn:hover { filter: brightness(1.4); }

  /* ── Detail expand icon ── */
  .detail-icon { cursor: pointer; margin-left: 0.4rem; color: #555;
                 font-size: 0.9rem; user-select: none; }
  .detail-icon:hover { color: #f90; }

  /* ── History cards ── */
  .run-card { background: #1a1a1a; border: 1px solid #333;
              border-radius: 6px; margin-bottom: 1rem;
              padding: 0.8rem 1.2rem; }
  .run-card h3 { margin: 0 0 0.3rem; color: #f90; font-size: 0.95rem; }
  .run-meta { font-size: 0.8rem; color: #888; margin-bottom: 0.6rem; }
  .run-link { font-size: 0.8rem; color: #4af; cursor: pointer; }
  .run-link:hover { text-decoration: underline; }

  /* ── Detail panel ── */
  #detail-panel { position: fixed; right: 0; top: 0; bottom: 0;
                  width: 420px; background: #1a1a1a;
                  border-left: 2px solid #f90; padding: 1.5rem;
                  overflow-y: auto; z-index: 200;
                  transform: translateX(100%);
                  transition: transform 0.25s ease; }
  #detail-panel.open { transform: translateX(0); }
  #detail-close { float: right; cursor: pointer; color: #f90;
                  font-size: 1.4rem; line-height: 1; }
  #detail-panel h2 { margin: 0 0 1rem; color: #f90; font-size: 1rem;
                     padding-right: 2rem; }
  .detail-row { margin-bottom: 0.6rem; font-size: 0.85rem; }
  .detail-label { color: #888; font-size: 0.75rem; }
  .appear-item { padding: 0.3rem 0; border-bottom: 1px solid #222;
                 font-size: 0.8rem; }

  /* ── Loading / empty ── */
  .loading { color: #888; padding: 2rem; text-align: center; }
  .empty   { color: #555; padding: 2rem; text-align: center; }

  /* ── Category + term filter rows ── */
  .filter-row  { display: flex; flex-wrap: wrap; gap: 0.4rem;
                 margin-bottom: 0.5rem; align-items: center; }
  .filter-label { font-size: 0.72rem; color: #555; white-space: nowrap;
                  margin-right: 0.2rem; text-transform: uppercase;
                  letter-spacing: 0.05em; min-width: 60px; }

  /* Category pills — amber accent */
  .cat-pill   { cursor: pointer; font-size: 0.78rem; padding: 0.2rem 0.65rem;
                border-radius: 4px; border: 1px solid #554400;
                background: #1a1500; color: #aa8800;
                transition: all 0.15s; white-space: nowrap; }
  .cat-pill:hover  { border-color: #f90; color: #f90; }
  .cat-pill.active { background: #f90; color: #111;
                     border-color: #f90; font-weight: 600; }

  /* Term filter pills ── */
  .term-bar   { display: flex; flex-wrap: wrap; gap: 0.4rem;
                margin-bottom: 0.75rem; align-items: center; }
  .term-label { font-size: 0.75rem; color: #666; white-space: nowrap;
                margin-right: 0.2rem; }
  .term-pill  { cursor: pointer; font-size: 0.78rem; padding: 0.2rem 0.65rem;
                border-radius: 20px; border: 1px solid #444;
                background: #1a1a1a; color: #ccc;
                transition: all 0.15s; white-space: nowrap; }
  .term-pill:hover   { border-color: #f90; color: #f90; }
  .term-pill.active  { background: #f90; color: #111;
                        border-color: #f90; font-weight: 600; }
  .term-pill.all-pill { border-color: #666; }
  .term-pill.all-pill.active { background: #555; color: #eee;
                                border-color: #888; }

  /* ── Pagination ── */
  .pagination { display: flex; gap: 0.5rem; margin-top: 1rem;
               align-items: center; font-size: 0.85rem; }
  .pg-btn { cursor: pointer; background: #1a1a1a; border: 1px solid #444;
            color: #eee; padding: 0.3rem 0.7rem; border-radius: 4px; }
  .pg-btn:hover { border-color: #f90; color: #f90; }
  .pg-btn:disabled { opacity: 0.35; cursor: default; }
  .pg-info { color: #888; }

  /* ── Purge button ── */
  #purge-btn { cursor: pointer; background: #2a0a0a; color: #c44;
               border: 1px solid #c44; border-radius: 4px;
               font-size: 0.78rem; font-weight: 600;
               padding: 0.25rem 0.65rem; margin-left: 0.5rem;
               transition: all 0.15s; white-space: nowrap; }
  #purge-btn:hover  { background: #c44; color: #fff; }
  #purge-btn:active { transform: scale(0.97); }

  /* ── Purge confirm overlay ── */
  #purge-overlay { display: none; position: fixed; inset: 0;
                   background: rgba(0,0,0,0.75); z-index: 500;
                   align-items: center; justify-content: center; }
  #purge-overlay.open { display: flex; }
  #purge-dialog { background: #1a1a1a; border: 2px solid #c44;
                  border-radius: 8px; padding: 2rem 2.5rem;
                  max-width: 420px; text-align: center; }
  #purge-dialog h2 { color: #c44; margin: 0 0 0.75rem; font-size: 1.1rem; }
  #purge-dialog p  { color: #aaa; font-size: 0.88rem; margin: 0 0 1.5rem; }
  .purge-btns { display: flex; gap: 0.75rem; justify-content: center; }
  .purge-cancel { cursor: pointer; background: #222; color: #eee;
                  border: 1px solid #555; border-radius: 4px;
                  padding: 0.4rem 1.2rem; font-size: 0.9rem; }
  .purge-cancel:hover { border-color: #888; }
  .purge-confirm { cursor: pointer; background: #c44; color: #fff;
                   border: none; border-radius: 4px;
                   padding: 0.4rem 1.2rem; font-size: 0.9rem;
                   font-weight: 600; }
  .purge-confirm:hover  { background: #e55; }
  .purge-confirm:disabled { opacity: 0.5; cursor: default; }
</style>
</head>
<body>

<div id="navbar">
  <h1>🧲 Magnet Library</h1>
  <a class="nav-link active" href="#" onclick="showPage('library',this)">Library</a>
  <a class="nav-link"        href="#" onclick="showPage('history',this)">History</a>
  <div id="stats-bar">
    <span id="stat-torrents">— torrents</span>
    <span id="stat-searches">— runs</span>
    <span id="stat-sites">— sites</span>
    <span id="stat-last">last: —</span>
  </div>
  <button id="purge-btn" onclick="openPurge()">🗑 Purge DB</button>
</div>

<!-- ── Purge confirm overlay ─────────────────────────────────────────── -->
<div id="purge-overlay">
  <div id="purge-dialog">
    <h2>⚠️ Purge Database?</h2>
    <p>This will permanently delete all torrents, search history, and
       search terms from the database.<br><br>
       The database file and table structure are kept —
       only the data is removed. This cannot be undone.</p>
    <div class="purge-btns">
      <button class="purge-cancel" onclick="closePurge()">Cancel</button>
      <button class="purge-confirm" id="purge-confirm-btn"
              onclick="executePurge()">Delete Everything</button>
    </div>
  </div>
</div>

<!-- ── Library page ─────────────────────────────────────────────────── -->
<div id="page-library" class="page active">
  <div class="filter-row" id="cat-bar">
    <span class="filter-label">Category:</span>
    <span class="cat-pill active" onclick="setCategory('')">All</span>
  </div>
  <div class="term-bar" id="term-bar">
    <span class="term-label">Term:</span>
    <span class="term-pill all-pill active" onclick="setTerm('')">All</span>
  </div>
  <div class="toolbar">
    <input id="lib-search" type="search" placeholder="Filter by title…"
           oninput="debounceSearch()">
    <select id="lib-sort" onchange="loadLibrary()">
      <option value="best_seeds">Sort: Seeds ↓</option>
      <option value="best_leeches">Sort: Leeches ↓</option>
      <option value="last_seen">Sort: Latest</option>
      <option value="first_seen">Sort: Oldest</option>
      <option value="title">Sort: Title A–Z</option>
    </select>
    <select id="lib-limit" onchange="loadLibrary()">
      <option value="100">100 rows</option>
      <option value="250">250 rows</option>
      <option value="500">500 rows</option>
    </select>
  </div>
  <div id="lib-table-wrap"><p class="loading">Loading…</p></div>
  <div class="pagination">
    <button class="pg-btn" id="pg-prev" onclick="changePage(-1)" disabled>‹ Prev</button>
    <span class="pg-info" id="pg-info"></span>
    <button class="pg-btn" id="pg-next" onclick="changePage(1)">Next ›</button>
  </div>
</div>

<!-- ── History page ─────────────────────────────────────────────────── -->
<div id="page-history" class="page">
  <div id="history-wrap"><p class="loading">Loading…</p></div>
</div>

<!-- ── Detail panel ─────────────────────────────────────────────────── -->
<div id="detail-panel">
  <span id="detail-close" onclick="closeDetail()">×</span>
  <h2 id="detail-title">—</h2>
  <div id="detail-body"></div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────
let libOffset      = 0;
let libTotal       = 0;
let searchTimer    = null;
let activeTerm     = '';   // currently selected search term ('' = all)
let activeCategory = '';   // currently selected category   ('' = all)

// ── Page switching ─────────────────────────────────────────────────────────
function showPage(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (el) el.classList.add('active');
  if (name === 'library') loadLibrary();
  if (name === 'history') loadHistory();
  return false;
}

// ── API fetch helper ───────────────────────────────────────────────────────
async function apiFetch(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`API error ${r.status}`);
  return r.json();
}

// ── Stats bar ──────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await apiFetch('/api/stats');
    document.getElementById('stat-torrents').textContent =
      s.total_torrents.toLocaleString() + ' torrents';
    document.getElementById('stat-searches').textContent =
      s.total_searches + ' runs';
    document.getElementById('stat-sites').textContent =
      s.total_sites + ' sites';
    document.getElementById('stat-last').textContent =
      'last: ' + (s.last_run ? s.last_run.slice(0,16).replace('T',' ') : '—');
  } catch(e) { console.warn('Stats failed', e); }
}

// ── Library ────────────────────────────────────────────────────────────────
function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { libOffset = 0; loadLibrary(); }, 300);
}

function changePage(dir) {
  const limit = parseInt(document.getElementById('lib-limit').value);
  libOffset   = Math.max(0, libOffset + dir * limit);
  loadLibrary();
}

// ── Category filter pills ─────────────────────────────────────────────────
async function loadCategories() {
  try {
    const cats = await apiFetch('/api/categories');
    const bar  = document.getElementById('cat-bar');
    // Clear all pills except the hardcoded "All"
    Array.from(bar.querySelectorAll('.cat-pill:not(:first-of-type)'))
         .forEach(p => p.remove());
    cats.forEach(c => {
      const pill = document.createElement('span');
      pill.className   = 'cat-pill';
      pill.textContent = c.category;
      pill.title       = `${c.term_count} term(s)`;
      pill.onclick     = () => setCategory(c.category);
      bar.appendChild(pill);
    });
  } catch(e) { console.warn('Categories load failed', e); }
}

function setCategory(cat) {
  activeCategory = cat;
  activeTerm     = '';   // clear term selection when category changes
  libOffset      = 0;

  // Update category pill states
  document.querySelectorAll('.cat-pill').forEach(p => {
    const active = cat === '' ? p.textContent === 'All'
                              : p.textContent === cat;
    p.classList.toggle('active', active);
  });

  // Reload term pills filtered by selected category then reload library
  loadTerms(cat);
  loadLibrary();
}

// ── Term filter pills ──────────────────────────────────────────────────────
async function loadTerms(categoryFilter = '') {
  try {
    const url   = categoryFilter
                  ? `/api/terms?category=${encodeURIComponent(categoryFilter)}`
                  : '/api/terms';
    const terms = await apiFetch(url);
    const bar   = document.getElementById('term-bar');

    // Rebuild — keep only the "All" pill then append fresh pills
    Array.from(bar.querySelectorAll('.term-pill:not(.all-pill)'))
         .forEach(p => p.remove());
    // Reset All pill to active since we cleared activeTerm
    bar.querySelector('.all-pill').classList.add('active');

    terms.forEach(t => {
      const pill = document.createElement('span');
      pill.className   = 'term-pill';
      pill.textContent = t.term;
      pill.title       = `[${t.category}] · Used ${t.use_count}× · last: ${t.last_used.slice(0,10)}`;
      pill.onclick     = () => setTerm(t.term);
      bar.appendChild(pill);
    });
  } catch(e) { console.warn('Terms load failed', e); }
}

function setTerm(term) {
  activeTerm = term;
  libOffset  = 0;
  document.querySelectorAll('.term-pill').forEach(p => {
    const isAll  = p.classList.contains('all-pill');
    const active = isAll ? term === '' : p.textContent === term;
    p.classList.toggle('active', active);
  });
  loadLibrary();
}

async function loadLibrary() {
  const q     = document.getElementById('lib-search').value.trim();
  const sort  = document.getElementById('lib-sort').value;
  const limit = document.getElementById('lib-limit').value;
  const url   = `/api/torrents?q=${encodeURIComponent(q)}&sort=${sort}` +
                `&limit=${limit}&offset=${libOffset}` +
                (activeTerm     ? `&term=${encodeURIComponent(activeTerm)}`         : '') +
                (activeCategory ? `&category=${encodeURIComponent(activeCategory)}` : '');

  document.getElementById('lib-table-wrap').innerHTML =
    '<p class="loading">Loading…</p>';

  try {
    const rows = await apiFetch(url);
    libTotal   = rows.length;   // approximate — server returns up to limit

    const wrap = document.getElementById('lib-table-wrap');
    if (!rows.length) {
      wrap.innerHTML = '<p class="empty">No results.</p>';
      return;
    }

    let html = `<table>
      <thead><tr>
        <th>🧲</th>
        <th onclick="setSortAndLoad('title')">Title</th>
        <th onclick="setSortAndLoad('size')">Size</th>
        <th onclick="setSortAndLoad('best_seeds')">Seeds</th>
        <th onclick="setSortAndLoad('best_leeches')">Leech</th>
        <th onclick="setSortAndLoad('last_seen')">Last seen</th>
        <th>Sites</th>
        <th>Hash</th>
      </tr></thead><tbody>`;

    rows.forEach(r => {
      const sites    = (r.sites || []).join(', ');
      const multiTag = r.sites && r.sites.length > 1
        ? `<span class="multi" title="${sites}">×${r.sites.length}</span>` : '';
      const lastSeen = r.last_seen ? r.last_seen.slice(0,10) : '—';
      html += `<tr>
        <td><a class="mag-btn" href="${escHtml(r.magnet)}" title="Open magnet link">🧲</a></td>
        <td>
          <a href="https://www.google.com/search?tbm=isch&q=${encodeURIComponent(r.title)}"
             target="_blank" title="Search Google Images for "${escHtml(r.title)}"">
            ${escHtml(r.title)}${multiTag}</a>
          <span class="detail-icon" onclick="showDetail('${r.info_hash}')"
                title="Show detail">⋯</span>
        </td>
        <td>${escHtml(r.size || '?')}</td>
        <td class="seeds">${r.best_seeds}</td>
        <td class="leeches">${r.best_leeches}</td>
        <td>${lastSeen}</td>
        <td class="sites">${escHtml(sites)}</td>
        <td class="hash">${r.info_hash.slice(0,12)}…</td>
      </tr>`;
    });

    html += '</tbody></table>';
    wrap.innerHTML = html;

    // Pagination controls
    const limit_n = parseInt(limit);
    document.getElementById('pg-prev').disabled = libOffset === 0;
    document.getElementById('pg-next').disabled = rows.length < limit_n;
    document.getElementById('pg-info').textContent =
      `Showing ${libOffset + 1}–${libOffset + rows.length}`;

  } catch(e) {
    document.getElementById('lib-table-wrap').innerHTML =
      `<p class="empty">Error: ${e.message}</p>`;
  }
}

function setSortAndLoad(col) {
  document.getElementById('lib-sort').value = col;
  libOffset = 0;
  loadLibrary();
}

// ── History ────────────────────────────────────────────────────────────────
async function loadHistory() {
  document.getElementById('history-wrap').innerHTML =
    '<p class="loading">Loading…</p>';
  try {
    const runs = await apiFetch('/api/searches');
    if (!runs.length) {
      document.getElementById('history-wrap').innerHTML =
        '<p class="empty">No scrape runs yet.</p>';
      return;
    }

    let html = '';
    runs.forEach(r => {
      const dt    = r.run_at.slice(0,16).replace('T', ' ');
      const mode  = r.js_mode ? ' · JS mode' : '';
      const dupes = r.dupes_removed ? ` · ${r.dupes_removed} dupes removed` : '';
      html += `<div class="run-card">
        <h3>${dt}${mode}</h3>
        <div class="run-meta">
          Terms: ${escHtml(r.terms)} &nbsp;·&nbsp;
          ${r.total_found} unique torrent(s)${dupes}
        </div>
        <span class="run-link" onclick="showRunDetail(${r.id})">
          View results →
        </span>
      </div>`;
    });
    document.getElementById('history-wrap').innerHTML = html;
  } catch(e) {
    document.getElementById('history-wrap').innerHTML =
      `<p class="empty">Error: ${e.message}</p>`;
  }
}

async function showRunDetail(searchId) {
  openDetailPanel('Loading run…', '<p class="loading">Loading…</p>');
  try {
    const s = await apiFetch(`/api/searches/${searchId}`);
    const dt = s.run_at.slice(0,16).replace('T',' ');
    let html = `<div class="detail-row">
      <div class="detail-label">Run</div>${dt}</div>
      <div class="detail-row"><div class="detail-label">Terms</div>
        ${escHtml(s.terms)}</div>
      <div class="detail-row"><div class="detail-label">Results</div>
        ${s.total_found} unique</div>
      <hr style="border-color:#333;margin:1rem 0">`;

    s.results.forEach(r => {
      const sites = (r.sites || []).join(', ');
      html += `<div class="appear-item">
        <a class="mag-btn" href="${escHtml(r.magnet)}" title="Open magnet link">🧲</a>
        &nbsp;<strong>${escHtml(r.title)}</strong><br>
        <span style="color:#888;font-size:0.75rem">
          ${escHtml(r.size||'?')} · <span class="seeds">${r.best_seeds}S</span>
          · ${escHtml(sites)} · ${escHtml(r.search_term)}
        </span>
      </div>`;
    });

    openDetailPanel(`Run: ${dt}`, html);
  } catch(e) {
    openDetailPanel('Error', `<p>${e.message}</p>`);
  }
}

// ── Torrent detail panel ───────────────────────────────────────────────────
async function showDetail(hash) {
  openDetailPanel('Loading…', '<p class="loading">Loading…</p>');
  try {
    const t = await apiFetch(`/api/torrents/${hash}`);
    const sites = (t.sites || []).join(', ');
    let html = `
      <div class="detail-row">
        <div class="detail-label">Hash</div>
        <code class="hash">${t.info_hash}</code>
      </div>
      <div class="detail-row">
        <div class="detail-label">Size</div>${escHtml(t.size||'?')}</div>
      <div class="detail-row">
        <div class="detail-label">Seeds / Leeches</div>
        <span class="seeds">${t.best_seeds}</span> /
        <span class="leeches">${t.best_leeches}</span>
      </div>
      <div class="detail-row">
        <div class="detail-label">Sites</div>${escHtml(sites)}</div>
      <div class="detail-row">
        <div class="detail-label">First seen</div>
        ${t.first_seen.slice(0,16).replace('T',' ')}</div>
      <div class="detail-row">
        <div class="detail-label">Last seen</div>
        ${t.last_seen.slice(0,16).replace('T',' ')}</div>
      <div style="margin:1rem 0">
        <a class="mag-btn" style="font-size:1.4rem" href="${escHtml(t.magnet)}"
           title="Open magnet link">🧲</a>
        &nbsp;Open in Transmission
      </div>
      <hr style="border-color:#333;margin:1rem 0">
      <div class="detail-label" style="margin-bottom:0.5rem">
        Seen in ${(t.appearances||[]).length} run(s)
      </div>`;

    (t.appearances||[]).forEach(a => {
      const dt = a.run_at.slice(0,16).replace('T',' ');
      html += `<div class="appear-item">
        ${dt} · <span class="seeds">${a.seeds}S</span>
        · ${escHtml(a.search_term)}
      </div>`;
    });

    openDetailPanel(t.title, html);
  } catch(e) {
    openDetailPanel('Error', `<p>${e.message}</p>`);
  }
}

function openDetailPanel(title, bodyHtml) {
  document.getElementById('detail-title').textContent = title;
  document.getElementById('detail-body').innerHTML    = bodyHtml;
  document.getElementById('detail-panel').classList.add('open');
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('open');
}

// ── Magnet launcher ────────────────────────────────────────────────────────
function fireMagnet(uri) {
  const a = document.createElement('a');
  a.href  = uri;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ── Utility ────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str||'')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Purge ─────────────────────────────────────────────────────────────────
function openPurge() {
  document.getElementById('purge-overlay').classList.add('open');
}

function closePurge() {
  document.getElementById('purge-overlay').classList.remove('open');
}

async function executePurge() {
  const btn = document.getElementById('purge-confirm-btn');
  btn.disabled    = true;
  btn.textContent = 'Purging…';

  try {
    const r = await fetch('/api/db/purge', { method: 'DELETE' });
    if (!r.ok) throw new Error(`Server error ${r.status}`);

    closePurge();

    // Reset all UI state
    activeTerm     = '';
    activeCategory = '';
    libOffset      = 0;

    // Clear term pills back to just "All"
    const tbar = document.getElementById('term-bar');
    tbar.innerHTML = '<span class="term-label">Term:</span>' +
      '<span class="term-pill all-pill active" onclick="setTerm(\'\')">All</span>';

    // Clear category pills back to just "All"
    const cbar = document.getElementById('cat-bar');
    cbar.innerHTML = '<span class="filter-label">Category:</span>' +
      '<span class="cat-pill active" onclick="setCategory(\'\')">All</span>';

    // Reload everything
    await loadStats();
    await loadLibrary();

  } catch(e) {
    alert('Purge failed: ' + e.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Delete Everything';
  }
}

// Close overlay on background click
document.getElementById('purge-overlay').addEventListener('click', function(e) {
  if (e.target === this) closePurge();
});

// ── Boot ───────────────────────────────────────────────────────────────────
loadStats();
loadCategories();
loadLibrary();
</script>
</body>
</html>
"""

# ══════════════════════════════════════════════════════════════════════════
# HTTP request handler
# ══════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):

    db_path: str = DEFAULT_DB

    def log_message(self, fmt, *args):
        # Suppress default per-request logging; uncomment to re-enable
        pass

    def _conn(self):
        return get_db(self.db_path)

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self):
        self._send_json({'error': 'Not found'}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        try:
            if path == '/api/db/purge':
                conn = self._conn()
                result = api_purge(conn)
                conn.close()
                print('[INFO] Database purged via web UI', file=sys.stderr)
                self._send_json(result)
            else:
                self._send_404()
        except Exception as e:
            self._send_json({'error': str(e)}, 500)
            print(f'[ERROR] purge failed: {e}', file=sys.stderr)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        qs     = parse_qs(parsed.query)

        def qs1(k, default=''):
            return qs.get(k, [default])[0]

        try:
            conn = self._conn()

            # ── Pages ─────────────────────────────────────────────────────
            if path in ('', '/'):
                self._send_html(PAGE_HTML)

            # ── API: stats ────────────────────────────────────────────────
            elif path == '/api/stats':
                self._send_json(api_stats(conn))

            # ── API: categories ───────────────────────────────────────────
            elif path == '/api/categories':
                self._send_json(api_categories(conn))

            # ── API: search terms list (optional ?category= filter) ───────
            elif path == '/api/terms':
                self._send_json(api_terms(conn, category=qs1('category')))

            # ── API: torrents list ────────────────────────────────────────
            elif path == '/api/torrents':
                rows = api_torrents(
                    conn,
                    q        = qs1('q'),
                    sort     = qs1('sort', 'best_seeds'),
                    order    = qs1('order', 'desc'),
                    limit    = int(qs1('limit', '200')),
                    offset   = int(qs1('offset', '0')),
                    term     = qs1('term'),
                    category = qs1('category'),
                )
                self._send_json(rows)

            # ── API: single torrent ───────────────────────────────────────
            elif path.startswith('/api/torrents/'):
                ih = unquote(path[len('/api/torrents/'):])
                t  = api_torrent(conn, ih)
                if t:
                    self._send_json(t)
                else:
                    self._send_404()

            # ── API: search history ───────────────────────────────────────
            elif path == '/api/searches':
                self._send_json(api_searches(conn))

            # ── API: single search run ────────────────────────────────────
            elif path.startswith('/api/searches/'):
                sid = int(path[len('/api/searches/'):])
                s   = api_search(conn, sid)
                if s:
                    self._send_json(s)
                else:
                    self._send_404()

            else:
                self._send_404()

            conn.close()

        except Exception as e:
            self._send_json({'error': str(e)}, 500)
            print(f'[ERROR] {e}', file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description='Magnet Library web interface')
    p.add_argument('--port', type=int, default=DEFAULT_PORT,
                   help=f'Port to listen on (default: {DEFAULT_PORT})')
    p.add_argument('--db', default=DEFAULT_DB,
                   help=f'SQLite database path (default: {DEFAULT_DB})')
    p.add_argument('--no-browser', action='store_true',
                   help='Do not open browser on startup')
    return p.parse_args()


def main():
    args = parse_args()

    if not Path(args.db).exists():
        print(f'[WARN] Database not found: {args.db}')
        print('       Run magnetlookup.py at least once to create it.')

    Handler.db_path = args.db
    server = HTTPServer(('127.0.0.1', args.port), Handler)

    url = f'http://localhost:{args.port}'
    print(f'🧲 Magnet Library running at {url}')
    print(f'   Database: {args.db}')
    print('   Press Ctrl+C to stop.\n')

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
