#!/usr/bin/env python3
"""
watch_terms.py — Monitor search_term.txt and launch magnetlookup.py on changes.

Watches the configured search_term.txt file using os.stat polling (no
third-party dependencies).  When the file's mtime or size changes AND the
new content differs from what was last seen, it launches magnetlookup.py
in a subprocess so the full scrape runs against the updated terms.

Usage:
    python3 watch_terms.py              # uses paths from config.ini
    python3 watch_terms.py --interval 5 # poll every 5 seconds (default: 3)
    python3 watch_terms.py --no-js      # pass --no-js to magnetlookup.py
    python3 watch_terms.py --dry-run    # print what would run, don't launch
"""

import os
import sys
import time
import argparse
import subprocess
import configparser
import re
import json
from pathlib import Path

# ── Read config (same logic as magnetlookup.py / webserver.py) ───────────────

def _expand(value: str) -> str:
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

_cfg = configparser.RawConfigParser()
_cfg_path = Path(__file__).with_name('config.ini')
_cfg.read(_cfg_path)

def _get(section, key, fallback):
    raw = _cfg.get(section, key, fallback=None)
    return _expand(raw) if raw is not None else fallback

DEFAULT_SEARCH_FILE = _get('paths', 'search_file',
    os.path.expanduser(
        '~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/search_term.txt'))
DEFAULT_SEARCH_FILE_2 = _get('paths', 'search_file_2', '')
DEFAULT_DB_FILE = _get('paths', 'db_file',
    os.path.expanduser('~/Documents/Development/magnet_library.db'))
DEFAULT_LOG_DIR = _get('paths', 'log_dir',
    os.path.expanduser('~/Documents/Development/logs'))

MAGNETLOOKUP = Path(__file__).with_name('magnetlookup.py')

# ── Watcher ───────────────────────────────────────────────────────────────────

def _read_terms_s3(path: str) -> str:
    try:
        result = subprocess.run(
            ['aws', 's3', 'cp', path, '-'],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout if result.returncode == 0 else ''
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ''

def _stat_key_s3(path: str):
    """Return the object's ETag (changes whenever content changes), or None
    if it can't be reached — via `aws s3api head-object`, no local mtime/size
    available for a remote object."""
    try:
        result = subprocess.run(
            ['aws', 's3api', 'head-object', '--bucket', path.split('/')[2],
             '--key', '/'.join(path.split('/')[3:])],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout).get('ETag')
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None

def read_terms(path: str) -> str:
    """Return raw file content, or empty string if unreadable. Supports
    both local paths and s3://bucket/key URIs."""
    if path.startswith('s3://'):
        return _read_terms_s3(path)
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''

def stat_key(path: str):
    """Return a cheap change-detection key (mtime+size locally, ETag for
    s3://), or None if the file/object is absent/unreachable."""
    if path.startswith('s3://'):
        return _stat_key_s3(path)
    try:
        s = os.stat(path)
        return (s.st_mtime, s.st_size)
    except OSError:
        return None

def launch(search_file: str, search_file_2: str, db: str, extra_args: list[str], dry_run: bool):
    cmd = [
        sys.executable, str(MAGNETLOOKUP),
        '--search-file', search_file,
        '--db', db,
        '--no-browser',
        '--cron',
    ]
    if search_file_2:
        cmd += ['--search-file-2', search_file_2]
    cmd += extra_args

    print(f'\n[watch_terms] Change detected — launching scrape')
    print(f'[watch_terms] Command: {" ".join(cmd)}\n')

    if dry_run:
        print('[watch_terms] DRY RUN — not executing.')
        return

    try:
        proc = subprocess.Popen(cmd)
        print(f'[watch_terms] PID {proc.pid} started. Waiting for completion…')
        proc.wait()
        rc = proc.returncode
        if rc == 0:
            print(f'[watch_terms] Scrape finished successfully.')
        else:
            print(f'[watch_terms] Scrape exited with code {rc}.')
    except Exception as e:
        print(f'[watch_terms] Failed to launch: {e}', file=sys.stderr)

def watch(search_file: str, search_file_2: str, db: str, interval: float,
          extra_args: list[str], dry_run: bool):
    print(f'[watch_terms] Watching: {search_file}')
    if search_file_2:
        print(f'[watch_terms] Watching: {search_file_2}')
    print(f'[watch_terms] Poll interval: {interval}s  |  Ctrl-C to stop\n')

    last_stat    = stat_key(search_file)
    last_content = read_terms(search_file)
    last_stat_2    = stat_key(search_file_2) if search_file_2 else None
    last_content_2 = read_terms(search_file_2) if search_file_2 else ''

    if last_stat is None:
        print(f'[watch_terms] File not found yet — will trigger when it appears.')

    while True:
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print('\n[watch_terms] Stopped.')
            break

        cur_stat   = stat_key(search_file)
        cur_stat_2 = stat_key(search_file_2) if search_file_2 else None

        # No change in mtime/size on either file — skip the reads
        if cur_stat == last_stat and cur_stat_2 == last_stat_2:
            continue

        cur_content   = read_terms(search_file) if cur_stat != last_stat else last_content
        cur_content_2 = (read_terms(search_file_2)
                          if search_file_2 and cur_stat_2 != last_stat_2
                          else last_content_2)

        if cur_content != last_content or cur_content_2 != last_content_2:
            last_stat, last_content     = cur_stat, cur_content
            last_stat_2, last_content_2 = cur_stat_2, cur_content_2
            launch(search_file, search_file_2, db, extra_args, dry_run)
        else:
            # mtime changed but content identical (e.g. iCloud/Drive sync touch)
            last_stat, last_stat_2 = cur_stat, cur_stat_2

def main():
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--search-file', default=DEFAULT_SEARCH_FILE, metavar='PATH',
        help=f'File to watch (default: {DEFAULT_SEARCH_FILE})')
    p.add_argument('--search-file-2', default=DEFAULT_SEARCH_FILE_2, metavar='PATH',
        help='2nd file to watch, e.g. Google Drive (default: '
             f'{DEFAULT_SEARCH_FILE_2 or "(none)"})')
    p.add_argument('--db', default=DEFAULT_DB_FILE, metavar='PATH',
        help=f'Database path passed to magnetlookup.py (default: {DEFAULT_DB_FILE})')
    p.add_argument('--interval', type=float, default=3.0, metavar='SECS',
        help='Poll interval in seconds (default: 3)')
    p.add_argument('--no-js', action='store_true',
        help='Pass --no-js to magnetlookup.py (faster, may miss JS-rendered sites)')
    p.add_argument('--dry-run', action='store_true',
        help='Print the command that would run but do not execute it')
    args = p.parse_args()

    extra = ['--no-js'] if args.no_js else []
    watch(args.search_file, args.search_file_2, args.db, args.interval, extra, args.dry_run)

if __name__ == '__main__':
    main()
