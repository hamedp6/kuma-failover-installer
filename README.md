# Kuma Failover Installer

This is a **one-click installer** for setting up an automatic DNS failover system using [Uptime Kuma](https://github.com/louislam/uptime-kuma) and [Cloudflare DNS](https://www.cloudflare.com/dns/).

When one of your servers goes down, this script will automatically update your Cloudflare DNS record to point to the backup IP, and notify you via Telegram.

---

## 📦 What It Does

- Installs Node.js, npm, and required packages (`express`, `node-fetch@2`, `dotenv`)
- Accepts your Cloudflare API token, zone ID, domain names, and IPs
- Creates `.env` and `server.js` files
- Creates a `systemd` service to auto-start on boot
- Logs DNS switch events and sends messages to your Telegram bot

---

## 🚀 One-click Installation

```bash
bash <(curl -s https://raw.githubusercontent.com/hamedp6/kuma-failover-installer/main/install.sh)
```

or

```bash
bash <(wget -qO- https://raw.githubusercontent.com/hamedp6/kuma-failover-installer/main/install.sh)
```

---

## 🧾 Requirements

- Ubuntu 22.04+ server
- Uptime Kuma installed on two different monitoring servers
- Cloudflare account (with API token that has DNS edit access)
- (Optional) Telegram Bot and Chat ID

---

## 📝 Usage

- Kuma 1 should send webhook to: `http://your-failover-server:5000/webhook/server1`
- Kuma 2 should send webhook to: `http://your-failover-server:5000/webhook/server2`

---

## 📂 Files

- `install.sh`: Installer script
- `server.js`: Created during installation
- `.env`: Config created based on your answers
- `/etc/systemd/system/kuma-failover.service`: auto-start service

---

