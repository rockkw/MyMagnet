#!/usr/bin/env bash
# backup_to_s3.sh — sync the SQLite library, logs, and HTML/CSV results to S3.
#
# Run via magnetlookup-backup.service/.timer (systemd), or manually. Reads
# its config from the same EnvironmentFile as the app (/etc/magnetlookup/env)
# when run under systemd; falls back to the current environment otherwise.
set -euo pipefail

if [ -z "${MAGNET_S3_BUCKET:-}" ]; then
  echo "[backup] MAGNET_S3_BUCKET not set — skipping S3 sync."
  exit 0
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "[backup] aws CLI not found — install it (see deploy/setup.sh) or skip this timer." >&2
  exit 1
fi

DB_FILE="${MAGNET_DB_FILE:-/opt/magnetlookup/data/magnet_library.db}"
LOG_DIR="${MAGNET_LOG_DIR:-/opt/magnetlookup/data/logs}"
OUTPUT_DIR="${MAGNET_OUTPUT_DIR:-/opt/magnetlookup/data/magnet_results}"
BUCKET="s3://${MAGNET_S3_BUCKET}/magnetlookup"

echo "[backup] syncing to ${BUCKET} ..."

# SQLite: copy to a temp file first rather than syncing the live file, to
# avoid grabbing it mid-write if a scrape is somehow still running.
if [ -f "$DB_FILE" ]; then
  TMP_DB="$(mktemp)"
  sqlite3 "$DB_FILE" ".backup '$TMP_DB'"
  aws s3 cp "$TMP_DB" "${BUCKET}/db/magnet_library.db"
  rm -f "$TMP_DB"
fi

[ -d "$LOG_DIR" ]    && aws s3 sync "$LOG_DIR"    "${BUCKET}/logs/"    --only-show-errors
[ -d "$OUTPUT_DIR" ] && aws s3 sync "$OUTPUT_DIR" "${BUCKET}/results/" --only-show-errors

echo "[backup] done."
