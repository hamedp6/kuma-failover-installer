Here’s a complete, drop-in **README.md** you can paste straight into your repo.

---

# Uptime Kuma → Cloudflare Failover (Flask UI)

A small Python service that flips Cloudflare DNS between **Server 1** and **Server 2** based on **Uptime Kuma** webhooks. Comes with a minimal **web UI** (status, logs, settings, manual switch) and a **Prometheus** `/metrics` endpoint. Installer supports one-liner, prompts, and non-interactive env-driven installs.

## Why this exists

When your primary host dies, you want traffic moved to a hot standby **now**, not when someone wakes up. This tool listens to Uptime Kuma heartbeats and updates Cloudflare records automatically. It’s simple, auditable, and doesn’t try to be a full load balancer.

---

## Features

* **Webhook endpoints** for Uptime Kuma:

  * `POST /webhook/server1`
  * `POST /webhook/server2`
* **Decision engine** with:

  * Manual freeze (hold DNS as-is)
  * Cooldown (avoid rapid flip/flop)
  * Simple hysteresis (reduce flapping)
* **Cloudflare updates**:

  * DNS `A/AAAA` records
  * Preserves `proxied` flag
  * Caches record IDs
* **UI (port defaults to 5000)**:

  * Dashboard (current IP, server status, records table)
  * Live logs (SSE)
  * Settings (TTL/cooldown/thresholds, secret rotation, config JSON)
  * Manual switch & freeze/unfreeze
* **Security**:

  * Webhook shared secret (`X-Webhook-Secret`)
  * Optional Basic Auth for the UI (ADMIN\_USER/PASS)
  * CSRF on all POSTs
* **Ops endpoints**:

  * `/metrics` (Prometheus)
  * `/healthz` (basic health)
* **Installer**:

  * One-liner install
  * Preseeds webhook secret
  * Creates systemd service `failover`
  * Optional UFW rule

---

## Requirements

* Ubuntu 22.04 (works on similar)
* Cloudflare API Token with **DNS\:Read** and **DNS\:Edit** on the target zone
* Uptime Kuma monitors for Server 1 and Server 2

---

## Quick start (one-liner)

> This runs an interactive installer that prompts for your settings, generates the `.env`, pre-seeds a webhook secret, and starts a systemd service.

```bash
curl -fsSL https://raw.githubusercontent.com/hamedp6/kuma-failover-installer/main/install.sh | sudo bash
```

What it does:

* Installs Python/venv + deps
* Creates `/opt/failover` and a systemd service `failover`
* Prompts for:

  * Cloudflare Zone ID & API token
  * DNS record names (FQDNs, comma-separated)
  * Server1/Server2 IPs
  * Optional UI admin user/pass
  * Optional Telegram bot/chat
  * Webhook secret (leave blank to auto-generate)
* Starts the service and (optionally) opens the port via UFW

---

## Non-interactive install (CI/automation)

Set env vars, then run the installer with sudo:

```bash
export REPO_RAW="https://raw.githubusercontent.com/hamedp6/kuma-failover-installer/main"
export CLOUDFLARE_ZONE_ID="Z1234567890abcdef"
export CLOUDFLARE_API_TOKEN="cf_api_token_here"
export DNS_RECORD_NAMES="api.example.com,www.example.com"
export SERVER1_IP="203.0.113.10"
export SERVER2_IP="203.0.113.20"
export PORT="5000"
export TTL="60"
export ADMIN_USER="admin"
export ADMIN_PASS="supersecret"
export TELEGRAM_BOT_TOKEN=""
export TELEGRAM_CHAT_ID=""
export WEBHOOK_SECRET="preseeded-secret-value"

curl -fsSL https://raw.githubusercontent.com/hamedp6/kuma-failover-installer/main/install.sh | sudo -E bash
```

> If you omit `WEBHOOK_SECRET`, the installer auto-generates one.

---

## Manual install (clone + run)

```bash
git clone https://github.com/hamedp6/kuma-failover-installer.git
cd kuma-failover-installer
sudo ./install.sh
```

---

## Uptime Kuma setup

Create **two monitors** (one per server). In each monitor’s webhook settings:

* **URL** (choose the right one per monitor):

  * `http://<failover-host>:<PORT>/webhook/server1`
  * `http://<failover-host>:<PORT>/webhook/server2`
* **Method:** `POST`
* **Content-Type:** `application/json`
* **Body:** leave empty (Kuma sends its default JSON)
* **HTTP Header:**

  * Name: `X-Webhook-Secret`
  * Value: `<your secret>`

### Where do I get the secret?

* The installer wrote it to `/opt/failover/failover-state.json` as `"webhook_secret"`.
* You can also **rotate** it in the UI → **Settings → Rotate Secret**.

### What JSON does Kuma send?

By default:

```json
{ "heartbeat": { "status": 1 } }
```

where `status` is `1` (UP) or `0` (DOWN). That’s exactly what the app expects.

---

## Configuration

Installer writes `/opt/failover/.env`. Example:

