#!/usr/bin/env bash
# Setup do OSRM self-hosted pra Minas Gerais — Linux/Mac.
#
# Roda UMA VEZ pra preparar os arquivos. Depois sobe o servidor via:
#     cd osrm && docker compose up -d
#
# Pré-requisito: Docker instalado e rodando.
#
# Etapas (tempo total: ~15-30min na primeira vez):
#   1. Baixar mapa de MG da Geofabrik (~150MB)
#   2. extract — converte OSM PBF em formato OSRM (10-20min, RAM ~2-4GB)
#   3. partition — particiona o grafo (Multi-Level Dijkstra) (~2min)
#   4. customize — otimiza pra queries (~2min)

set -euo pipefail

DATA_DIR="$(cd "$(dirname "$0")" && pwd)/data"
PBF="$DATA_DIR/minas-gerais-latest.osm.pbf"
URL="https://download.geofabrik.de/south-america/brazil/sudeste/minas-gerais-latest.osm.pbf"

# 1. Verifica Docker
if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    echo "❌ Docker não está rodando." >&2
    exit 1
fi

mkdir -p "$DATA_DIR"

# 2. Baixa mapa se não tem
if [ ! -f "$PBF" ]; then
    echo "⬇️  Baixando mapa de MG (~150MB)..."
    curl -L --fail -o "$PBF" "$URL"
    echo "✓ Mapa baixado"
else
    echo "✓ Mapa já existe"
fi

# 3. Extract
if [ -f "$DATA_DIR/minas-gerais-latest.osrm.nbg" ]; then
    echo "✓ Extract já feito"
else
    echo "🔄 Extraindo (10-20min, RAM ~2-4GB)..."
    docker run --rm -v "$DATA_DIR:/data" osrm/osrm-backend \
        osrm-extract -p /opt/car.lua /data/minas-gerais-latest.osm.pbf
fi

# 4. Partition
echo "🔄 Particionando (~2min)..."
docker run --rm -v "$DATA_DIR:/data" osrm/osrm-backend \
    osrm-partition /data/minas-gerais-latest.osrm

# 5. Customize
echo "🔄 Customizando (~2min)..."
docker run --rm -v "$DATA_DIR:/data" osrm/osrm-backend \
    osrm-customize /data/minas-gerais-latest.osrm

cat <<EOF

✅ OSRM pronto.

Próximos passos:
  1. Sobe o servidor:
       cd osrm && docker compose up -d
  2. Aponta o app pra ele antes de rodar:
       export OSRM_URL=http://localhost:5001
       python app.py
EOF
