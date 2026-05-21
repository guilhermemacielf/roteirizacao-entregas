#!/usr/bin/env bash
# Setup completo do servidor (Ubuntu 22.04/24.04) pra rodar a roteirização.
# Roda UMA vez num VPS limpo. Idempotente — pode rodar de novo sem quebrar.
#
# Uso:
#   1. SSH no servidor como root
#   2. Coloca o código em /opt/roteirizacao (git clone ou scp)
#   3. cd /opt/roteirizacao && bash deploy/setup-vps.sh
#
# Depois do setup: configura secrets e sobe (ver deploy/GUIA.md).

set -euo pipefail

PROJETO_DIR="${PROJETO_DIR:-/opt/roteirizacao}"

echo "==> 1/4 Instalando Docker + plugin compose"
if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl gnupg git
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
else
    echo "    Docker já instalado, pulando."
fi

echo "==> 2/4 Pré-baixando imagem OSRM (evita timeout no 1º up)"
docker pull osrm/osrm-backend:latest

echo "==> 3/4 Processando mapa OSRM da RMBH (baixa sudeste ~800MB, ~10-15min)"
cd "$PROJETO_DIR/osrm"
if [ -f data/rmbh.osrm ]; then
    echo "    Mapa já processado (data/rmbh.osrm existe), pulando."
else
    chmod +x setup.sh
    ./setup.sh
fi

echo "==> 4/4 Configurando firewall (libera SSH + HTTP)"
if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH || true
    ufw allow 80/tcp || true
    ufw --force enable || true
fi

echo ""
echo "============================================================"
echo "✓ Servidor pronto. Próximos passos (ver deploy/GUIA.md):"
echo "  1. cd $PROJETO_DIR"
echo "  2. Copiar oauth_client.json pra raiz do projeto"
echo "  3. Criar .env com:  GOOGLE_MAPS_API_KEY=AIza..."
echo "  4. docker compose -f docker-compose.prod.yml up -d --build"
echo "  5. Acessar http://SEU_IP no navegador"
echo "============================================================"
