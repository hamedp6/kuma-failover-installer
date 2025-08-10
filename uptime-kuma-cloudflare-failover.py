#!/usr/bin/env python3
"""
Uptime Kuma → Cloudflare DNS failover with a built-in Flask UI (HTMX/Jinja)

Features
- Webhooks: /webhook/server1, /webhook/server2 (expects {"heartbeat":{"status":1|0}})
- Decision engine with optional freeze, cooldown, simple hysteresis
- Cloudflare DNS updater (A/AAAA; preserves proxied; caches record IDs)
- Telegram alerts (optional)
- Persistent state JSON + structured logs
- Flask UI on $PORT (default 5000): Dashboard, Logs (SSE), Settings, Manual switch
- JSON API; /metrics (Prometheus) and /healthz
- Optional HTTP Basic auth via ADMIN_USER / ADMIN_PASS
- CSRF token on mutating routes

Deps
  pip install flask requests python-dotenv
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, asdict
from functools import wraps
from typing import Dict, List, Optional
from types import SimpleNamespace

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template_string,
    request,
    session,
    url_for,
)
from jinja2 import DictLoader

# -------------------------
# Load .env if available
# -------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -------------------------
# Configuration & Defaults
# -------------------------
CLOUDFLARE_ZONE_ID = os.getenv("CLOUDFLARE_ZONE_ID", "zone_ID")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "TOKEN_API")
DNS_RECORD_NAMES = [s.strip() for s in os.getenv("DNS_RECORD_NAMES", "").split(",") if s.strip()]
SERVER1_IP = os.getenv("SERVER1_IP", "1.2.3.4")
SERVER2_IP = os.getenv("SERVER2_IP", "5.6.7.8")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
LOG_FILE = os.getenv("LOG_FILE", "./failover.log")
STATE_FILE = os.getenv("STATE_FILE", "./failover-state.json")
TTL_DEFAULT = int(os.getenv("TTL", "60"))
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(16)
ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS = os.getenv("ADMIN_PASS")

CLOUDFLARE_API_BASE = f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}"

# -------------
# Logging setup
# -------------
logger = logging.getLogger("failover")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
sh = logging.StreamHandler()
sh.setFormatter(fmt)
logger.addHandler(sh)
if LOG_FILE:
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

# ---------------
# Shared app state
# ---------------
state_lock = threading.Lock()

@dataclass
class ServiceState:
    server1_up: bool = True
    server2_up: bool = True
    current_dns: str = SERVER1_IP
    last_switch_at: Optional[str] = None  # ISO8601
    freeze: bool = False
    ttl: int = TTL_DEFAULT
    webhook_secret: Optional[str] = None
    # Hysteresis thresholds
    down_threshold: int = 1
    up_threshold: int = 1
    cooldown_seconds: int = 0
    _s1_down_streak: int = 0
    _s1_up_streak: int = 0
    _s2_down_streak: int = 0
    _s2_up_streak: int = 0

    def to_public(self):
        d = asdict(self)
        for k in list(d.keys()):
            if k.startswith("_s"):
                d.pop(k, None)
        if d.get("webhook_secret"):
            d["webhook_secret"] = "***"
        return d

    @classmethod
    def load(cls, path: str) -> "ServiceState":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            obj = cls(**{**asdict(cls()), **data})
            if not obj.webhook_secret:
                obj.webhook_secret = secrets.token_urlsafe(24)
            return obj
        except FileNotFoundError:
            obj = cls()
            obj.webhook_secret = secrets.token_urlsafe(24)
            return obj
        except Exception as e:
            logger.warning("Failed to load state, using defaults: %s", e)
            obj = cls()
            obj.webhook_secret = secrets.token_urlsafe(24)
            return obj

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

app_state = ServiceState.load(STATE_FILE)

# Cached Cloudflare records: name -> record dict
cached_records: Dict[str, Dict] = {}

# ---------------------
# Helper/infra functions
# ---------------------

def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram send failed: %s - %s", resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Telegram send exception: %s", e)


def cf_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"}


def load_dns_records() -> None:
    global cached_records
    page = 1
    per_page = 100
    found: Dict[str, Dict] = {}
    while True:
        url = f"{CLOUDFLARE_API_BASE}/dns_records?page={page}&per_page={per_page}"
        resp = requests.get(url, headers=cf_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for r in data.get("result", []):
            name = r.get("name")
            if not DNS_RECORD_NAMES or name in DNS_RECORD_NAMES:
                found[name] = r
        info = data.get("result_info") or {}
        total_pages = info.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1
    cached_records = found
    logger.info("Cached DNS Records: %s", ", ".join(found.keys()) or "<none>")


def update_dns(new_ip: str) -> None:
    if not cached_records:
        load_dns_records()
    if not cached_records:
        logger.error("No DNS records cached/found. Aborting DNS update.")
        return
    for name, record in cached_records.items():
        record_id = record.get("id")
        if not record_id:
            continue
        payload = {
            "type": record.get("type", "A"),
            "name": name,
            "content": new_ip,
            "ttl": app_state.ttl,
            "proxied": record.get("proxied", False),
        }
        url = f"{CLOUDFLARE_API_BASE}/dns_records/{record_id}"
        resp = requests.put(url, headers=cf_headers(), json=payload, timeout=15)
        if not resp.ok:
            logger.error("Cloudflare update failed for %s: %s - %s", name, resp.status_code, resp.text)
        else:
            logger.info("Updated %s → %s", name, new_ip)
    with state_lock:
        app_state.current_dns = new_ip
        app_state.last_switch_at = dt.datetime.utcnow().isoformat() + "Z"
        app_state.save(STATE_FILE)


def _cooldown_ok(last_iso: Optional[str]) -> bool:
    if not app_state.cooldown_seconds or not last_iso:
        return True
    try:
        last = dt.datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
        return (dt.datetime.utcnow() - last.replace(tzinfo=None)).total_seconds() >= app_state.cooldown_seconds
    except Exception:
        return True


def decide_failover(source: str = "auto") -> str:
    with state_lock:
        if app_state.freeze:
            msg = (
                f"🧊 Frozen. No DNS change. "
                f"S1={'UP' if app_state.server1_up else 'DOWN'} "
                f"S2={'UP' if app_state.server2_up else 'DOWN'} | Current: {app_state.current_dns}"
            )
            logger.info(msg)
            return msg

        if not _cooldown_ok(app_state.last_switch_at):
            msg = f"⏳ Cooldown active; holding DNS at {app_state.current_dns}"
            logger.info(msg)
            return msg

        # Update streaks for hysteresis
        if app_state.server1_up:
            app_state._s1_up_streak += 1
            app_state._s1_down_streak = 0
        else:
            app_state._s1_down_streak += 1
            app_state._s1_up_streak = 0
        if app_state.server2_up:
            app_state._s2_up_streak += 1
            app_state._s2_down_streak = 0
        else:
            app_state._s2_down_streak += 1
            app_state._s2_up_streak = 0

        desired_ip = app_state.current_dns
        choose_s1 = app_state.server1_up and (app_state.up_threshold <= 1 or app_state._s1_up_streak >= app_state.up_threshold)
        choose_s2 = (not app_state.server1_up and app_state.server2_up and (app_state.up_threshold <= 1 or app_state._s2_up_streak >= app_state.up_threshold))

        if choose_s1:
            desired_ip = SERVER1_IP
        elif choose_s2:
            desired_ip = SERVER2_IP
        else:
            desired_ip = app_state.current_dns

        changed = desired_ip != app_state.current_dns

    if changed:
        update_dns(desired_ip)
        msg = (
            f"🔄 DNS switched to {desired_ip} by {source}\n"
            f"📡 S1={'UP' if app_state.server1_up else 'DOWN'}, "
            f"S2={'UP' if app_state.server2_up else 'DOWN'}"
        )
        logger.info(msg)
        telegram_send(msg)
    else:
        msg = (
            f"ℹ️ No DNS change. S1={'UP' if app_state.server1_up else 'DOWN'}, "
            f"S2={'UP' if app_state.server2_up else 'DOWN'} | Current: {app_state.current_dns}"
        )
        logger.info(msg)
        with state_lock:
            app_state.save(STATE_FILE)
    return msg


# -----------------
# Flask application
# -----------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- HTMX helpers (refresh / redirect) ---
def hx_refresh():
    """Tell HTMX to reload the current page."""
    return Response("", 204, {"HX-Refresh": "true"})

def hx_redirect(url: str):
    """Tell HTMX to navigate to a full page URL."""
    return Response("", 204, {"HX-Redirect": url})

# Template loader for base/layout
BASE_HTML = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Failover</title>
  <script src=\"https://unpkg.com/htmx.org@2.0.2\"></script>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;margin:0;background:#0b0c10;color:#e6e6e6}
    header{padding:12px 16px;background:#11141a;border-bottom:1px solid #222}
    a{color:#8ab4f8;text-decoration:none}
    .container{padding:16px;max-width:1100px;margin:0 auto}
    .grid{display:grid;gap:12px}
    .cards{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
    .card{background:#141820;border:1px solid #232b36;border-radius:12px;padding:14px}
    .title{font-size:14px;color:#9aa0a6;margin:0 0 6px}
    .big{font-size:22px;margin:4px 0}
    .btn{display:inline-block;padding:8px 12px;border-radius:10px;border:1px solid #2a323f;background:#1a2230;color:#e6e6e6;cursor:pointer}
    .btn:hover{filter:brightness(1.1)}
    .danger{border-color:#5f2a2a;background:#2a1a1a}
    .ok{color:#a5d6a7}
    .bad{color:#ef9a9a}
    .chip{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #2a323f;background:#1a2230;font-size:12px}
    table{width:100%;border-collapse:collapse}
    td,th{padding:8px;border-bottom:1px solid #232b36;text-align:left}
    input,select{background:#0e141b;color:#e6e6e6;border:1px solid #2a323f;border-radius:8px;padding:6px 8px}
    label{font-size:12px;color:#9aa0a6}
    .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .mt{margin-top:12px}
    .warn{background:#2a1f10;border:1px solid #5a4320;color:#ffda7b;padding:8px;border-radius:10px}
  </style>
</head>
<body>
  <header>
    <div class=\"container row\">
      <strong>Failover UI</strong>
      <a href=\"{{ url_for('ui_dashboard') }}\" style=\"margin-left:16px\">Dashboard</a>
      <a href=\"{{ url_for('ui_logs') }}\" style=\"margin-left:12px\">Logs</a>
      <a href=\"{{ url_for('ui_settings') }}\" style=\"margin-left:12px\">Settings</a>
      <span style=\"margin-left:auto;font-size:12px;color:#9aa0a6\">Zone {{zone}}</span>
    </div>
  </header>
  <div class=\"container\">
    {% block content %}{% endblock %}
  </div>
</body>
</html>
"""

