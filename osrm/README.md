# OSRM self-hosted (MG)

Servidor próprio de matriz de distância/tempo + rotas pra **Minas Gerais**.
Substitui o OSRM público (`router.project-osrm.org`) que limita ~100 pontos
por matriz — esse aqui não tem limite e roda em ~10× mais rápido por estar
local.

## Quando usar

- Rodada diária com > 100 entregas (público recusa)
- Quer geometria real das ruas no mapa (não polyline reta)
- Quer eliminar dependência de serviço externo

## Pré-requisito

- Docker Desktop instalado e rodando
- ~2GB de espaço livre (mapa + arquivos OSRM)
- ~4GB de RAM no `extract` (etapa única de setup)

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
1. Baixa o mapa de MG da Geofabrik (~150MB) → `data/minas-gerais-latest.osm.pbf`
2. `osrm-extract` — converte OSM em formato OSRM (10-20min)
3. `osrm-partition` — particiona o grafo MLD (~2min)
4. `osrm-customize` — otimiza pra queries (~2min)

Saída: `data/minas-gerais-latest.osrm*` (~500MB).

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
rm data/minas-gerais-latest.osm.pbf
./setup.sh   # ou .ps1
docker compose up -d
```

Frequência razoável: a cada 1-3 meses.

## Outros estados / Brasil inteiro

Trocar `URL` no setup:

| Mapa | URL | Tamanho PBF | Tempo extract |
|---|---|---|---|
| Minas Gerais | `.../sudeste/minas-gerais-latest.osm.pbf` | ~150MB | 10-20min |
| Sudeste | `.../sudeste-latest.osm.pbf` | ~600MB | 40-60min |
| Brasil | `.../brazil-latest.osm.pbf` | ~2GB | 2-3h |

Brasil inteiro precisa ~8GB de RAM no extract. Sudeste é o ponto doce
custo-benefício pra quem entrega só em RMBH + viagens ocasionais.

## Hospedagem em produção

Pra rodar 24×7 sem ter o Docker Desktop aberto, sobe num VPS:

| Provedor | Tier mínimo | Preço/mês |
|---|---|---|
| Hetzner CX22 | 2 vCPU, 4GB RAM, 40GB SSD | €4 |
| DigitalOcean | 1 vCPU, 2GB RAM, 50GB SSD | $6 |
| Contabo | 4 vCPU, 8GB RAM, 100GB NVMe | €5 |

OSRM consome ~500MB RAM em runtime (com mapa de MG carregado). O `extract`
de uma vez exige 2-4GB temporários — pode ser feito local e copiar os
arquivos `.osrm*` pro VPS (mais barato que rodar extract no VPS).
