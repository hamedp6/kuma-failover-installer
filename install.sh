#!/usr/bin/env bash
# Ensure we're running under bash (not dash/sh)
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

set -euo pipefail

REPO_RAW_DEFAULT="https://raw.githubusercontent.com/hamedp6/kuma-failover-installer/main"
REPO_RAW="${REPO_RAW:-$REPO_RAW_DEFAULT}"

APP_NAME="failover"
APP_USER="failover"
APP_DIR="/opt/${APP_NAME}"
PYFILE="uptime-kuma-cloudflare-failover.py"
ENV_FILE="${APP_DIR}/.env"
VENV_DIR="${APP_DIR}/venv"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
PORT_DEFAULT=5000
TTL_DEFAULT=60
LOG_FILE="${APP_DIR}/failover.log"
STATE_FILE="${APP_DIR}/failover-state.json"

red()  { printf "\033[31m%s\033[0m\n" "$*"; }
grn()  { printf "\033[32m%s\033[0m\n" "$*"; }
ylw()  { printf "\033[33m%s\033[0m\n" "$*"; }
blu()  { printf "\033[34m%s\033[0m\n" "$*"; }

need_root() {
  if [[ $EUID -ne 0 ]]; then
    red "Please run as root (sudo)."
    exit 1
  fi
}

usage() {
  cat <<EOF
Usage: sudo ./install.sh [--uninstall] [--update] [--repo <raw_base_url>]
EOF
}

prompt() {
  local var="$1" prompt_text="$2" default="${3-}"
  # If exported env exists, use it and return
  if [[ -n "${!var:-}" ]]; then
    printf "%s" "${!var}"
    return
  fi
  local value=""
  if [[ -n "${default}" ]]; then
    read -r -p "${prompt_text} [${default}]: " value </dev/tty || true
    value="${value:-$default}"
  else
    while [[ -z "${value}" ]]; do
      read -r -p "${prompt_text}: " value </dev/tty || true
    done
  fi
  printf "%s" "$value"
}

gen_secret() {
  python3 - <<'PY' 2>/dev/null || true
import secrets; print(secrets.token_urlsafe(24))
PY
}

install_packages() {
  blu "Installing OS packages…"
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip ufw curl jq
}

create_user() {
  if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    blu "Creating system user ${APP_USER}…"
    useradd --system --create-home --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
}

setup_dirs() {
  blu "Creating ${APP_DIR}…"
  mkdir -p "${APP_DIR}"
  touch "${LOG_FILE}" "${STATE_FILE}"
  chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
}

create_venv() {
  blu "Creating Python venv…"
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install flask requests python-dotenv
}

download_app() {
  blu "Fetching ${PYFILE} from ${REPO_RAW}…"
  curl -fsSL "${REPO_RAW}/${PYFILE}" -o "${APP_DIR}/${PYFILE}"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/${PYFILE}"
}

write_env() {
  blu "Configuring environment (.env)…"

  CF_ZONE_ID=$(prompt CLOUDFLARE_ZONE_ID "Cloudflare Zone ID" "")
  CF_API_TOKEN=$(prompt CLOUDFLARE_API_TOKEN "Cloudflare API Token (DNS edit scope)" "")
  DNS_RECORDS=$(prompt DNS_RECORD_NAMES "Comma-separated FQDNs" "")
  SERVER1_IP=$(prompt SERVER1_IP "Server 1 IP" "")
  SERVER2_IP=$(prompt SERVER2_IP "Server 2 IP" "")
  PORT=$(prompt PORT "HTTP Port" "${PORT_DEFAULT}")
  TTL=$(prompt TTL "DNS TTL" "${TTL_DEFAULT}")
  ADMIN_USER_IN=$(prompt ADMIN_USER "Admin username for UI" "")
  ADMIN_PASS_IN=""
  if [[ -n "${ADMIN_USER_IN}" ]]; then
    ADMIN_PASS_IN=$(prompt ADMIN_PASS "Admin password for UI" "")
  fi
  TG_BOT=$(prompt TELEGRAM_BOT_TOKEN "Telegram Bot Token" "")
  TG_CHAT=$(prompt TELEGRAM_CHAT_ID "Telegram Chat ID" "")

  SECRET_KEY=$(gen_secret)
  [[ -z "${SECRET_KEY}" ]] && SECRET_KEY="$(head -c 16 /dev/urandom | xxd -p)"

  cat > "${ENV_FILE}" <<EOF
CLOUDFLARE_ZONE_ID=${CF_ZONE_ID}
CLOUDFLARE_API_TOKEN=${CF_API_TOKEN}
DNS_RECORD_NAMES=${DNS_RECORDS}
SERVER1_IP=${SERVER1_IP}
SERVER2_IP=${SERVER2_IP}
PORT=${PORT}
TTL=${TTL}
LOG_FILE=${LOG_FILE}
STATE_FILE=${STATE_FILE}
SECRET_KEY=${SECRET_KEY}
ADMIN_USER=${ADMIN_USER_IN}
ADMIN_PASS=${ADMIN_PASS_IN}
TELEGRAM_BOT_TOKEN=${TG_BOT}
TELEGRAM_CHAT_ID=${TG_CHAT}
EOF

  chown "${APP_USER}:${APP_USER}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
}

preseed_secret() {
  blu "Pre-seeding webhook secret…"
  if [[ -n "${WEBHOOK_SECRET:-}" ]]; then
    WH_SECRET="${WEBHOOK_SECRET}"
  else
    read -r -p "Webhook secret (leave empty to auto-generate): " WH_SECRET </dev/tty || true
  fi
  if [[ -z "${WH_SECRET}" ]]; then
    WH_SECRET="$(gen_secret)"
    [[ -z "${WH_SECRET}" ]] && WH_SECRET="$(head -c 24 /dev/urandom | base64 -w0)"
    ylw "Generated secret."
  fi
  echo "{\"webhook_secret\":\"${WH_SECRET}\"}" > "${STATE_FILE}"
  chown "${APP_USER}:${APP_USER}" "${STATE_FILE}"
  chmod 600 "${STATE_FILE}"
}

write_service() {
  blu "Writing systemd unit…"
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Uptime Kuma → Cloudflare Failover
After=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/${PYFILE}
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

enable_firewall() {
  read -r -p "Open port ${PORT} with UFW? [y/N]: " openufw </dev/tty || true
  if [[ "${openufw,,}" == "y" ]]; then
    ufw allow "${PORT}"/tcp || true
  fi
}

start_service() {
  blu "Starting service…"
  systemctl enable "${APP_NAME}" --now
  systemctl status --no-pager "${APP_NAME}" || true
}

main() {
  need_root
  exec </dev/tty || true
  install_packages
  create_user
  setup_dirs
  create_venv
  download_app
  write_env
  preseed_secret
  write_service
  enable_firewall
  start_service
  grn "Done."
}

main "$@"
