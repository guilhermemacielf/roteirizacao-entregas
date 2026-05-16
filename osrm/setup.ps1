# Setup do OSRM self-hosted pra Minas Gerais.
#
# Roda UMA VEZ pra preparar os arquivos. Depois sobe o servidor via:
#     cd osrm; docker compose up -d
#
# Pré-requisito: Docker Desktop instalado e rodando.
#
# Etapas (tempo total: ~15-30min na primeira vez):
#   1. Baixar mapa de MG da Geofabrik (~150MB)
#   2. extract — converte OSM PBF em formato OSRM (10-20min, RAM ~2-4GB)
#   3. partition — particiona o grafo (Multi-Level Dijkstra) (~2min)
#   4. customize — otimiza pra queries (~2min)
#
# Saída: arquivos `osrm/data/minas-gerais-latest.osrm*` (~500MB total)

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $PSScriptRoot
$DATA_DIR = Join-Path $PSScriptRoot "data"
$PBF      = Join-Path $DATA_DIR "minas-gerais-latest.osm.pbf"
$URL      = "https://download.geofabrik.de/south-america/brazil/sudeste/minas-gerais-latest.osm.pbf"

# 1. Verifica Docker
try {
    docker version --format '{{.Server.Version}}' | Out-Null
} catch {
    Write-Host "❌ Docker não está rodando. Inicie o Docker Desktop e tente de novo." -ForegroundColor Red
    exit 1
}

# 2. Cria data dir
if (-not (Test-Path $DATA_DIR)) {
    New-Item -ItemType Directory $DATA_DIR -Force | Out-Null
}

# 3. Baixa o mapa se ainda não tem
if (-not (Test-Path $PBF)) {
    Write-Host "⬇️  Baixando mapa de MG (~150MB)..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $URL -OutFile $PBF -UseBasicParsing
    Write-Host "✓ Mapa baixado em $PBF"
} else {
    Write-Host "✓ Mapa já existe em $PBF"
}

# 4. Extract — converte OSM em formato OSRM. Reseta arquivos .osrm* anteriores.
#    Pula se já foi feito (arquivo .osrm.nbg existe).
$NBG_FILE = Join-Path $DATA_DIR "minas-gerais-latest.osrm.nbg"
if (Test-Path $NBG_FILE) {
    Write-Host "✓ Extract já feito (delete os arquivos .osrm* pra refazer)"
} else {
    Write-Host "🔄 Extraindo (pode levar 10-20min, RAM ~2-4GB)..." -ForegroundColor Cyan
    docker run --rm -v "${DATA_DIR}:/data" osrm/osrm-backend `
        osrm-extract -p /opt/car.lua /data/minas-gerais-latest.osm.pbf
    if ($LASTEXITCODE -ne 0) { Write-Host "❌ Falhou em extract" -ForegroundColor Red; exit 1 }
}

# 5. Partition — particiona o grafo (MLD)
Write-Host "🔄 Particionando (~2min)..." -ForegroundColor Cyan
docker run --rm -v "${DATA_DIR}:/data" osrm/osrm-backend `
    osrm-partition /data/minas-gerais-latest.osrm
if ($LASTEXITCODE -ne 0) { Write-Host "❌ Falhou em partition" -ForegroundColor Red; exit 1 }

# 6. Customize — otimiza pra queries
Write-Host "🔄 Customizando (~2min)..." -ForegroundColor Cyan
docker run --rm -v "${DATA_DIR}:/data" osrm/osrm-backend `
    osrm-customize /data/minas-gerais-latest.osrm
if ($LASTEXITCODE -ne 0) { Write-Host "❌ Falhou em customize" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "✅ OSRM pronto." -ForegroundColor Green
Write-Host ""
Write-Host "Próximos passos:"
Write-Host "  1. Sobe o servidor:"
Write-Host "       cd osrm; docker compose up -d"
Write-Host "  2. Aponta o app pra ele antes de rodar:"
Write-Host "       `$env:OSRM_URL = 'http://localhost:5001'"
Write-Host "       python app.py"
Write-Host ""
Write-Host "Pra atualizar o mapa quando o OSM mudar (geralmente a cada 1-3 meses):"
Write-Host "  1. Delete `$DATA_DIR\minas-gerais-latest.osm.pbf"
Write-Host "  2. Rode esse script de novo"
