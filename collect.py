#!/usr/bin/env python3
"""
collect.py — Search and Collect

Recursively searches a directory and all subdirectories for files and folders
whose names contain a search term, then moves all matches into a single
collection folder created at the parent level.

Usage:
    python3 collect.py <search_root> <term>
    python3 collect.py <search_root> <term> [options]

Examples:
    # Search ~/Downloads for anything containing "polymer", collect into ~/polymer/
    python3 collect.py ~/Downloads polymer

    # Case-sensitive search
    python3 collect.py ~/Documents Polymer --case-sensitive

    # Dry run — show what would move without actually moving anything
    python3 collect.py ~/Downloads polymer --dry-run

    # Search file contents as well as names
    python3 collect.py ~/Documents polymer --search-contents

    # Set a custom output folder name (default is the search term)
    python3 collect.py ~/Downloads polymer --output-name "Polymer Materials"

    # Search for multiple terms (any match is collected)
    python3 collect.py ~/Downloads polymer rarbg databricks

Design:
    - The collection folder is created at the PARENT of search_root, not inside
      it, so it is never picked up as a match during the search.
    - Filename conflicts are resolved by appending _1, _2, etc.
    - Hidden files/folders (dot-prefixed) are skipped by default.
    - The collection folder itself and its contents are never searched.
    - Only stdlib is used — no pip installs needed.
"""

import sys
import os
import re
import shutil
import argparse
from pathlib import Path
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════
# Match logic
# ══════════════════════════════════════════════════════════════════════════

def name_matches(name: str, terms: list[str], case_sensitive: bool) -> bool:
    """Return True if any term appears in the file/folder name."""
    check_name = name if case_sensitive else name.lower()
    for term in terms:
        check_term = term if case_sensitive else term.lower()
        if check_term in check_name:
            return True
    return False


