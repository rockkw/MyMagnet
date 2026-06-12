#!/bin/bash
# setup_git.sh — initialise local git repo for magnetlookup
# Run once from ~/Documents/Development/
# Usage: bash setup_git.sh

set -e
cd "$(dirname "$0")"

echo "→ Initialising git repo..."
git init

echo "→ Setting identity..."
git config user.name  "mach50"
git config user.email "mach50@local"

echo "→ Staging files..."
git add magnetlookup.py
git add webserver.py
git add requirements.txt
git add .gitignore
git add com.mach50.magnetlookup.plist
git add urls.txt 2>/dev/null || true   # may not exist yet

echo "→ Initial commit..."
git commit -m "feat: magnetlookup unified scraper + webserver + launchd"

echo ""
echo "✅ Git repo ready. Useful commands:"
echo ""
echo "  git status                          # what changed"
echo "  git diff magnetlookup.py            # see changes"
echo "  git add magnetlookup.py && git commit -m 'fix: ...'  # save version"
echo "  git log --oneline                   # history"
echo "  git checkout -- magnetlookup.py     # discard uncommitted changes"
echo "  git checkout <hash> -- magnetlookup.py  # restore old version"
echo ""
echo "  # Work on a risky change safely:"
echo "  git checkout -b fix/tpb-selector"
echo "  git checkout main && git merge fix/tpb-selector"
echo ""
