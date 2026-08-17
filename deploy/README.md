# Deploying magnetlookup to AWS

Lift-and-shift of the current Mac/launchd setup onto a single small EC2 or
Lightsail instance. One box runs the daily scrape (`magnetlookup.py`,
retargeted to archive.org + LinuxTracker) and the web dashboard
(`webserver.py`), fronted by nginx, with the SQLite library backed up to S3.

Nothing in this repo talks to AWS's control plane on its own — you still
provision the instance, bucket, and IAM role yourself (console, CLI, or your
own Terraform). This just configures the instance once it exists.

## 1. Provision

- **Instance**: EC2 `t4g.small` (arm64, ~$12/mo on-demand) or a Lightsail
  instance of similar size, Ubuntu 24.04 LTS. This app is a single daily
  scrape job plus a low-traffic dashboard — don't size up further unless you
  add a lot more search sources.
- **Storage**: default 8–16 GB gp3 EBS volume is plenty; the SQLite library
  and HTML/CSV results are small.
- **Security group**: inbound 80/443 open (443 once you have TLS — see step
  4), 22 restricted to your own IP. Do **not** open 8080 — `webserver.py`
  binds to `127.0.0.1` by default and nginx is the only public entry point.
- **IAM role** (optional, for backups): attach an instance role with the
  policy in `iam-policy-s3-backup.json` (fill in your bucket name) scoped to
  one S3 bucket. This lets `aws s3 sync` work with zero stored credentials
  on the box — no `aws configure` needed.
- **S3 bucket** (optional, for backups): any bucket, e.g.
  `aws s3 mb s3://your-magnetlookup-backups`.

## 2. Install

SSH in, clone/copy this repo onto the instance, then:

```bash
cd MyMagnet
sudo ./deploy/setup.sh
```

This installs Python/nginx/certbot/awscli, creates an unprivileged
`magnetlookup` system user, copies the app to `/opt/magnetlookup/app`, sets
up a venv with just `requests` + `beautifulsoup4` + `lxml` (Selenium is
skipped — see below), installs the systemd units and nginx config, and
starts the web dashboard + timers. It prints a checklist of the few things
only you can fill in (env file, search terms, DNS/TLS).

## 3. Why no headless Chrome

The old site list (Pirate Bay/RARBG/1337x) needed Selenium because those
sites gate their results behind a JS challenge. Neither replacement does:
archive.org is a plain JSON API call, and linuxtracker.org is classic
server-rendered HTML. The systemd unit runs with `--no-js`, so there's no
Chromium/chromedriver to install or keep patched on the instance. If you add
a JS-heavy source later, drop `--no-js` from `magnetlookup.service` and
`pip install selenium webdriver-manager` plus a Chromium package into the
venv/instance.

## 4. TLS

Point a DNS A record at the instance's public IP, edit `server_name` in
`/etc/nginx/sites-available/magnetlookup`, then:

```bash
sudo certbot --nginx -d magnet.yourdomain.com
```

Certbot edits the nginx config in place to add the 443 block and HTTP→HTTPS
redirect, and sets up auto-renewal. Also consider uncommenting the
`auth_basic` lines in `nginx-magnetlookup.conf` — the dashboard shows your
search/download history, worth gating even over TLS.

## 5. Day-to-day

- Logs: `sudo journalctl -u magnetlookup.service` (scrape run),
  `sudo journalctl -u magnetlookup-web.service` (dashboard).
- Manual scrape: `sudo systemctl start magnetlookup.service`.
- Manual backup: `sudo systemctl start magnetlookup-backup.service`.
- Config lives in `/etc/magnetlookup/env` (systemd EnvironmentFile) — same
  `MAGNET_*` variables `config.ini` already supports, so no code changes are
  needed to move settings between your Mac and this box.
- `search_term.txt` and `urls.txt` live in `/opt/magnetlookup/data/` on the
  instance now — there's no iCloud/Drive sync path on a server, so edit them
  directly (scp a new version up, or wire in a small script against the S3
  bucket if you want to keep syncing terms from Drive).

## 6. If this outgrows one instance

Single SQLite file + single daily job doesn't need more than this. If you
later want the scrape job to scale independently of the dashboard, or want
the DB reachable from more than one place, the next step is: EventBridge
Scheduler triggering an ECS Fargate task for the scrape (same `--cron
--no-browser --no-js` invocation, packaged as a container instead of a
systemd unit), SQLite file moved to EFS so both the Fargate task and a small
always-on Fargate service (dashboard) can mount it, config moved from
`/etc/magnetlookup/env` to SSM Parameter Store. Not worth building until the
single-instance setup actually becomes a bottleneck.
