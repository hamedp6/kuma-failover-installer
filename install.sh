#!/bin/bash

echo "🔧 Uptime Kuma Failover Installer for Ubuntu 22.04"

if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Please run this script as root."
    exit 1
fi

read -p "Enter your Cloudflare API Token: " CLOUDFLARE_API_TOKEN
read -p "Enter your Cloudflare Zone ID: " CLOUDFLARE_ZONE_ID
read -p "Enter the domains to update (comma-separated, e.g., example.com,www.example.com): " DNS_RECORD_NAMES
read -p "Enter the PRIMARY IP address: " SERVER1_IP
read -p "Enter the BACKUP IP address: " SERVER2_IP
read -p "Enter your Telegram Bot Token (or leave blank to skip): " TELEGRAM_BOT_TOKEN
read -p "Enter your Telegram Chat ID (or leave blank to skip): " TELEGRAM_CHAT_ID

echo "📦 Installing Node.js and dependencies..."
apt update && apt install -y curl git nodejs npm

curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

mkdir -p /opt/kuma-failover
cd /opt/kuma-failover
npm init -y
npm install express node-fetch@2 dotenv

echo "🔐 Writing .env file..."
cat <<EOF > .env
CLOUDFLARE_API_TOKEN=${CLOUDFLARE_API_TOKEN}
CLOUDFLARE_ZONE_ID=${CLOUDFLARE_ZONE_ID}
SERVER1_IP=${SERVER1_IP}
SERVER2_IP=${SERVER2_IP}
DNS_RECORD_NAMES=${DNS_RECORD_NAMES}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
EOF

echo "📝 Writing server.js..."
cat <<'EOF' > server.js

const express = require("express");
const fetch = require("node-fetch");
const fs = require("fs");
require("dotenv").config();

const app = express();
app.use(express.json());

// 🔹 Config
const CLOUDFLARE_ZONE_ID = process.env.CLOUDFLARE_ZONE_ID;
const CLOUDFLARE_API_TOKEN = process.env.CLOUDFLARE_API_TOKEN;
const DNS_RECORD_NAMES = process.env.DNS_RECORD_NAMES.split(",");

const SERVER1_IP = process.env.SERVER1_IP;
const SERVER2_IP = process.env.SERVER2_IP;

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;

const LOG_FILE = "./failover.log";
const STATE_FILE = "./failover-state.json";

let cachedRecords = [];

function loadState() {
  if (fs.existsSync(STATE_FILE)) {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
  }
  return { server1Status: true, server2Status: true, currentDNS: SERVER1_IP };
}

function saveState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

let { server1Status, server2Status, currentDNS } = loadState();

function logEvent(message) {
  const timestamp = new Date().toISOString();
  const logMessage = `[\${timestamp}] \${message}\n`;
  fs.appendFileSync(LOG_FILE, logMessage);
  console.log(logMessage.trim());
  return logMessage;
}

async function sendTelegramMessage(text) {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) return;
  const url = `https://api.telegram.org/bot\${TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: TELEGRAM_CHAT_ID, text }),
  });
}

async function loadDNSRecords() {
  const url = `https://api.cloudflare.com/client/v4/zones/\${CLOUDFLARE_ZONE_ID}/dns_records`;
  const res = await fetch(url, {
    method: "GET",
    headers: {
      "Authorization": `Bearer \${CLOUDFLARE_API_TOKEN}`,
      "Content-Type": "application/json",
    },
  });
  const data = await res.json();
  cachedRecords = data.result.filter(r => DNS_RECORD_NAMES.includes(r.name));
  console.log("✅ Cached DNS Records:", cachedRecords.map(r => r.name));
}

async function updateDNS(newIP) {
  for (const record of cachedRecords) {
    const url = `https://api.cloudflare.com/client/v4/zones/\${CLOUDFLARE_ZONE_ID}/dns_records/\${record.id}`;
    await fetch(url, {
      method: "PUT",
      headers: {
        "Authorization": `Bearer \${CLOUDFLARE_API_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        type: record.type,
        name: record.name,
        content: newIP,
        ttl: 60,
        proxied: record.proxied,
      }),
    });
  }
  currentDNS = newIP;
  saveState({ server1Status, server2Status, currentDNS });
}

async function decideFailover() {
  let newIP = currentDNS;

  if (server1Status) {
    newIP = SERVER1_IP;
  } else if (!server1Status && server2Status) {
    newIP = SERVER2_IP;
  } else if (!server1Status && !server2Status) {
    newIP = currentDNS;
  }

  if (newIP !== currentDNS) {
    await updateDNS(newIP);
    const msg = `🔄 DNS switched to \${newIP}\n📡 Status: S1=\${server1Status ? "UP" : "DOWN"}, S2=\${server2Status ? "UP" : "DOWN"}`;
    const log = logEvent(msg);
    await sendTelegramMessage(log);
  } else {
    const msg = `ℹ️ No DNS change. Status: S1=\${server1Status ? "UP" : "DOWN"}, S2=\${server2Status ? "UP" : "DOWN"} | Current DNS: \${currentDNS}`;
    const log = logEvent(msg);
    await sendTelegramMessage(log);
    saveState({ server1Status, server2Status, currentDNS });
  }
}

app.post("/webhook/server1", async (req, res) => {
  server1Status = req.body?.heartbeat?.status === 1;
  await decideFailover();
  res.json({ message: "Server1 status updated", server1Status, server2Status });
});

app.post("/webhook/server2", async (req, res) => {
  server2Status = req.body?.heartbeat?.status === 1;
  await decideFailover();
  res.json({ message: "Server2 status updated", server1Status, server2Status });
});

app.listen(5000, async () => {
  console.log("🚀 Starting Failover Server...");
  await loadDNSRecords();
  console.log("✅ Failover server running on port 5000");
});

EOF

echo "🛠️ Creating systemd service..."
cat <<EOF > /etc/systemd/system/kuma-failover.service
[Unit]
Description=Uptime Kuma Failover Script
After=network.target

[Service]
ExecStart=/usr/bin/node /opt/kuma-failover/server.js
Restart=always
WorkingDirectory=/opt/kuma-failover
EnvironmentFile=/opt/kuma-failover/.env
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kuma-failover
systemctl start kuma-failover

echo "✅ Installed and running!"
echo "👉 Use journalctl -u kuma-failover -f to follow logs."
