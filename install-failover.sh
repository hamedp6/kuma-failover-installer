#!/bin/bash

set -e  # Exit on error
clear

echo "=========================================="
echo " 🚀 Kuma DNS Failover Auto-Installer"
echo "=========================================="

# === Ask for required Cloudflare info ===
read -p "📌 Cloudflare API Token: " CLOUDFLARE_API_TOKEN
read -p "📌 Cloudflare Zone ID: " CLOUDFLARE_ZONE_ID
read -p "🌐 DNS record names (comma-separated): " DNS_RECORDS
read -p "🖥️  Primary Server IP (SERVER1_IP): " SERVER1_IP
read -p "🖥️  Backup Server IP (SERVER2_IP): " SERVER2_IP

# === Optional Telegram config ===
read -p "🤖 Telegram Bot Token (optional): " TELEGRAM_BOT_TOKEN
read -p "💬 Telegram Chat ID (optional): " TELEGRAM_CHAT_ID

INSTALL_DIR="$HOME/kuma-failover"
SERVICE_NAME="kuma-failover"

# === Create project directory ===
echo "📁 Creating directory at $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# === Save .env file ===
echo "📝 Creating .env file..."
cat <<EOF > .env
CLOUDFLARE_API_TOKEN=$CLOUDFLARE_API_TOKEN
CLOUDFLARE_ZONE_ID=$CLOUDFLARE_ZONE_ID
DNS_RECORD_NAMES=$(echo "$DNS_RECORDS" | sed 's/ //g')
SERVER1_IP=$SERVER1_IP
SERVER2_IP=$SERVER2_IP
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
EOF

# === Install system packages ===
echo "📦 Installing Python + dependencies..."
sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv curl git

# === Create Python virtual environment ===
echo "🐍 Setting up virtual environment..."
python3 -m venv venv
source venv/bin/activate

# === Download the Flask failover script ===
echo "⬇️ Downloading failover_server.py..."
curl -sSLo failover_server.py https://raw.githubusercontent.com/hamedp6/kuma-failover-installer
/main/failover_server.py

# === Install Python packages ===
pip install --upgrade pip
pip install flask requests python-dotenv

# === Create systemd service ===
echo "⚙️ Creating systemd service..."
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME.service"

sudo tee "$SERVICE_PATH" > /dev/null <<EOF
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

# === Enable and start the service ===
echo "🚀 Enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# === Make script executable (if saved locally) ===
chmod +x "$INSTALL_DIR/failover_server.py"

# === Done ===
echo ""
echo "✅ Installation complete!"
echo "🌍 Failover server is now running on: http://$(hostname -I | awk '{print $1}'):5000"
echo "➡️  Test endpoint:  curl http://localhost:5000/status"
echo "📄 Logs:           journalctl -u $SERVICE_NAME -f"
