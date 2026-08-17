#!/usr/bin/env python3
"""
sync_backlog.py — Import backlog items typed on your phone into Backlog.md.

Reads a plain-text inbox file synced via Google Drive (edit it from the
Drive app, or any editor that syncs into your Drive folder, on your
phone) and creates one Backlog.md task per line via the `backlog` CLI.
Processed lines are cleared from the inbox afterward, same pattern as
watch_terms.py clearing search_term.txt.

Inbox file format (one task per line):

    Fix the leaky faucet
    Buy milk | remember oat milk this time #errand
    Write blog post about Q3 roadmap #writing #high

  - Text before " | " is the title; text after it is the description.
  - Trailing #tags become Backlog.md labels.
  - Blank lines and lines starting with # are ignored.

Usage:
    python3 sync_backlog.py              # uses default inbox path
    python3 sync_backlog.py --dry-run    # show what would be created
    python3 sync_backlog.py --inbox PATH
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_INBOX = os.path.expanduser(
    '~/Library/CloudStorage/GoogleDrive-rock.k.whitney@gmail.com/'
    'My Drive/Downloads/backlog_inbox.txt')

TAG_RE = re.compile(r'#(\S+)')


def parse_line(line: str):
    """Split a line into (title, description, labels)."""
    labels = TAG_RE.findall(line)
    text = TAG_RE.sub('', line).strip()

    if ' | ' in text:
        title, description = text.split(' | ', 1)
        title, description = title.strip(), description.strip()
    else:
        title, description = text, ''

    return title, description, labels


def read_inbox(path: str) -> list[str]:
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return []
    return [l for l in lines if l.strip() and not l.strip().startswith('#')]


def create_task(title: str, description: str, labels: list[str], dry_run: bool) -> bool:
    cmd = ['backlog', 'task', 'create', title]
    if description:
        cmd += ['-d', description]
    if labels:
        cmd += ['-l', ','.join(labels)]

    print(f'[sync_backlog] {"Would create" if dry_run else "Creating"}: {title}')
    if dry_run:
        return True

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'[sync_backlog] Failed: {result.stderr.strip()}', file=sys.stderr)
        return False
    print(result.stdout.strip())
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--inbox', default=DEFAULT_INBOX, metavar='PATH',
        help=f'Inbox file to read (default: {DEFAULT_INBOX})')
    p.add_argument('--dry-run', action='store_true',
        help='Print what would be created without calling backlog or clearing the inbox')
    args = p.parse_args()

    raw_lines = read_inbox(args.inbox)
    if not raw_lines:
        print(f'[sync_backlog] No new items in {args.inbox}')
        return

    print(f'[sync_backlog] {len(raw_lines)} item(s) found in {args.inbox}')

    all_ok = True
    for line in raw_lines:
        title, description, labels = parse_line(line)
        if not title:
            continue
        ok = create_task(title, description, labels, args.dry_run)
        all_ok = all_ok and ok

    if args.dry_run:
        print('[sync_backlog] DRY RUN — inbox left untouched.')
        return

    if all_ok:
        Path(args.inbox).write_text('', encoding='utf-8')
        print(f'[sync_backlog] Cleared {args.inbox}')
    else:
        print('[sync_backlog] Some items failed — inbox left as-is for retry.',
              file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
