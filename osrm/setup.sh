#!/usr/bin/env bash
# Setup do OSRM self-hosted pra RMBH (Região Metropolitana de Belo Horizonte) — Linux/Mac.
#
# O Geofabrik não publica PBF por estado do Brasil — só por região (sudeste,
# nordeste, etc.) ou Brasil inteiro. Estratégia: baixa sudeste-latest (~340MB
# uma vez), recorta o bbox da RMBH com osmium-tool (~50-80MB), roda OSRM no
# arquivo cortado (leve e rápido).
#
# Roda UMA VEZ pra preparar os arquivos. Depois sobe o servidor via:
#     cd osrm && docker compose up -d
#
# Pré-requisito: Docker instalado e rodando.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
SUDESTE="$DATA_DIR/sudeste-latest.osm.pbf"
RMBH_PBF="$DATA_DIR/rmbh.osm.pbf"
SUDESTE_URL="https://download.geofabrik.de/south-america/brazil/sudeste-latest.osm.pbf"
# Bbox RMBH: oeste,sul,leste,norte
BBOX="-44.4,-20.3,-43.5,-19.4"

if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    echo "Docker não está rodando." >&2
    exit 1
fi

mkdir -p "$DATA_DIR"

# 1. Imagem osmium-tool (build uma vez)
if [ -z "$(docker images -q local/osmium-tool)" ]; then
    echo "Buildando local/osmium-tool (uma vez)..."
    docker build -t local/osmium-tool -f "$SCRIPT_DIR/osmium.Dockerfile" "$SCRIPT_DIR"
fi

# 2. Baixa sudeste (valida tamanho — Geofabrik retorna HTML pequeno se path inválido)
if [ -f "$SUDESTE" ] && [ "$(stat -c%s "$SUDESTE" 2>/dev/null || stat -f%z "$SUDESTE")" -gt 104857600 ]; then
    echo "Sudeste-latest já baixado"
else
    [ -f "$SUDESTE" ] && rm "$SUDESTE"
    echo "Baixando sudeste-latest.osm.pbf (~340MB)..."
    curl -L --fail -o "$SUDESTE" "$SUDESTE_URL"
    tam=$(stat -c%s "$SUDESTE" 2>/dev/null || stat -f%z "$SUDESTE")
    if [ "$tam" -lt 104857600 ]; then
        echo "Download falhou: arquivo só tem $tam bytes (esperado >300MB)" >&2
        exit 1
    fi
fi

# 3. Recorta RMBH
if [ -f "$RMBH_PBF" ]; then
    echo "rmbh.osm.pbf já existe (delete pra refazer)"
else
    echo "Recortando bbox RMBH ($BBOX)..."
    docker run --rm -v "$DATA_DIR:/data" local/osmium-tool \
        extract --bbox=$BBOX /data/sudeste-latest.osm.pbf -o /data/rmbh.osm.pbf --overwrite
fi

# 4. osrm-extract
if [ -f "$DATA_DIR/rmbh.osrm.nbg" ]; then
    echo "osrm-extract já feito"
else
    echo "osrm-extract (~2-5min)..."
    docker run --rm -v "$DATA_DIR:/data" osrm/osrm-backend \
        osrm-extract -p /opt/car.lua /data/rmbh.osm.pbf
fi

echo "osrm-partition..."
docker run --rm -v "$DATA_DIR:/data" osrm/osrm-backend \
    osrm-partition /data/rmbh.osrm

echo "osrm-customize..."
docker run --rm -v "$DATA_DIR:/data" osrm/osrm-backend \
    osrm-customize /data/rmbh.osrm

cat <<EOF

OSRM pronto.

Próximos passos:
  1. Sobe o servidor:
       cd osrm && docker compose up -d
  2. Aponta o app pra ele antes de rodar:
       export OSRM_URL=http://localhost:5001
       python app.py

Pra atualizar o mapa (a cada 1-3 meses):
  1. rm $DATA_DIR/sudeste-latest.osm.pbf $DATA_DIR/rmbh.osm.pbf
  2. Rode esse script de novo
EOF