DASHBOARD_HTML = """
{% extends 'base.html' %}
{% block content %}
<div class=\"grid cards\">
  <div class=\"card\">
    <div class=\"title\">Current DNS Target</div>
    <div class=\"big\">{{state.current_dns}}</div>
    <div>Last switch: {{state.last_switch_at or '—'}}</div>
  </div>
  <div class=\"card\">
    <div class=\"title\">Server 1</div>
    <div class=\"big\">{{'UP' if state.server1_up else 'DOWN'}}</div>
    <div class=\"{{'ok' if state.server1_up else 'bad'}}\">{{ SERVER1_IP }}</div>
  </div>
  <div class=\"card\">
    <div class=\"title\">Server 2</div>
    <div class=\"big\">{{'UP' if state.server2_up else 'DOWN'}}</div>
    <div class=\"{{'ok' if state.server2_up else 'bad'}}\">{{ SERVER2_IP }}</div>
  </div>
  <div class=\"card\">
    <div class=\"title\">Mode</div>
    <div class=\"big\">{{ 'Frozen' if state.freeze else 'Automatic' }}</div>
    <div>TTL {{state.ttl}} • Cooldown {{state.cooldown_seconds}}s</div>
  </div>
</div>

<div class=\"card mt\">
  <div class=\"title\">Records</div>
  <div id=\"records\" hx-get=\"{{ url_for('api_records') }}\" hx-trigger=\"load, every 10s\" hx-target=\"#records\" hx-swap=\"outerHTML\"></div>
</div>

<div class=\"card mt\">
  <div class=\"row\">
    <form method=\"post\" action=\"{{ url_for('api_switch') }}\" hx-post=\"{{ url_for('api_switch') }}\" hx-headers='{"X-CSRF-Token": "{{ csrf }}"}' hx-swap=\"none\">
      <input type=\"hidden\" name=\"target\" value=\"server1\"><button class=\"btn\">Switch to Server1</button>
    </form>
    <form method=\"post\" action=\"{{ url_for('api_switch') }}\" hx-post=\"{{ url_for('api_switch') }}\" hx-headers='{"X-CSRF-Token": "{{ csrf }}"}' hx-swap=\"none\">
      <input type=\"hidden\" name=\"target\" value=\"server2\"><button class=\"btn\">Switch to Server2</button>
    </form>
    <form method=\"post\" action=\"{{ url_for('api_freeze') }}\" hx-post=\"{{ url_for('api_freeze') }}\" hx-headers='{"X-CSRF-Token": "{{ csrf }}"}' hx-swap=\"none\">
      <input type=\"hidden\" name=\"enabled\" value=\"{{ 'false' if state.freeze else 'true' }}\">
      <button class=\"btn {{'danger' if not state.freeze else ''}}\">{{ 'Freeze' if not state.freeze else 'Unfreeze' }}</button>
    </form>
    <form method=\"post\" action=\"{{ url_for('api_refresh_records') }}\" hx-post=\"{{ url_for('api_refresh_records') }}\" hx-headers='{"X-CSRF-Token": "{{ csrf }}"}' hx-swap=\"none\">
      <button class=\"btn\">Refresh Records</button>
    </form>
    <form method=\"post\" action=\"{{ url_for('api_test_telegram') }}\" hx-post=\"{{ url_for('api_test_telegram') }}\" hx-headers='{"X-CSRF-Token": "{{ csrf }}"}' hx-swap=\"none\">
      <button class=\"btn\">Test Telegram</button>
    </form>
  </div>
</div>

{% if not state.server1_up and not state.server2_up %}
<div class=\"warn mt\">Both servers are DOWN. Holding last DNS unless you switch manually.</div>
{% endif %}
{% endblock %}
"""

