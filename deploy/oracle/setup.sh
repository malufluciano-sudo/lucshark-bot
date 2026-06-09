#!/bin/bash
# LucShark Bot — instalação Oracle Cloud Always Free (Ubuntu ARM)
# Uso: sudo bash setup.sh
set -euo pipefail

REPO_URL="https://github.com/malufluciano-sudo/lucshark-bot.git"
APP_DIR="/opt/lucshark-bot"
SERVICE_NAME="lucshark"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Execute como root: sudo bash setup.sh"
  exit 1
fi

echo "=== LucShark Bot — Oracle Cloud setup ==="

apt-get update -qq
apt-get install -y -qq git python3 python3-pip python3-venv

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Clonando repositório..."
  git clone "$REPO_URL" "$APP_DIR"
else
  echo "Atualizando repositório..."
  cd "$APP_DIR"
  git pull --ff-only origin main
fi

cd "$APP_DIR"
chown -R ubuntu:ubuntu "$APP_DIR"

if [[ ! -d "$APP_DIR/venv" ]]; then
  echo "Criando ambiente Python..."
  sudo -u ubuntu python3 -m venv "$APP_DIR/venv"
fi

echo "Instalando dependências (pode levar 2-3 min)..."
sudo -u ubuntu "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u ubuntu "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$SCRIPT_DIR/env.template" "$APP_DIR/.env"
  chown ubuntu:ubuntu "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo ""
  echo ">>> IMPORTANTE: edite o .env e cole o TELEGRAM_TOKEN:"
  echo "    sudo nano $APP_DIR/.env"
  echo ""
  read -r -p "Pressione ENTER depois de salvar o .env (Ctrl+O, Enter, Ctrl+X no nano)..."
fi

if grep -q "COLE_SEU_TOKEN_AQUI" "$APP_DIR/.env" 2>/dev/null; then
  echo "ERRO: TELEGRAM_TOKEN ainda não foi configurado em $APP_DIR/.env"
  exit 1
fi

cp "$SCRIPT_DIR/lucshark.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo ""
  echo "=== SUCESSO: LucShark ONLINE ==="
  systemctl status "$SERVICE_NAME" --no-pager -l | head -15
  echo ""
  echo "Logs ao vivo:  sudo journalctl -u lucshark -f"
  echo "Reiniciar:     sudo systemctl restart lucshark"
  echo "Health local:  curl -s http://127.0.0.1:8080/health"
else
  echo "ERRO: serviço não subiu. Veja: sudo journalctl -u lucshark -n 50"
  exit 1
fi
