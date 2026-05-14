# Roteirização de Entregas

Motor de roteirização das entregas diárias do ecommerce — substitui o uso semi-manual do SimpliRoute.

## Problema

- **2 janelas de entrega por dia.** Roteirização começa ~3h antes de cada janela.
- Dia normal: **~120 entregas, ~8 entregadores** → média 15/entregador.
- Cada entregador faz **10 a 18 entregas** (nunca passa de 18).
- Cada rota **sai do CD e termina na casa do entregador**.
- Quando faltam entregadores: gerar **rotas curtas perto do CD** como candidatas a app (Lalamove).

## Arquitetura (v1 — só o motor)

```
entrada (lista de entregas) → motor → saída (rotas ordenadas)
```

| Etapa | Módulo | O que faz |
|-------|--------|-----------|
| 1. Matriz | `motor/matriz.py` | distância/tempo entre todos os pontos (OSRM) |
| 2. Roteirização | `motor/roteirizar.py` | CVRP com OR-Tools: agrupa + atribui + ordena de uma vez |
| 3. Lalamove | `motor/lalamove.py` | seleciona rotas curtas perto do CD quando faltam entregadores |

**Por que OSRM e não Google:** a Distance Matrix API do Google custaria ~US$4.000/mês
nesse volume. OSRM self-hosted (OpenStreetMap) faz a mesma matriz de graça.
OR-Tools (o otimizador) sempre foi grátis — nunca foi a fonte do custo.

## Rodar

```bash
pip install -r requirements.txt
python -m motor.cli dados/exemplo_entregas.csv
```

Opções:
```bash
python -m motor.cli dados/exemplo_entregas.csv --export saida/rotas.csv
python -m motor.cli dados/exemplo_entregas.csv --min 10 --max 18 --tempo 60
python -m motor.cli dados/exemplo_entregas.csv --config dados/config.json
```

Por padrão usa o servidor OSRM público de demonstração (limite ~100 pontos).
Pra produção (~120+ entregas), suba um OSRM self-hosted e configure:
```bash
export OSRM_URL=http://seu-osrm:5000
```

## Estrutura

```
motor/
├── modelos.py     # dataclasses: Entrega, Entregador, CD, Rota, Parada
├── matriz.py      # cliente OSRM — matriz de distância/tempo
├── roteirizar.py  # CVRP com OR-Tools (agrupa + atribui + ordena)
├── io.py          # parse de CSV/JSON + formatação da saída
└── cli.py         # entry point
dados/
├── config.json            # CD + cadastro de entregadores
└── exemplo_entregas.csv    # 36 entregas de exemplo (região BH)
```

## Formato de entrada

**Entregas (CSV):** colunas `id, lat, lng` obrigatórias; `obs, janela_inicio,
janela_fim` opcionais (janelas em minutos desde o início da roteirização).

**Config (JSON):** o CD e a lista de entregadores com endereço de casa.
`disponivel: false` tira o entregador do dia.

> Geocoding (endereço → lat/lng) não está no v1 — o CSV já vem com coordenadas.

## Status

v1 em construção — motor puro (entra CSV, sai rotas). Próximas etapas:
geocoding com cache, camada Lalamove (rotas curtas perto do CD), UI web.
