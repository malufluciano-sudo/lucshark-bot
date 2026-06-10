#!/bin/bash
# Atualiza código do GitHub e reinicia o bot
set -euo pipefail

APP_DIR="/opt/lucshark-bot"

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "ERRO: $APP_DIR não existe. Rode setup.sh primeiro."
  exit 1
fi

cd "$APP_DIR"
git pull --ff-only origin main
"$APP_DIR/venv/bin/pip" install -r requirements.txt -q
systemctl restart lucshark
sleep 2
systemctl is-active lucshark && echo "OK: lucshark reiniciado." || echo "ERRO: serviço não subiu."
