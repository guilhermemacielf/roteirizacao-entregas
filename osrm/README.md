# OSRM self-hosted (RMBH)

Servidor próprio de matriz de distância/tempo + rotas pra **Região
Metropolitana de Belo Horizonte**. Substitui o OSRM público
(`router.project-osrm.org`) que limita ~100 pontos por matriz — esse aqui
não tem limite e roda mais rápido por estar local.

## Quando usar

- Rodada diária com > 100 entregas (público recusa)
- Quer geometria real das ruas no mapa (não polyline reta)
- Quer eliminar dependência de serviço externo

## Por que recortar a RMBH em vez de baixar o estado

O Geofabrik **não publica PBF por estado do Brasil** — só por região
(sudeste, nordeste, etc.) ou Brasil inteiro. O setup baixa
`sudeste-latest.osm.pbf` (~340MB) uma vez e recorta o bbox da RMBH com
`osmium-tool`, gerando um PBF leve (~50-80MB) que o OSRM processa em
poucos minutos.

## Pré-requisito

- Docker Desktop instalado e rodando
- ~1GB de espaço livre (sudeste PBF + RMBH PBF + arquivos OSRM)
- ~1GB de RAM no `osrm-extract`

## Setup (uma vez)

**Windows (PowerShell):**
```powershell
cd osrm
./setup.ps1
```

**Linux/Mac:**
```bash
cd osrm
chmod +x setup.sh
./setup.sh
```

Etapas:
1. Build da imagem `local/osmium-tool` via `osmium.Dockerfile` (~1min)
2. Baixa `sudeste-latest.osm.pbf` da Geofabrik (~340MB)
3. `osmium extract --bbox=-44.4,-20.3,-43.5,-19.4` → `rmbh.osm.pbf` (~1-2min)
4. `osrm-extract` → formato OSRM (~2-5min)
5. `osrm-partition` (MLD) (~30s)
6. `osrm-customize` (~30s)

Saída: `data/rmbh.osrm*` (~150-250MB).

## Rodar o servidor

```bash
docker compose up -d
```

Escuta em `http://localhost:5001`. Logs com `docker compose logs -f osrm`.

Pra parar: `docker compose down`.

## Apontar a app pra ele

**Windows:**
```powershell
$env:OSRM_URL = "http://localhost:5001"
python app.py
```

**Linux/Mac:**
```bash
export OSRM_URL=http://localhost:5001
python app.py
```

A app loga `OSRM em http://localhost:5001` ao iniciar quando a env está
setada.

## Atualizar o mapa

OSM evolui. Pra pegar mudanças recentes (rua nova, número novo):

```bash
docker compose down
rm data/sudeste-latest.osm.pbf data/rmbh.osm.pbf
./setup.sh   # ou .ps1
docker compose up -d
```

Frequência razoável: a cada 1-3 meses.

## Aumentar a área coberta

Edita `$BBOX` no `setup.ps1` / `setup.sh` (formato `oeste,sul,leste,norte`).
Pra cobrir mais (ex: incluir Itabira, Sete Lagoas), expande o bbox e
deleta `data/rmbh.osm.pbf` + `data/rmbh.osrm*` antes de rerodar.

Se precisar do **sudeste inteiro** (cobrir viagens fora da RMBH), pula o
corte do osmium: aponta o `osrm-extract` direto pra `sudeste-latest.osm.pbf`
e ajusta os nomes em `docker-compose.yml`. Custo: ~30-45min de extract, ~5GB
de RAM, ~1.5GB de disco.

| Mapa | Tamanho PBF | Tempo extract |
|---|---|---|
| RMBH (bbox) | ~50-80MB | ~2-5min |
| Sudeste inteiro | ~340MB | ~30-45min |
| Brasil inteiro | ~2GB | ~2-3h, RAM ~8GB |

## Hospedagem em produção

Pra rodar 24×7 sem ter o Docker Desktop aberto, sobe num VPS:

| Provedor | Tier mínimo | Preço/mês |
|---|---|---|
| Hetzner CX22 | 2 vCPU, 4GB RAM, 40GB SSD | €4 |
| DigitalOcean | 1 vCPU, 2GB RAM, 50GB SSD | $6 |
| Contabo | 4 vCPU, 8GB RAM, 100GB NVMe | €5 |

OSRM consome ~200-400MB RAM em runtime (com RMBH carregado). O `extract`
de uma vez exige ~1GB temporário — pode ser feito local e copiar os
arquivos `rmbh.osrm*` pro VPS (mais barato que rodar extract no VPS).