RECORDS_PARTIAL = """
<div id=\"records\">
  <table>
    <thead>
    <tr><th>Name</th><th>Type</th><th>Content</th><th>TTL</th><th>Proxied</th></tr>
    </thead>
    <tbody>
    {% for name, r in records.items() %}
      <tr>
        <td>{{name}}</td>
        <td>{{r.type}}</td>
        <td>{{r.content}}</td>
        <td>{{r.ttl}}</td>
        <td>{{'yes' if r.proxied else 'no'}}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
"""

LOGS_HTML = """
{% extends 'base.html' %}
{% block content %}
<div class=\"card\">
  <div class=\"title\">Live logs</div>
  <pre id=\"logbox\" style=\"white-space:pre-wrap;max-height:70vh;overflow:auto;background:#0a0e14;padding:8px;border-radius:8px;border:1px solid #232b36\"></pre>
</div>
<script>
const logbox=document.getElementById('logbox');
const es=new EventSource('{{ url_for('api_logs_stream') }}');
es.onmessage=(e)=>{logbox.textContent += e.data + "\\n"; logbox.scrollTop = logbox.scrollHeight;};
</script>
{% endblock %}
"""

SETTINGS_HTML = """
{% extends 'base.html' %}
{% block content %}
<form class=\"card\" method=\"post\" action=\"{{ url_for('api_config_post') }}\" hx-post=\"{{ url_for('api_config_post') }}\" hx-headers='{"X-CSRF-Token":"{{ csrf }}"}' hx-swap=\"none\">
  <div class=\"title\">Settings</div>
  <div class=\"grid\" style=\"grid-template-columns:repeat(auto-fit,minmax(240px,1fr))\">
    <label>TTL<br><input type=\"number\" name=\"ttl\" value=\"{{state.ttl}}\"></label>
    <label>Cooldown seconds<br><input type=\"number\" name=\"cooldown_seconds\" value=\"{{state.cooldown_seconds}}\"></label>
    <label>UP threshold<br><input type=\"number\" name=\"up_threshold\" value=\"{{state.up_threshold}}\"></label>
    <label>DOWN threshold (not used yet)<br><input type=\"number\" name=\"down_threshold\" value=\"{{state.down_threshold}}\"></label>
  </div>
  <div class=\"row mt\">
    <button class=\"btn\">Save</button>
  </div>
</form>

<div class=\"card mt\">
  <div class=\"row\">
    <div>
      <div class=\"title\">Webhook secret</div>
      <code>Send header X-Webhook-Secret: ******</code>
    </div>
    <form method=\"post\" action=\"{{ url_for('api_webhook_secret_rotate') }}\" hx-post=\"{{ url_for('api_webhook_secret_rotate') }}\" hx-headers='{"X-CSRF-Token":"{{ csrf }}"}' hx-swap=\"none\">
      <button class=\"btn\">Rotate Secret</button>
    </form>
    <a class=\"btn\" href=\"{{ url_for('api_config') }}\">Download config (JSON)</a>
  </div>
</div>
{% endblock %}
"""