```env
CLOUDFLARE_ZONE_ID=your_zone_id
CLOUDFLARE_API_TOKEN=your_api_token
DNS_RECORD_NAMES=api.example.com,www.example.com
SERVER1_IP=203.0.113.10
SERVER2_IP=203.0.113.20
PORT=5000
TTL=60
LOG_FILE=/opt/failover/failover.log
STATE_FILE=/opt/failover/failover-state.json
SECRET_KEY=auto_generated_for_sessions
ADMIN_USER=admin
ADMIN_PASS=your_password
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

**Webhook secret** lives in the state file, **not** in `.env`:

```
/opt/failover/failover-state.json
{
  "webhook_secret": "abcd1234..."
}
```

### Variable reference

| Var                         | Required | What                                               |
| --------------------------- | -------: | -------------------------------------------------- |
| `CLOUDFLARE_ZONE_ID`        |        ✅ | Your Cloudflare Zone ID                            |
| `CLOUDFLARE_API_TOKEN`      |        ✅ | Token with **Zone.DNS\:Read** + **Zone.DNS\:Edit** |
| `DNS_RECORD_NAMES`          |        ✅ | Comma-separated FQDNs (must exist in the zone)     |
| `SERVER1_IP` / `SERVER2_IP` |        ✅ | Public A record targets                            |
| `PORT`                      |        ❌ | UI/API port (default 5000)                         |
| `TTL`                       |        ❌ | DNS TTL (≥ 30 recommended)                         |
| `ADMIN_USER`/`ADMIN_PASS`   |        ❌ | If set, UI requires Basic Auth                     |
| `TELEGRAM_*`                |        ❌ | If set, send Telegram alerts                       |

---

## Service management

```bash
# Status / logs
sudo systemctl status failover
journalctl -u failover -f

# Restart after config changes
sudo systemctl restart failover

# Enable on boot (installer already does this)
sudo systemctl enable failover
```

---

## Web UI

Open: `http://<failover-host>:<PORT>/`

* **Dashboard** – current DNS target, server states, TTL/cooldown, records
* **Logs** – live tail via SSE
* **Settings** – TTL/cooldown/thresholds, **Rotate Webhook Secret**, download config JSON
* **Actions** – Switch to Server1/Server2, Freeze/Unfreeze, Refresh Cloudflare records, Test Telegram

> If you set `ADMIN_USER`/`ADMIN_PASS`, UI is protected with Basic Auth.

---

## API & endpoints

* `GET /` – dashboard (HTML)
* `GET /logs` – live logs (HTML)
* `GET /settings` – settings page (HTML)
* `GET /api/status` – current state + DNS records (JSON)
* `POST /api/switch` – manual switch (form/json: `target=server1|server2`)
* `POST /api/freeze` – enable/disable freeze (form/json: `enabled=true|false`)
* `POST /api/refresh-records` – refresh Cloudflare records cache
* `GET /api/logs` – last N log lines (default 200)
* `GET /api/logs/stream` – Server-Sent Events log stream
* `GET /api/config` – env subset + state
* `POST /api/config` – save TTL/cooldown/thresholds
* `GET /api/diagnostics` – Cloudflare API and DNS lookups (light checks)
* `GET /api/webhook-secret` – returns masked secret
* `POST /api/webhook-secret/rotate` – rotate secret
* `GET /metrics` – Prometheus metrics
* `GET /healthz` – health probe

---

## Prometheus metrics (sample)

```
failover_current_active 1           # 1=server1, 2=server2
failover_server1_up 1
failover_server2_up 0
failover_frozen 0
failover_last_switch {} 1723234567  # unix timestamp (if known)
```

Scrape `http://<failover-host>:<PORT>/metrics`.

---

## Updating / Uninstalling

```bash
# Update the app from repo + deps; service restarts
sudo ./install.sh --update

# Uninstall (prompts to remove /opt/failover)
sudo ./install.sh --uninstall
```

---

## Firewall / HTTPS

* Installer can open the port in **UFW** if you say yes.
* For HTTPS, put **Nginx/Caddy** in front and keep the Flask app on 127.0.0.1:5000.

---

## Troubleshooting

**Installer hangs at “Configuring environment (.env)…”.**
You piped the script and lost TTY. Use the one-liner above (this installer already forces reads from `/dev/tty`), or run:

```bash
curl -fsSL <…>/install.sh -o install.sh && sudo bash install.sh
```

**`set: Illegal option -o pipefail`.**
You ran it under `sh`/`dash`. The installer self-reexecs with bash; if you still see this, run:

```bash
sudo bash install.sh
```

**Cloudflare update errors / records not found.**
Your `DNS_RECORD_NAMES` must match FQDNs **exactly** as they exist in Cloudflare.

**401 on webhook.**
Make sure Kuma adds header `X-Webhook-Secret: <your-secret>`.

**UI asks for login.**
You set `ADMIN_USER`/`ADMIN_PASS`. Either use them or clear those values and restart the service.

**State migration warning (`server1Status`/`currentDNS`).**
Old state keys get migrated in memory. If you want a fresh start:
`sudo systemctl stop failover && sudo rm -f /opt/failover/failover-state.json && sudo systemctl start failover`

---

## Directory layout (on the server)

```
/opt/failover/
├─ venv/                          # Python venv
├─ uptime-kuma-cloudflare-failover.py
├─ .env                           # generated by installer
├─ failover-state.json            # includes webhook_secret
└─ failover.log
```

---

## License

MIT (see `LICENSE`).

---

## Contributing

Open issues/PRs. Keep the installer idempotent and small. Don’t leak secrets to logs. Add useful metrics before adding “flashy” features.

---

## Credit

Built for a pragmatic “flip the A record when bad things happen” use case. If you outgrow this, consider **Cloudflare Load Balancer** with managed health checks.
