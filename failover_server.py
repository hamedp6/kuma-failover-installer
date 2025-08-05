import os
import json
import logging
import requests
from flask import Flask, request
from dotenv import load_dotenv

# Load .env file if exists
load_dotenv()

# 🔹 Config
CLOUDFLARE_ZONE_ID = os.getenv("CLOUDFLARE_ZONE_ID", "zone_ID")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "TOKEN_API")
DNS_RECORD_NAMES = ["iran.agdpks.com", "iran1.agdpks.com", "iran2.agdpks.com"]

SERVER1_IP = os.getenv("SERVER1_IP", "server1_ip")
SERVER2_IP = os.getenv("SERVER2_IP", "backup_ip")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "telegram_BOT_token")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "chat_id")

LOG_FILE = "./failover.log"
STATE_FILE = "./failover-state.json"

app = Flask(__name__)
cached_records = []

# 📌 Logging
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='[%(asctime)s] %(message)s')
logger = logging.getLogger()

def log_event(message):
    logger.info(message)
    print(message)
    return message

# 📌 Telegram
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        log_event(f"❌ Failed to send Telegram message: {e}")

# 📌 Load & Save State
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"server1Status": True, "server2Status": True, "currentDNS": SERVER1_IP}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

state = load_state()
server1_status = state["server1Status"]
server2_status = state["server2Status"]
current_dns = state["currentDNS"]

# 📌 Load DNS Records
def load_dns_records():
    global cached_records
    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}/dns_records"
    response = requests.get(url, headers=headers)
    data = response.json()
    cached_records = [r for r in data["result"] if r["name"] in DNS_RECORD_NAMES]
    print("✅ Cached DNS Records:", [r["name"] for r in cached_records])

# 📌 Update DNS
def update_dns(new_ip):
    global current_dns
    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json"
    }

    for record in cached_records:
        url = f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}/dns_records/{record['id']}"
        payload = {
            "type": record["type"],
            "name": record["name"],
            "content": new_ip,
            "ttl": 60,
            "proxied": record["proxied"]
        }
        requests.put(url, headers=headers, json=payload)

    current_dns = new_ip
    save_state({"server1Status": server1_status, "server2Status": server2_status, "currentDNS": current_dns})

# 📌 Decide Failover
def decide_failover():
    global current_dns
    new_ip = current_dns

    if server1_status:
        new_ip = SERVER1_IP
    elif server2_status:
        new_ip = SERVER2_IP

    if new_ip != current_dns:
        update_dns(new_ip)
        msg = f"🔄 DNS switched to {new_ip}\n📡 SHATEL={'UP' if server1_status else 'DOWN'}, OMID={'UP' if server2_status else 'DOWN'}"
        log = log_event(msg)
        send_telegram_message(log)
    else:
        msg = f"ℹ️ No DNS change. SHATEL={'UP' if server1_status else 'DOWN'}, OMID={'UP' if server2_status else 'DOWN'} | Current DNS: {current_dns}"
        log = log_event(msg)
        send_telegram_message(log)
        save_state({"server1Status": server1_status, "server2Status": server2_status, "currentDNS": current_dns})

# 📌 Webhooks with error handling
@app.route("/webhook/server1", methods=["POST"])
def webhook_server1():
    global server1_status
    try:
        data = request.get_json(force=True)
        log_event(f"🔍 Incoming payload to /webhook/server1: {data}")

        hb = data.get("heartbeat")
        if isinstance(hb, dict):
            server1_status = hb.get("status") == 1
            decide_failover()
            return {
                "message": "✅ Server1 status updated",
                "server1Status": server1_status,
                "server2Status": server2_status
            }
        else:
            log_event("⚠️ Invalid heartbeat format. Skipping failover logic.")
            return {"warning": "Invalid heartbeat payload"}, 400

    except Exception as e:
        log_event(f"❌ Server1 webhook error: {e}")
        return {"error": "Internal server error"}, 500


@app.route("/webhook/server2", methods=["POST"])
def webhook_server2():
    global server2_status
    try:
        data = request.get_json(force=True)
        server2_status = data.get("heartbeat", {}).get("status") == 1
        decide_failover()
        return {
            "message": "✅ Server2 status updated",
            "server1Status": server1_status,
            "server2Status": server2_status
        }
    except Exception as e:
        log_event(f"❌ Server2 webhook error: {e}")
        return {"error": "Internal server error"}, 500

# 📌 Status Endpoint
@app.route("/status", methods=["GET"])
def status():
    return {
        "currentDNS": current_dns,
        "server1Status": "UP" if server1_status else "DOWN",
        "server2Status": "UP" if server2_status else "DOWN",
        "dnsRecords": [r["name"] for r in cached_records]
    }

# 📌 Root Endpoint
@app.route("/", methods=["GET"])
def index():
    return {
        "message": "✅ Kuma Failover Server is running.",
        "availableEndpoints": ["/webhook/server1", "/webhook/server2", "/status"]
    }

# 📌 Start Server
if __name__ == "__main__":
    print("🚀 Starting Failover Server...")
    try:
        load_dns_records()
        app.run(host="0.0.0.0", port=5000)
    except Exception as e:
        log_event(f"❌ Error on startup: {e}")