# ----- Auth (optional HTTP Basic) -----

def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ADMIN_USER and not ADMIN_PASS:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="Failover"'})
        return f(*args, **kwargs)
    return wrapper

# ----- CSRF (simple token) -----

def get_csrf_token():
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["csrf"] = tok
    return tok


def check_csrf():
    expected = session.get("csrf")
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
    if expected and provided and secrets.compare_digest(expected, provided):
        return True
    abort(400, "CSRF token missing or invalid")

# Register templates so {% extends 'base.html' %} works
app.jinja_loader = DictLoader({
    'base.html': BASE_HTML,
})

# -------------
# UI routes
# -------------
@app.route("/")
@requires_auth
def ui_dashboard():
    csrf = get_csrf_token()
    return render_template_string(
        DASHBOARD_HTML,
        state=app_state,
        SERVER1_IP=SERVER1_IP,
        SERVER2_IP=SERVER2_IP,
        csrf=csrf,
        zone=CLOUDFLARE_ZONE_ID,
    )


@app.route("/logs")
@requires_auth
def ui_logs():
    return render_template_string(LOGS_HTML, zone=CLOUDFLARE_ZONE_ID)


@app.route("/settings")
@requires_auth
def ui_settings():
    csrf = get_csrf_token()
    return render_template_string(SETTINGS_HTML, state=app_state, csrf=csrf, zone=CLOUDFLARE_ZONE_ID)


