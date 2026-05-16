# Setup do OSRM self-hosted pra RMBH (Região Metropolitana de Belo Horizonte).
#
# O Geofabrik não publica PBF por estado do Brasil — só por região (sudeste,
# nordeste, etc.) ou Brasil inteiro. Estratégia: baixa sudeste-latest (~340MB
# uma vez), recorta o bbox da RMBH com osmium-tool (~50-80MB), roda OSRM no
# arquivo cortado (leve e rápido).
#
# Roda UMA VEZ pra preparar os arquivos. Depois sobe o servidor via:
#     cd osrm; docker compose up -d
#
# Pré-requisito: Docker Desktop instalado e rodando.
#
# Etapas (tempo total: ~10-15min na primeira vez):
#   1. Build da imagem local/osmium-tool (~1min, primeira vez só)
#   2. Baixar sudeste-latest da Geofabrik (~340MB)
#   3. osmium extract — corta bbox da RMBH (~1-2min)
#   4. osrm-extract — converte PBF cortado em formato OSRM (~2-5min)
#   5. osrm-partition — particiona o grafo (MLD) (~30s)
#   6. osrm-customize — otimiza pra queries (~30s)
#
# Saída: arquivos `osrm/data/rmbh.osrm*` (~150-250MB total)

$ErrorActionPreference = "Stop"

$DATA_DIR    = Join-Path $PSScriptRoot "data"
$SUDESTE     = Join-Path $DATA_DIR "sudeste-latest.osm.pbf"
$RMBH_PBF    = Join-Path $DATA_DIR "rmbh.osm.pbf"
$SUDESTE_URL = "https://download.geofabrik.de/south-america/brazil/sudeste-latest.osm.pbf"

# Bbox da RMBH (oeste, sul, leste, norte). Cobre BH + Contagem + Betim +
# Nova Lima + Sabará + Vespasiano + Lagoa Santa + Ribeirão das Neves + Santa
# Luzia + Ibirité e arredores. Folga generosa pra entregas no entorno.
$BBOX = "-44.4,-20.3,-43.5,-19.4"

# 1. Verifica Docker
try {
    docker version --format '{{.Server.Version}}' | Out-Null
} catch {
    Write-Host "Docker nao esta rodando. Inicie o Docker Desktop e tente de novo." -ForegroundColor Red
    exit 1
}

# 2. Cria data dir
if (-not (Test-Path $DATA_DIR)) {
    New-Item -ItemType Directory $DATA_DIR -Force | Out-Null
}

# 3. Garante que a imagem local/osmium-tool existe
$osmiumImg = docker images -q local/osmium-tool
if (-not $osmiumImg) {
    Write-Host "Buildando imagem local/osmium-tool (uma vez)..." -ForegroundColor Cyan
    docker build -t local/osmium-tool -f (Join-Path $PSScriptRoot "osmium.Dockerfile") $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { Write-Host "Falhou no build do osmium-tool" -ForegroundColor Red; exit 1 }
} else {
    Write-Host "Imagem local/osmium-tool ja existe"
}

# 4. Baixa sudeste se nao tem (valida que e PBF, nao HTML de erro)
$precisaBaixar = $true
if (Test-Path $SUDESTE) {
    $tam = (Get-Item $SUDESTE).Length
    if ($tam -gt 100MB) {
        Write-Host "Sudeste-latest ja baixado ($([math]::Round($tam/1MB,1)) MB)"
        $precisaBaixar = $false
    } else {
        Write-Host "Arquivo sudeste-latest corrompido/incompleto ($tam bytes), rebaixando..." -ForegroundColor Yellow
        Remove-Item $SUDESTE
    }
}
if ($precisaBaixar) {
    Write-Host "Baixando sudeste-latest.osm.pbf (~340MB)..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $SUDESTE_URL -OutFile $SUDESTE -UseBasicParsing
    $tam = (Get-Item $SUDESTE).Length
    if ($tam -lt 100MB) {
        Write-Host "Download falhou: arquivo so tem $tam bytes (esperado >300MB). Geofabrik pode ter mudado o caminho." -ForegroundColor Red
        exit 1
    }
    Write-Host "Mapa baixado ($([math]::Round($tam/1MB,1)) MB)"
}

# 5. Corta bbox da RMBH (rapido, ~1-2min). Pula se ja foi feito.
if (Test-Path $RMBH_PBF) {
    Write-Host "rmbh.osm.pbf ja existe (delete pra refazer)"
} else {
    Write-Host "Recortando bbox da RMBH ($BBOX)..." -ForegroundColor Cyan
    docker run --rm -v "${DATA_DIR}:/data" local/osmium-tool `
        extract --bbox=$BBOX /data/sudeste-latest.osm.pbf -o /data/rmbh.osm.pbf --overwrite
    if ($LASTEXITCODE -ne 0) { Write-Host "Falhou no osmium extract" -ForegroundColor Red; exit 1 }
    $tam = (Get-Item $RMBH_PBF).Length
    Write-Host "PBF recortado: $([math]::Round($tam/1MB,1)) MB"
}

# 6. osrm-extract. Pula se ja foi feito (arquivo .osrm.nbg existe).
$NBG = Join-Path $DATA_DIR "rmbh.osrm.nbg"
if (Test-Path $NBG) {
    Write-Host "osrm-extract ja feito (delete os arquivos rmbh.osrm* pra refazer)"
} else {
    Write-Host "osrm-extract (~2-5min)..." -ForegroundColor Cyan
    docker run --rm -v "${DATA_DIR}:/data" osrm/osrm-backend `
        osrm-extract -p /opt/car.lua /data/rmbh.osm.pbf
    if ($LASTEXITCODE -ne 0) { Write-Host "Falhou em osrm-extract" -ForegroundColor Red; exit 1 }
}

# 7. osrm-partition
Write-Host "osrm-partition (~30s)..." -ForegroundColor Cyan
docker run --rm -v "${DATA_DIR}:/data" osrm/osrm-backend `
    osrm-partition /data/rmbh.osrm
if ($LASTEXITCODE -ne 0) { Write-Host "Falhou em osrm-partition" -ForegroundColor Red; exit 1 }

# 8. osrm-customize
Write-Host "osrm-customize (~30s)..." -ForegroundColor Cyan
docker run --rm -v "${DATA_DIR}:/data" osrm/osrm-backend `
    osrm-customize /data/rmbh.osrm
if ($LASTEXITCODE -ne 0) { Write-Host "Falhou em osrm-customize" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "OSRM pronto." -ForegroundColor Green
Write-Host ""
Write-Host "Proximos passos:"
Write-Host "  1. Sobe o servidor:"
Write-Host "       cd osrm; docker compose up -d"
Write-Host "  2. Aponta o app pra ele antes de rodar:"
Write-Host "       `$env:OSRM_URL = 'http://localhost:5001'"
Write-Host "       python app.py"
Write-Host ""
Write-Host "Pra atualizar o mapa (OSM evolui, recomendado a cada 1-3 meses):"
Write-Host "  1. Delete $DATA_DIR\sudeste-latest.osm.pbf  e  $DATA_DIR\rmbh.osm.pbf"
Write-Host "  2. Rode esse script de novo"
