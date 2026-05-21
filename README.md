# Roteirização de Entregas

Motor de roteirização das entregas diárias do ecommerce — substitui o uso
semi-manual do SimpliRoute. Lê a planilha do Instabuy (Google Sheets ou
CSV), geocodifica os endereços, agrupa em rotas balanceadas, ordena cada
rota minimizando distância e termina na casa do entregador. Restantes
viram candidatas a Lalamove.

## Cenário

- **2 janelas/dia.** Roteirização ~3h antes de cada janela.
- Dia normal: ~120 entregas, ~8 entregadores. Cada rota tem **10 a 18
  paradas** (nunca > 18).
- Cada rota **sai do CD e termina na casa do entregador**.
- Quando faltam entregadores: **rotas curtas perto do CD** viram
  candidatas a Lalamove.

## Stack

| Camada | O que usa |
|---|---|
| Otimização | OR-Tools (grátis) — K-means equilibrado + TSP por cluster |
| Matriz dist/tempo | OSRM self-hosted sobre OpenStreetMap (RMBH) |
| Geocoding | Nominatim → Google Maps (fallback) → BrasilAPI/CEP → centroide bairro |
| Cache geocode | JSON em disco (`dados/geocode.cache.json`) — endereços repetem entre dias |
| Backend | Flask + gunicorn |
| Frontend | Single-page (Leaflet/OSM) servido pelo próprio Flask |
| Planilha | Google Sheets API (export CSV) + Sheets write via OAuth |

**Por que OSRM self-hosted:** a Distance Matrix do Google custaria
~US$4.000/mês nesse volume. OSRM com mapa RMBH faz a mesma matriz de
graça (~€4/mês de VPS).

## Setup em máquina nova

Pré-requisitos:
- **Python 3.12+** (testado em 3.14)
- **Docker Desktop** (pro OSRM self-hosted) — rodando antes do setup
- ~1 GB de disco livre pro PBF da região

```powershell
# 1. Clone
git clone https://github.com/SEU_USUARIO/roteirizacao-entregas.git
cd roteirizacao-entregas

# 2. Ambiente virtual + deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

# 3. OSRM self-hosted (RMBH) — uma vez, leva ~10-15min
cd osrm
.\setup.ps1                       # Linux/Mac: bash setup.sh
docker compose up -d              # sobe o container osrm-mg em :5001
cd ..

# 4. Secrets (NÃO commitados — pegue com o dono do projeto):
#    - oauth_client.json  na raiz       → integração Google Sheets
#    - dados/sheets_oauth_token.json    → token já autorizado (opcional)
#    - .env com GOOGLE_MAPS_API_KEY     → fallback de geocoding
copy NUL .env
notepad .env                      # cole: GOOGLE_MAPS_API_KEY=AIza...

# 5. Roda
$env:OSRM_URL = "http://localhost:5001"
python app.py
# → http://localhost:5000
```

Em Linux/Mac, troque `$env:VAR = ...` por `export VAR=...`.

## Variáveis de ambiente

| Variável | Default | Pra quê |
|---|---|---|
| `OSRM_URL` | `https://router.project-osrm.org` (público, limite ~100 pontos) | Endpoint OSRM. Em prod aponte pro self-hosted. |
| `NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | Endpoint Nominatim. |
| `PHOTON_URL` | `https://photon.komoot.io` | Fallback (não usado por padrão). |
| `GEOCODE_USER_AGENT` | `roteirizacao-entregas/1.0` | Identificação no Nominatim. |
| `GOOGLE_MAPS_API_KEY` | — | Fallback de geocoding quando o Nominatim falha. 10k req/mês grátis. |
| `GEOCODE_CACHE` | `dados/geocode.cache.json` | Caminho do cache em disco. |
| `GOOGLE_SHEETS_OAUTH_CLIENT_FILE` | `./oauth_client.json` | OAuth do Google Sheets (write). |

## Rodar — UI web

```bash
python app.py
```

Abre em `http://localhost:5000`. Tem três caminhos de entrada:

- **URL do Google Sheets** (formato Instabuy) — baixa, parseia, geocodifica, separa janelas
- **Upload CSV** com colunas `id, lat, lng [, nome, obs, bairro, cidade, janela_inicio, janela_fim]`
- **CSV de exemplo** (`dados/exemplo_entregas.csv`)

Marca os entregadores disponíveis, ajusta `min`/`max`/tempo e clica em
Roteirizar. As rotas aparecem na sidebar e no mapa com geometria real das
ruas (via OSRM). Dá pra **selecionar paradas e mover entre rotas**
clicando — o backend re-roda o TSP só das afetadas.

## Rodar — CLI

```bash
python -m motor.cli dados/exemplo_entregas.csv
python -m motor.cli dados/exemplo_entregas.csv --export saida/rotas.csv
python -m motor.cli dados/exemplo_entregas.csv --min 10 --max 18 --tempo 60
```

## Estrutura

```
app.py                  Flask: SPA + API que roda o motor
static/index.html       UI single-page (Leaflet)
motor/
  modelos.py            dataclasses (Entrega, Entregador, CD, Rota, Parada)
  matriz.py             cliente OSRM (table + route)
  clustering.py         K-means equilibrado + TSP por cluster
  roteirizar.py         pipeline principal + reroteirização parcial
  geocode.py            cascata Nominatim → Google → BrasilAPI → centroide bairro
  io.py                 leitura CSV/Sheets, serializer de rotas
  obs.py                parser de janelas ("até 10h", "entre 8h e 12h", ...)
  valores.py            cálculo de pagamento por bairro/cidade
  sheets_write.py       export pra Sheets via OAuth
  entregadores_sheet.py sincronização da aba "Entregadores"
  cli.py                entry point CLI
dados/
  config.json           CD + cadastro de entregadores
  valores.json          tabela de pagamento por bairro/cidade
  exemplo_entregas.csv  36 entregas (região BH)
  geocode.cache.json    cache em disco (ignorado pelo git por enquanto)
osrm/
  setup.ps1 / setup.sh  baixa PBF do sudeste, recorta RMBH, processa
  docker-compose.yml    sobe o container osrm-mg em :5001
deploy/
  setup-vps.sh          provisiona VPS Ubuntu (Docker + firewall)
  GUIA.md               passo-a-passo Hetzner CX22
Dockerfile              imagem do app Flask
docker-compose.prod.yml stack prod (app + osrm)
```

## Deploy

Ver [`deploy/GUIA.md`](deploy/GUIA.md) — provisiona um Hetzner CX22 (€4/mês),
sobe Docker Compose com Flask + OSRM no mesmo host, acessível via HTTP.

## Status

- Motor (K-means equilibrado + TSP/CVRP por cluster + balanceamento por
  km/tempo): preserva blocos densos, span ≤1 entre clusters, cap rígido
  10-18 paradas/rota
- UI web: upload CSV, integração Sheets (read/write), edição manual
  (mover paradas), geometria real das ruas, badge Lalamove agrupada
- OSRM self-hosted (RMBH) processado e validado
- Geocoding em cascata com cache em disco, ~230 endereços resolvidos
  hoje; precarga em lote dos clientes recorrentes a fazer
- Deploy: arquivos prontos (Dockerfile, compose, guia), VPS a provisionar