# -------------
# API routes
# -------------
@app.get("/api/status")
@requires_auth
def api_status():
    return jsonify({
        "state": app_state.to_public(),
        "records": {k: {"type": v.get("type"), "content": v.get("content"), "ttl": v.get("ttl"), "proxied": v.get("proxied")} for k, v in cached_records.items()}
    })


@app.get("/api/records")
@requires_auth
def api_records():
    ns = {k: SimpleNamespace(**v) for k, v in cached_records.items()}
    html = render_template_string(RECORDS_PARTIAL, records=ns)
    return Response(html, 200, {"Content-Type": "text/html"})


@app.post("/api/refresh-records")
@requires_auth
def api_refresh_records():
    check_csrf()
    load_dns_records()
    return hx_refresh()  # force full page reload


@app.post("/api/test-telegram")
@requires_auth
def api_test_telegram():
    check_csrf()
    telegram_send("Test from Failover UI")
    return hx_refresh()


@app.post("/api/switch")
@requires_auth
def api_switch():
    check_csrf()
    form = request.form if request.form else (request.json or {})
    target = str(form.get("target", "")).lower()
    if target not in {"server1", "server2"}:
        abort(400, "target must be server1 or server2")
    ip = SERVER1_IP if target == "server1" else SERVER2_IP
    update_dns(ip)
    logger.info("Manual switch → %s (%s)", ip, target)
    return hx_refresh()


@app.post("/api/freeze")
@requires_auth
def api_freeze():
    check_csrf()
    form = request.form if request.form else (request.json or {})
    enabled = str(form.get("enabled", "false")).lower() in {"1", "true", "yes", "on"}
    with state_lock:
        app_state.freeze = enabled
        app_state.save(STATE_FILE)
    return hx_refresh()


@app.get("/api/config")
@requires_auth
def api_config():
    return jsonify({
        "env": {
            "CLOUDFLARE_ZONE_ID": CLOUDFLARE_ZONE_ID,
            "DNS_RECORD_NAMES": DNS_RECORD_NAMES,
            "SERVER1_IP": SERVER1_IP,
            "SERVER2_IP": SERVER2_IP,
            "TTL_DEFAULT": TTL_DEFAULT,
        },
        "state": app_state.to_public(),
    })


