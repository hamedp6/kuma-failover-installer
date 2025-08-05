#!/bin/bash

echo "==============================="
echo "🌐 Kuma DNS Failover Installer"
echo "==============================="

# === Step 1: Ask for user input ===
read -p "📌 Enter your Cloudflare API Token: " CLOUDFLARE_API_TOKEN
read -p "📌 Enter your Cloudflare Zone ID: " CLOUDFLARE_ZONE_ID
read -p "📌 Enter your DNS record names (comma-separated): " DNS_RECORDS
read -p "📡 Enter Server 1 IP (Primary): " SERVER1_IP
read -p "📡 Enter Server 2 IP (Backup): " SERVER2_IP

read -p "🤖 Enter Telegram Bot Token (optional): " TELEGRAM_BOT_TOKEN
read -p "💬 Enter Telegram Chat ID (optional): " TELEGRAM_CHAT_ID

INSTALL_DIR="$HOME/kuma-failover"

# === Step 2: Create directory ===
echo "📁 Creating project directory at $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# === Step 3: Save .env file ===
echo "📝 Creating .env configuration..."
cat > .env <<EOF
CLOUDFLARE_API_TOKEN=$CLOUDFLARE_API_TOKEN
CLOUDFLARE_ZONE_ID=$CLOUDFLARE_ZONE_ID
DNS_RECORD_NAMES=$(echo "$DNS_RECORDS" | sed 's/ //g')
SERVER1_IP=$SERVER1_IP
SERVER2_IP=$SERVER2_IP
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
EOF

# === Step 4: Install system dependencies ===
echo "📦 Installing system packages..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv curl

# === Step 5: Set up Python virtualenv ===
echo "🐍 Setting up virtual environment..."
python3 -m venv venv
source venv/bin/activate

# === Step 6: Download failover_server.py ===
echo "⬇️ Downloading server script..."
curl -sSLo failover_server.py https://raw.githubusercontent.com/hamedp6/kuma-failover-installer
/main/failover_server.py

# === Step 7: Install Python packages ===
pip install flask requests python-dotenv

# === Step 8: Create systemd service ===
echo "⚙️ Setting up systemd service..."
SERVICE_NAME=kuma-failover
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME.service"

sudo bash -c "cat > $SERVICE_PATH" <<EOF
[Unit]
Description=Kuma DNS Failover Flask Server
After=network.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/failover_server.py
Restart=always
User=$USER
EnvironmentFile=$INSTALL_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

# === Step 9: Enable + Start service ===
echo "🚀 Starting failover service..."
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl start $SERVICE_NAME
sudo systemctl status $SERVICE_NAME --no-pager

echo "✅ Installation complete!"
echo "🌍 Your Failover Server should now be running on http://<your-server-ip>:5000"
echo "➡️ Check status:    curl http://localhost:5000/status"
echo "➡️ Logs:            journalctl -u $SERVICE_NAME -f"
