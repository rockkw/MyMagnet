#!/usr/bin/env bash
# setup.sh — bootstrap a fresh Ubuntu 24.04 EC2/Lightsail instance to run
# magnetlookup (scraper + dashboard) as it would under launchd on a Mac,
# minus Chrome/Selenium since neither archive.org (JSON API) nor
# linuxtracker.org (plain HTML) need JS rendering.
#
# Run as: sudo ./deploy/setup.sh   (from inside the repo checkout)
#
# What it does:
#   1. Installs OS packages (Python, sqlite3, nginx, certbot, awscli)
#   2. Creates a dedicated, unprivileged 'magnetlookup' system user
#   3. Copies the app into /opt/magnetlookup/app and creates a venv
#   4. Installs the systemd units (daily scrape timer, web dashboard
#      service, S3 backup timer) and the nginx reverse-proxy config
#   5. Leaves you a checklist of manual steps (env file, search terms,
#      DNS/TLS) that are yours to fill in — nothing here touches AWS
#      resources directly, this only configures the instance itself.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo: sudo ./deploy/setup.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/magnetlookup/app
DATA_DIR=/opt/magnetlookup/data
VENV_DIR=/opt/magnetlookup/venv
SVC_USER=magnetlookup

echo "==> Installing OS packages"
apt-get update -y
apt-get install -y \
  python3 python3-venv python3-pip \
  libxml2-dev libxslt1-dev \
  sqlite3 \
  nginx certbot python3-certbot-nginx \
  awscli \
  rsync

echo "==> Creating service user '${SVC_USER}'"
id -u "$SVC_USER" &>/dev/null || useradd --system --create-home --home-dir /opt/magnetlookup --shell /usr/sbin/nologin "$SVC_USER"

echo "==> Laying out ${APP_DIR} and ${DATA_DIR}"
mkdir -p "$APP_DIR" "$DATA_DIR"/{logs,magnet_results}
rsync -a --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='backlog' \
  "$REPO_DIR"/ "$APP_DIR"/

echo "==> Creating Python venv and installing dependencies"
python3 -m venv "$VENV_DIR"
"$VENV_DIR"/bin/pip install --upgrade pip
# Core deps only — Selenium/webdriver-manager are skipped since the systemd
# unit runs with --no-js. Add them (`pip install selenium webdriver-manager`
# + a Chromium install) only if you point urls.txt at a JS-rendered site.
"$VENV_DIR"/bin/pip install requests==2.31.0 beautifulsoup4==4.12.3 lxml==6.1.1

echo "==> Seeding config.ini env-var overrides"
mkdir -p /etc/magnetlookup
if [ ! -f /etc/magnetlookup/env ]; then
  cp "$APP_DIR/deploy/magnetlookup.env.example" /etc/magnetlookup/env
  echo "    Wrote /etc/magnetlookup/env from the example — edit it before starting services."
fi
chmod 640 /etc/magnetlookup/env
chown root:"$SVC_USER" /etc/magnetlookup/env

if [ ! -f "$DATA_DIR/urls.txt" ]; then
  cp "$APP_DIR/urls.txt.sample" "$DATA_DIR/urls.txt"
  echo "    Seeded $DATA_DIR/urls.txt from urls.txt.sample — review/edit it."
fi
if [ ! -f "$DATA_DIR/search_term.txt" ]; then
  cat > "$DATA_DIR/search_term.txt" <<'EOF'
[Software]
Ubuntu 24.04

[Books]
Polymer Materials
EOF
  echo "    Seeded a starter $DATA_DIR/search_term.txt — edit it with your real terms."
fi

chown -R "$SVC_USER":"$SVC_USER" /opt/magnetlookup

echo "==> Installing systemd units"
cp "$APP_DIR"/deploy/magnetlookup.service        /etc/systemd/system/
cp "$APP_DIR"/deploy/magnetlookup.timer          /etc/systemd/system/
cp "$APP_DIR"/deploy/magnetlookup-web.service    /etc/systemd/system/
cp "$APP_DIR"/deploy/magnetlookup-backup.service /etc/systemd/system/
cp "$APP_DIR"/deploy/magnetlookup-backup.timer   /etc/systemd/system/
chmod +x "$APP_DIR"/deploy/backup_to_s3.sh
systemctl daemon-reload

echo "==> Installing nginx site"
cp "$APP_DIR"/deploy/nginx-magnetlookup.conf /etc/nginx/sites-available/magnetlookup
ln -sf /etc/nginx/sites-available/magnetlookup /etc/nginx/sites-enabled/magnetlookup
rm -f /etc/nginx/sites-enabled/default
nginx -t

echo "==> Enabling services"
systemctl enable --now magnetlookup-web.service
systemctl enable --now magnetlookup.timer
systemctl enable --now magnetlookup-backup.timer
systemctl reload nginx

cat <<'EOF'

==> Setup complete. Remaining manual steps:

  1. Edit /etc/magnetlookup/env — at minimum set MAGNET_S3_BUCKET if you
     want the daily backup timer to actually sync anywhere.
  2. Edit /opt/magnetlookup/data/search_term.txt and
     /opt/magnetlookup/data/urls.txt with your real search terms/sources.
  3. Point a DNS name at this instance's public IP, edit server_name in
     /etc/nginx/sites-available/magnetlookup, then run:
       sudo certbot --nginx -d magnet.yourdomain.com
  4. Restrict the EC2/Lightsail security group to allow inbound 80/443
     (and 22 from your IP only) — port 8080 should NOT be open externally,
     nginx is the only public entry point.
  5. Try a manual scrape run before waiting for the timer:
       sudo systemctl start magnetlookup.service
       sudo journalctl -u magnetlookup.service -f
  6. Check the dashboard: sudo systemctl status magnetlookup-web.service,
     then visit http://<your-domain-or-IP>/

EOF