@app.post("/api/config")
@requires_auth
def api_config_post():
    check_csrf()
    form = request.form if request.form else (request.json or {})

    def _int(name, default):
        try:
            return int(form.get(name, default))
        except Exception:
            return default

    with state_lock:
        app_state.ttl = max(30, _int("ttl", app_state.ttl))
        app_state.cooldown_seconds = max(0, _int("cooldown_seconds", app_state.cooldown_seconds))
        app_state.up_threshold = max(1, _int("up_threshold", app_state.up_threshold))
        app_state.down_threshold = max(0, _int("down_threshold", app_state.down_threshold))
        app_state.save(STATE_FILE)
    return hx_redirect(url_for("ui_settings"))  # back to settings with a full load


@app.get("/api/diagnostics")
@requires_auth
def api_diagnostics():
    ok_cf = False
    err_cf = None
    try:
        r = requests.get(f"{CLOUDFLARE_API_BASE}", headers=cf_headers(), timeout=10)
        ok_cf = r.ok
        if not ok_cf:
            err_cf = r.text
    except Exception as e:
        err_cf = str(e)

    results = []
    for name in (DNS_RECORD_NAMES or list(cached_records.keys()))[:3]:
        try:
            rr = requests.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": name, "type": "A"},
                headers={"accept": "application/dns-json"},
                timeout=8,
            )
            results.append({"name": name, "ok": rr.ok, "body": rr.json() if rr.ok else rr.text})
        except Exception as e:
            results.append({"name": name, "ok": False, "error": str(e)})

    return jsonify({"cloudflare_api_ok": ok_cf, "cloudflare_error": err_cf, "dns_checks": results})


@app.get("/api/webhook-secret")
@requires_auth
def api_webhook_secret_get():
    return jsonify({"webhook_secret": "***"})


@app.post("/api/webhook-secret/rotate")
@requires_auth
def api_webhook_secret_rotate():
    check_csrf()
    with state_lock:
        app_state.webhook_secret = secrets.token_urlsafe(24)
        app_state.save(STATE_FILE)
    return hx_redirect(url_for("ui_settings"))


@app.get("/api/logs")
@requires_auth
def api_logs():
    limit = int(request.args.get("limit", 200))
    out = tail_log(limit)
    return jsonify({"lines": out})


def tail_log(n: int) -> List[str]:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except Exception:
        return []


@app.get("/api/logs/stream")
@requires_auth
def api_logs_stream():
    def gen():
        last_size = 0
        while True:
            try:
                with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(last_size)
                    chunk = f.read()
                    last_size = f.tell()
                for line in chunk.splitlines():
                    yield f"data: {line}\n\n"
            except Exception:
                pass
            time.sleep(1)
    return Response(gen(), mimetype="text/event-stream")


@app.get("/metrics")
def metrics():
    lines = []
    lines.append("failover_current_active 1" if app_state.current_dns == SERVER1_IP else "failover_current_active 2")
    lines.append(f"failover_server1_up {1 if app_state.server1_up else 0}")
    lines.append(f"failover_server2_up {1 if app_state.server2_up else 0}")
    lines.append(f"failover_frozen {1 if app_state.freeze else 0}")
    if app_state.last_switch_at:
        try:
            last = dt.datetime.fromisoformat(app_state.last_switch_at.replace("Z", "+00:00")).timestamp()
            lines.append(f"failover_last_switch {last}")
        except Exception:
            pass
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

# -----------------
# Webhook endpoints
# -----------------

def _parse_status(body: dict) -> bool:
    try:
        return int(body.get("heartbeat", {}).get("status", 0)) == 1
    except Exception:
        return False


def _check_webhook_secret():
    secret = request.headers.get("X-Webhook-Secret")
    if not secret or not app_state.webhook_secret or not secrets.compare_digest(secret, app_state.webhook_secret):
        abort(401, "Invalid webhook secret")


@app.post("/webhook/server1")
def webhook_server1():
    _check_webhook_secret()
    is_up = _parse_status(request.json or {})
    with state_lock:
        app_state.server1_up = is_up
    msg = decide_failover(source="webhook:s1")
    return jsonify({"message": "Server1 status updated", "server1Status": is_up, "server2Status": app_state.server2_up, "info": msg})


@app.post("/webhook/server2")
def webhook_server2():
    _check_webhook_secret()
    is_up = _parse_status(request.json or {})
    with state_lock:
        app_state.server2_up = is_up
    msg = decide_failover(source="webhook:s2")
    return jsonify({"message": "Server2 status updated", "server1Status": app_state.server1_up, "server2Status": is_up, "info": msg})


# --------------
# Initialize cache
# --------------
load_dns_records()

# --------------
# Run dev server
# --------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
