#!/bin/bash
# Verifica se o LucShark está saudável na VM Oracle
set -euo pipefail

echo "=== LucShark — verificação ==="
echo ""

echo "--- systemctl ---"
systemctl is-active lucshark && echo "Serviço: ATIVO" || echo "Serviço: INATIVO"

echo ""
echo "--- health local ---"
curl -sf http://127.0.0.1:8080/health || echo "ERRO: /health não respondeu"

echo ""
echo ""
echo "--- IP público (cole no JJ webhook) ---"
curl -sf ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'
echo ""
echo "URL webhook JJ: http://$(curl -sf ifconfig.me 2>/dev/null || echo 'SEU_IP'):8080/api/jj/sinal"

echo ""
echo "--- últimos logs ---"
journalctl -u lucshark -n 8 --no-pager