def content_matches(path: Path, terms: list[str],
                    case_sensitive: bool) -> bool:
    """
    Return True if any term appears in the file's text content.
    Only attempts text files — silently skips binary files.
    Limits read to first 1 MB to avoid huge files stalling the search.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(1_048_576)   # 1 MB cap
        check_content = content if case_sensitive else content.lower()
        for term in terms:
            check_term = term if case_sensitive else term.lower()
            if check_term in check_content:
                return True
    except (OSError, PermissionError):
        pass
    return False

# ══════════════════════════════════════════════════════════════════════════
# Conflict-safe destination path
# ══════════════════════════════════════════════════════════════════════════

def safe_destination(dest_dir: Path, name: str) -> Path:
    """
    Return a destination path that does not already exist.
    If dest_dir/name exists, tries dest_dir/name_1, _2, etc.
    """
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate

    stem   = Path(name).stem
    suffix = Path(name).suffix   # empty string for folders
    counter = 1
    while True:
        new_name  = f"{stem}_{counter}{suffix}"
        candidate = dest_dir / new_name
        if not candidate.exists():
            return candidate
        counter += 1

# ══════════════════════════════════════════════════════════════════════════
# Recursive search
# ══════════════════════════════════════════════════════════════════════════

def find_matches(root: Path, terms: list[str], case_sensitive: bool,
                 search_contents: bool, include_hidden: bool,
                 collect_dir: Path) -> list[Path]:
    """
    Walk root recursively and return paths of all matching files and folders.
    - Skips the collect_dir itself and anything inside it.
    - When a matching folder is found, its contents are not searched further
      (the whole folder will be moved as a unit).
    - Processes deepest paths first so that a folder match does not leave
      orphaned children behind.
    """
    matches: list[Path] = []
    matched_dirs: set[Path] = set()   # track folders already matched

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        current_dir = Path(dirpath)

        # Never descend into the collection folder
        if current_dir == collect_dir or \
                any(current_dir.is_relative_to(d) for d in matched_dirs
                    if collect_dir not in (current_dir,)):
            dirnames.clear()
            continue

        # Skip hidden directories unless requested
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]

        # Check subdirectory names for matches (in-place to skip descent
        # into matching dirs — they'll be moved whole)
        non_matching_dirs = []
        for d in dirnames:
            dir_path = current_dir / d
            if dir_path == collect_dir:
                continue
            if name_matches(d, terms, case_sensitive):
                matches.append(dir_path)
                matched_dirs.add(dir_path)
                # Don't descend — the whole dir is moving
            else:
                non_matching_dirs.append(d)
        dirnames[:] = non_matching_dirs

        # Check file names (and optionally contents)
        for filename in filenames:
            if not include_hidden and filename.startswith('.'):
                continue
            file_path = current_dir / filename
            matched = name_matches(filename, terms, case_sensitive)
            if not matched and search_contents:
                matched = content_matches(file_path, terms, case_sensitive)
            if matched:
                matches.append(file_path)

    return matches

# ══════════════════════════════════════════════════════════════════════════
# Move with conflict resolution
# ══════════════════════════════════════════════════════════════════════════

def move_item(src: Path, dest_dir: Path, dry_run: bool) -> tuple[str, str]:
    """
    Move src into dest_dir, resolving name conflicts.
    Returns (status, message) for reporting.
    """
    dest = safe_destination(dest_dir, src.name)
    renamed = dest.name != src.name

    if dry_run:
        tag = "[DRY RUN] " if not renamed else f"[DRY RUN → {dest.name}] "
        return 'dry', f"{tag}{src} → {dest}"

    try:
        shutil.move(str(src), str(dest))
        tag = f"[MOVED → {dest.name}]" if renamed else "[MOVED]"
        return 'ok', f"{tag} {src.name}"
    except PermissionError as e:
        return 'err', f"[PERMISSION ERROR] {src}: {e}"
    except shutil.Error as e:
        return 'err', f"[ERROR] {src}: {e}"

# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description='Search a directory tree for a term and collect matches '
                    'into a single folder at the parent level.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        'search_root',
        help='Directory to search recursively'
    )
    p.add_argument(
        'terms', nargs='+',
        help='One or more search terms (any match is collected)'
    )
    p.add_argument(
        '--output-name', '-o', metavar='NAME',
        help='Name for the collection folder (default: first search term)'
    )
    p.add_argument(
        '--case-sensitive', '-c', action='store_true',
        help='Match case-sensitively (default: case-insensitive)'
    )
    p.add_argument(
        '--search-contents', '-C', action='store_true',
        help='Also search inside text file contents (slower)'
    )
    p.add_argument(
        '--include-hidden', '-H', action='store_true',
        help='Include hidden files and folders (dot-prefixed)'
    )
    p.add_argument(
        '--dry-run', '-n', action='store_true',
        help='Show what would be moved without actually moving anything'
    )
    p.add_argument(
        '--verbose', '-v', action='store_true',
        help='Print every match as it is found'
    )
    return p.parse_args()

# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    search_root = Path(args.search_root).expanduser().resolve()
    if not search_root.is_dir():
        print(f"[ERROR] Not a directory: {search_root}")
        sys.exit(1)

    terms       = args.terms
    output_name = args.output_name or terms[0]
    parent_dir  = search_root.parent

    # Collection folder lives BESIDE the search root, not inside it
    collect_dir = parent_dir / output_name

    # ── Summary header ────────────────────────────────────────────────────
    print(f"\n{'━'*60}")
    print(f"  Search root : {search_root}")
    print(f"  Term(s)     : {', '.join(terms)}")
    print(f"  Collect to  : {collect_dir}")
    print(f"  Case        : {'sensitive' if args.case_sensitive else 'insensitive'}")
    print(f"  Contents    : {'yes' if args.search_contents else 'no'}")
    print(f"  Dry run     : {'YES — nothing will be moved' if args.dry_run else 'no'}")
    print(f"{'━'*60}\n")

    # ── Find matches ──────────────────────────────────────────────────────
    print(f"[1/3] Searching {search_root} …")
    matches = find_matches(
        root            = search_root,
        terms           = terms,
        case_sensitive  = args.case_sensitive,
        search_contents = args.search_contents,
        include_hidden  = args.include_hidden,
        collect_dir     = collect_dir,
    )

    if not matches:
        print(f"\n  No matches found for: {', '.join(terms)}")
        sys.exit(0)

    print(f"  Found {len(matches)} match(es)\n")
    if args.verbose or args.dry_run:
        for m in matches:
            kind = "DIR " if m.is_dir() else "FILE"
            print(f"    [{kind}] {m}")
        print()

    # ── Confirm before moving ─────────────────────────────────────────────
    if not args.dry_run:
        answer = input(
            f"[2/3] Move {len(matches)} item(s) to:\n"
            f"      {collect_dir}\n"
            f"      Proceed? [y/N] "
        ).strip().lower()
        if answer not in ('y', 'yes'):
            print("  Aborted.")
            sys.exit(0)

        collect_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n      Created: {collect_dir}\n")
    else:
        print(f"[2/3] Dry run — would create: {collect_dir}\n")

    # ── Move items ────────────────────────────────────────────────────────
    print(f"[3/3] {'Simulating moves' if args.dry_run else 'Moving'} …\n")

    counts = {'ok': 0, 'dry': 0, 'err': 0}
    for item in matches:
        # Skip if item was inside a matched parent dir that's already moved
        if not item.exists() and not args.dry_run:
            continue
        status, msg = move_item(item, collect_dir, args.dry_run)
        counts[status] += 1
        print(f"  {msg}")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'━'*60}")
    if args.dry_run:
        print(f"  Dry run complete — {counts['dry']} item(s) would be moved")
        print(f"  Run without --dry-run to perform the move")
    else:
        print(f"  Done — {counts['ok']} moved, {counts['err']} error(s)")
        if counts['ok']:
            print(f"  Collection folder: {collect_dir}")
    print(f"{'━'*60}\n")

    if counts['err']:
        sys.exit(1)


if __name__ == '__main__':
    main()
