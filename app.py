"""
UI web do motor de roteirização — Flask.

Sobe a interface (static/index.html) e expõe a API que roda o motor:
  GET  /                     → SPA
  GET  /api/config           → CD + entregadores do dados/config.json
  GET  /api/exemplo-entregas → CSV de exemplo (pra testar a UI sem dados reais)
  POST /api/rotear           → roda o CVRP e devolve as rotas em JSON

Rodar:
    pip install -r requirements.txt
    python app.py
    → http://localhost:5000
"""

import json
import logging
import os

from flask import Flask, request, jsonify

from motor.modelos import CD, Entrega, Entregador
from motor.io import carregar_entregas_texto, rotas_para_dict, sheets_para_csv_motor
from motor.roteirizar import roteirizar, _tsp_cluster, _agrupar_lalamoves
from motor.matriz import MatrizError, matriz as osrm_matriz
from motor.geocode import (GeocodeError, carregar_cache, salvar_cache,
                            _normalizar, geocodificar,
                            purgar_centroides_genericos)
from motor.obs import extrair_janela
from motor.sheets_write import escrever_rotas, SheetsWriteError
from motor.entregadores_sheet import sincronizar_entregadores, carregar_valores
from motor.valores import calcular_valor_todas

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "dados", "config.json")
EXEMPLO_CSV = os.path.join(BASE_DIR, "dados", "exemplo_entregas.csv")

app = Flask(__name__, static_folder="static")

# Avisa qual OSRM tá sendo usado — o público recusa matrizes >100 pontos,
# o que pega de surpresa quem rodou só com a planilha de exemplo. Setup
# do self-hosted: ver `osrm/README.md`.
_osrm_url = os.environ.get("OSRM_URL", "https://router.project-osrm.org")
if "project-osrm.org" in _osrm_url:
    log.warning("OSRM público em uso (%s). Limite ~100 pontos por matriz.", _osrm_url)
    log.warning("Pra >100 pontos, suba o self-hosted: ver osrm/README.md")
else:
    log.info("OSRM em %s (self-hosted, sem limite de pontos)", _osrm_url)


@app.route("/")
def index():
    resp = app.make_response(app.send_static_file("index.html"))
    # Desabilita cache do browser — desenvolvimento ativo, mudanças no HTML/JS
    # devem aparecer ao recarregar sem precisar de hard refresh.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/config")
def api_config():
    """CD + entregadores do config.json — todos, com a flag `disponivel`,
    pra a UI montar os toggles de quem está no dia."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return jsonify({"erro": f"não consegui ler dados/config.json: {e}"}), 500
    c = cfg.get("cd", {})
    return jsonify({
        "cd": {"nome": c.get("nome", "CD"), "lat": c.get("lat"), "lng": c.get("lng")},
        "entregadores": [
            {
                "id": str(e.get("id")),
                "nome": e.get("nome"),
                "lat": e.get("lat"),
                "lng": e.get("lng"),
                "disponivel": e.get("disponivel", True),
                "preferencias": e.get("preferencias", []),
            }
            for e in cfg.get("entregadores", [])
        ],
    })


@app.route("/api/exemplo-entregas")
def api_exemplo_entregas():
    """CSV de exemplo (entregas na região de BH) pra testar a UI."""
    try:
        with open(EXEMPLO_CSV, encoding="utf-8-sig") as f:
            return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except OSError as e:
        return jsonify({"erro": f"exemplo não encontrado: {e}"}), 404


@app.route("/api/sheets", methods=["POST"])
def api_sheets():
    """Recebe {"url": "..."} de uma planilha Google Sheets pública no formato
    Instabuy, baixa, parseia, geocodifica e devolve um CSV pronto pro motor.

    Resposta:
      {
        "entregas_csv": "<csv id,nome,lat,lng,obs,janela_*>",
        "n_ok": 120,
        "n_falhas": 2,
        "falhas": [{id,nome,endereco,motivo}, ...],   # endereços não geocodificados
        "amostras": [{id,nome,endereco,janela_inicio,janela_fim}, ...]   # 1as 5, pra a UI mostrar
      }

    Erros:
      400 — URL inválida, planilha sem coluna ENDEREÇO, sem entregas válidas
      502 — falha de rede no Nominatim
    """
    d = request.get_json(silent=True) or {}
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"erro": "informe a URL da planilha do Google Sheets"}), 400

    try:
        csv_texto, ok, falhas = sheets_para_csv_motor(url)
    except ValueError as e:
        # URL inválida, planilha sem cabeçalho, falha pra baixar — tudo erro de entrada.
        return jsonify({"erro": str(e)}), 400
    except GeocodeError as e:
        return jsonify({"erro": f"falha consultando o Nominatim: {e}"}), 502
    except Exception as e:
        log.exception("erro inesperado no /api/sheets")
        return jsonify({"erro": f"erro inesperado: {e}"}), 500

    if not ok and not falhas:
        return jsonify({"erro": "a planilha não tem linhas de entrega com endereço"}), 400

    amostras = [
        {"id": e["id"], "nome": e["nome"], "endereco": e["endereco"],
         "janela_inicio": e["janela_inicio"], "janela_fim": e["janela_fim"]}
        for e in ok[:5]
    ]
    return jsonify({
        "entregas_csv": csv_texto,
        "n_ok":         len(ok),
        "n_falhas":     len(falhas),
        "falhas":       falhas,
        "amostras":     amostras,
    })


@app.route("/api/geocode/manual", methods=["POST"])
def api_geocode_manual():
    """Cadastra coordenada manual pra um endereço que não geocodificou.
    Persiste no cache (chave normalizada) e devolve a entrega formatada
    pra UI adicionar à lista de entregas sem precisar recarregar tudo.

    Body: {endereco, lat, lng, id?, nome?, obs?}
    Resposta: dict com {id, nome, lat, lng, obs, janela_inicio, janela_fim}
    no mesmo formato que a UI usa pras entregas geocodificadas."""
    d = request.json or {}
    endereco = (d.get("endereco") or "").strip()
    nome     = (d.get("nome") or "").strip()
    obs      = (d.get("obs") or "").strip()
    cod      = (d.get("id") or "").strip()
    if not endereco:
        return jsonify({"erro": "endereco obrigatório"}), 400
    try:
        lat = float(d.get("lat"))
        lng = float(d.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"erro": "lat/lng inválidos — use formato decimal (ex: -19.93)"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"erro": "lat/lng fora dos limites válidos"}), 400

    # Persiste no cache pra próxima vez que esse endereço aparecer
    # (ex: cliente recorrente que sempre falha no Nominatim) não falhar mais.
    cache = carregar_cache()
    cache[_normalizar(endereco)] = [lat, lng]
    salvar_cache(cache)

    # Mesma extração de horário do pipeline principal (obs + nome combinados —
    # sufixo "até 10h" pode vir em qualquer um dos dois).
    ini, fim = extrair_janela(f"{nome} {obs}")

    return jsonify({
        "id":            cod or "MANUAL",
        "nome":          nome,
        "lat":           lat,
        "lng":           lng,
        "obs":           obs,
        "janela_inicio": ini,
        "janela_fim":    fim,
    })


@app.route("/api/geocode/purgar-genericos", methods=["POST"])
def api_geocode_purgar():
    """Remove do cache de geocoding entradas que caíram em centroides
    genéricos (coords compartilhadas por 2+ endereços diferentes).
    Pra reaproveitar quando descobre que vários endereços estão TODOS
    no mesmo ponto do mapa por causa de fallback do Nominatim.
    Após purgar, próxima sincronização da planilha re-geocodifica.

    Body opcional: {"tolerancia_metros": 50}
    Resposta: {"removidas": N, "duplicados": [[lat, lng, n_apontamentos]...]}
    """
    d = request.get_json(silent=True) or {}
    tol = float(d.get("tolerancia_metros", 50))
    n, dups = purgar_centroides_genericos(tolerancia_metros=tol)
    return jsonify({"removidas": n, "duplicados": dups})


@app.route("/api/geocode/buscar", methods=["POST"])
def api_geocode_buscar():
    """Busca lat/lng de um endereço (pipeline completo: Nominatim → Photon →
    BrasilAPI CEP → centroide do bairro/cidade). Usado pelo editor de ponto
    no mapa quando o usuário quer consertar uma entrega geocodificada errada
    sem precisar abrir o Google Maps. Resposta: {lat, lng}."""
    d = request.json or {}
    endereco = (d.get("endereco") or "").strip()
    if not endereco:
        return jsonify({"erro": "endereço obrigatório"}), 400
    try:
        coord = geocodificar(endereco)
    except GeocodeError as e:
        return jsonify({"erro": f"falha no geocoder: {e}"}), 502
    if not coord:
        return jsonify({"erro": "endereço não encontrado — tente algo mais específico ou colar coords"}), 404
    return jsonify({"lat": coord[0], "lng": coord[1]})


@app.route("/api/rotear", methods=["POST"])
def api_rotear():
    """Roda o motor. Body JSON:
      {
        "entregas_csv": "<conteúdo do CSV>",
        "cd": {"nome","lat","lng"},
        "entregadores": [{"id","nome","lat","lng"}, ...],  # só os disponíveis
        "min": 10, "max": 18, "tempo": 30
      }
    """
    d = request.get_json(silent=True) or {}

    # ── entregas ──
    try:
        entregas = carregar_entregas_texto(d.get("entregas_csv") or "")
    except Exception as e:
        return jsonify({"erro": f"erro lendo o CSV de entregas: {e}"}), 400
    if not entregas:
        return jsonify({"erro": "nenhuma entrega válida no CSV — precisa das colunas id, lat, lng"}), 400

    # ── CD ──
    cd_in = d.get("cd") or {}
    try:
        cd = CD(lat=float(cd_in["lat"]), lng=float(cd_in["lng"]),
                nome=cd_in.get("nome", "CD"))
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "CD inválido — precisa de lat e lng"}), 400

    # ── entregadores ──
    entregadores = []
    for e in d.get("entregadores") or []:
        try:
            entregadores.append(Entregador(
                id=str(e["id"]), nome=e.get("nome") or str(e["id"]),
                lat=float(e["lat"]), lng=float(e["lng"]),
                preferencias=[p.strip() for p in (e.get("preferencias") or []) if p and p.strip()],
            ))
        except (KeyError, TypeError, ValueError):
            return jsonify({"erro": f"entregador inválido: {e}"}), 400
    if not entregadores:
        return jsonify({"erro": "nenhum entregador disponível — marque ao menos um"}), 400

    # ── parâmetros ──
    try:
        min_p = int(d.get("min", 10))
        max_p = int(d.get("max", 18))
        tempo = int(d.get("tempo", 30))
    except (TypeError, ValueError):
        return jsonify({"erro": "min/max/tempo devem ser números"}), 400
    if min_p < 1 or max_p < min_p or tempo < 1:
        return jsonify({"erro": "parâmetros inválidos: precisa de 1 ≤ min ≤ max e tempo ≥ 1"}), 400

    # ── roda o motor ──
    try:
        rotas = roteirizar(
            entregas, entregadores, cd,
            min_paradas=min_p, max_paradas=max_p, tempo_limite_s=tempo,
        )
    except MatrizError as e:
        return jsonify({"erro": f"matriz de distância (OSRM): {e}"}), 400
    except (ValueError, RuntimeError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        log.exception("erro inesperado na roteirização")
        return jsonify({"erro": f"erro inesperado: {e}"}), 500

    resultado = rotas_para_dict(rotas, cd)
    resultado["n_entregas_entrada"] = len(entregas)

    # Se tem tabela de valores cadastrada (sincronizada da planilha de
    # entregadores), calcula valor pago por rota com memória de cálculo.
    valores = carregar_valores()
    if valores:
        calcular_valor_todas(resultado["rotas"], valores)
        resultado["valores"] = valores
        resultado["pagamento_total"] = round(sum(
            r["pagamento"]["valor_total"] for r in resultado["rotas"]
            if r.get("pagamento")
        ), 2)

    return jsonify(resultado)


def _entrega_de_parada(p: dict) -> Entrega:
    """Reconstrói uma Entrega a partir de um dict de parada vindo do front."""
    return Entrega(
        id=str(p.get("id") or ""),
        lat=float(p["lat"]),
        lng=float(p["lng"]),
        nome=(p.get("nome") or "").strip(),
        obs=(p.get("obs") or "").strip(),
        bairro=(p.get("bairro") or "").strip(),
        cidade=(p.get("cidade") or "").strip(),
        janela_inicio=p.get("janela_inicio"),
        janela_fim=p.get("janela_fim"),
    )


@app.route("/api/rotear/mover", methods=["POST"])
def api_rotear_mover():
    """Aplica movimentações manuais de entregas entre rotas e recalcula
    o TSP só das rotas afetadas. Body:
      {
        "rotas": [...],     // estado atual completo (de /api/rotear)
        "cd":    {...},
        "movimentos": [
          {"entrega_id": "abc-123", "para_rota_idx": 3},
          ...
        ]
      }
    Resposta: mesmo formato de /api/rotear, com as rotas atualizadas e
    pagamento recalculado.
    """
    d = request.get_json(silent=True) or {}
    rotas_in = d.get("rotas") or []
    cd_dict = d.get("cd") or {}
    movimentos = d.get("movimentos") or []

    try:
        cd = CD(lat=float(cd_dict["lat"]), lng=float(cd_dict["lng"]),
                nome=cd_dict.get("nome", "CD"))
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "CD inválido"}), 400

    # Mapa entrega_id → (idx_rota_origem, dict_parada). Pra cada movimento,
    # achamos origem e movemos pra rota destino. Lalamoves são identificadas
    # por candidata_lalamove=True; entregas movidas pra Lalamove vão pra
    # uma "pool" e re-agrupamos no fim.
    id_para_origem: dict[str, tuple[int, dict]] = {}
    for ridx, r in enumerate(rotas_in):
        for p in r.get("paradas") or []:
            id_para_origem[str(p.get("id"))] = (ridx, p)

    afetadas: set[int] = set()
    paradas_por_rota: list[list[dict]] = [list(r.get("paradas") or []) for r in rotas_in]
    n_movidas = 0
    for mov in movimentos:
        eid = str(mov.get("entrega_id") or "")
        try:
            para = int(mov.get("para_rota_idx"))
        except (TypeError, ValueError):
            continue
        origem = id_para_origem.get(eid)
        if origem is None or para < 0 or para >= len(rotas_in) or origem[0] == para:
            continue
        ridx_o, parada = origem
        # Remove da origem
        paradas_por_rota[ridx_o] = [p for p in paradas_por_rota[ridx_o]
                                     if str(p.get("id")) != eid]
        # Adiciona no destino
        paradas_por_rota[para] = list(paradas_por_rota[para]) + [parada]
        afetadas.add(ridx_o)
        afetadas.add(para)
        # Atualiza mapa pra movimentos subsequentes que mexam nessa entrega
        id_para_origem[eid] = (para, parada)
        n_movidas += 1

    # Re-roteiriza só as afetadas.
    # Pra entregadores normais: TSP local com matriz OSRM.
    # Pra Lalamove: junta tudo das rotas lala afetadas e re-agrupa em rotas
    # de até MAX_PARADAS_LALAMOVE pela proximidade.
    rotas_dict_nova = list(rotas_in)
    lalamove_pool: list[Entrega] = []
    rotas_lala_idx: list[int] = [ridx for ridx, r in enumerate(rotas_in)
                                  if r.get("candidata_lalamove")]
    lala_afetada = any(ridx in afetadas for ridx in rotas_lala_idx)

    # Coleta TODAS as entregas das Lalamoves (mantém pool unificado)
    if lala_afetada:
        for ridx in rotas_lala_idx:
            for p in paradas_por_rota[ridx]:
                lalamove_pool.append(_entrega_de_parada(p))
            # Zera a rota Lalamove antiga (vai ser substituída)
            rotas_dict_nova[ridx] = None

    # TSP nas rotas normais afetadas
    from motor.matriz import rota_geometria
    for ridx in afetadas:
        if ridx in rotas_lala_idx:
            continue  # tratado abaixo
        r = rotas_in[ridx]
        ent_dict = r.get("entregador") or {}
        if not ent_dict:
            continue
        try:
            ent = Entregador(
                id=str(ent_dict["id"]), nome=ent_dict["nome"],
                lat=float(ent_dict["lat"]), lng=float(ent_dict["lng"]),
            )
        except (KeyError, TypeError, ValueError):
            continue

        entregas_rota = [_entrega_de_parada(p) for p in paradas_por_rota[ridx]]
        if not entregas_rota:
            # Rota ficou vazia depois das movimentações
            rotas_dict_nova[ridx] = {
                **r, "paradas": [], "n_paradas": 0,
                "distancia_km": 0, "duracao_s": 0,
                "geometry": [[cd.lat, cd.lng], [ent.lat, ent.lng]],
            }
            continue

        coords = ([(e.lat, e.lng) for e in entregas_rota]
                  + [(cd.lat, cd.lng), (ent.lat, ent.lng)])
        try:
            mat = osrm_matriz(coords)
        except MatrizError as e:
            return jsonify({"erro": f"OSRM: {e}"}), 502

        rota_nova, drops = _tsp_cluster(
            entregas_rota, ent, cd, mat["distancia"], mat["duracao"],
            servico_por_entrega_s=600, limite_rota_min=300, tempo_limite_s=10,
        )
        if rota_nova is None:
            # Não conseguiu — manda todas pra Lalamove pool
            lalamove_pool.extend(entregas_rota)
            rotas_dict_nova[ridx] = {
                **r, "paradas": [], "n_paradas": 0,
                "distancia_km": 0, "duracao_s": 0,
                "geometry": [[cd.lat, cd.lng], [ent.lat, ent.lng]],
            }
            continue

        # Droppadas pelo solver (janela apertada) → Lalamove pool
        lalamove_pool.extend(drops)

        # Geometria real pra UI
        seq_pts = ([(cd.lat, cd.lng)]
                   + [(p.entrega.lat, p.entrega.lng) for p in rota_nova.paradas]
                   + [(ent.lat, ent.lng)])
        geom = rota_geometria(seq_pts)

        rotas_dict_nova[ridx] = {
            "entregador": {"id": ent.id, "nome": ent.nome,
                           "lat": ent.lat, "lng": ent.lng},
            "candidata_lalamove": False,
            "n_paradas": rota_nova.n_paradas,
            "distancia_km": round(rota_nova.distancia_m / 1000, 2),
            "duracao_s": int(rota_nova.duracao_s),
            "geometry": [[lat, lng] for lat, lng in geom],
            "paradas": [
                {
                    "ordem": p.ordem, "id": p.entrega.id, "nome": p.entrega.nome,
                    "lat": p.entrega.lat, "lng": p.entrega.lng,
                    "bairro": p.entrega.bairro, "cidade": p.entrega.cidade,
                    "obs": p.entrega.obs,
                    "janela_inicio": p.entrega.janela_inicio,
                    "janela_fim": p.entrega.janela_fim,
                    "chegada_estimada_s": int(p.chegada_estimada_s),
                }
                for p in rota_nova.paradas
            ],
        }

    # Re-agrupa Lalamoves se afetada
    if lala_afetada or lalamove_pool:
        novas_lalas = _agrupar_lalamoves(lalamove_pool, cd)
        # Substitui as posições antigas pelas novas; se houver mais que vagas,
        # adiciona no fim; se sobrar, remove.
        rotas_lala_pos = [i for i, r in enumerate(rotas_dict_nova) if r is None]
        # Aplica novas em posições antigas até onde der
        for k, nova in enumerate(novas_lalas):
            ent = nova.entregador
            d_nova = {
                "entregador": {"id": ent.id, "nome": ent.nome,
                               "lat": ent.lat, "lng": ent.lng},
                "candidata_lalamove": True,
                "n_paradas": nova.n_paradas,
                "distancia_km": round(nova.distancia_m / 1000, 2),
                "duracao_s": int(nova.duracao_s),
                "geometry": [(cd.lat, cd.lng)] + [(p.entrega.lat, p.entrega.lng) for p in nova.paradas],
                "paradas": [
                    {
                        "ordem": p.ordem, "id": p.entrega.id, "nome": p.entrega.nome,
                        "lat": p.entrega.lat, "lng": p.entrega.lng,
                        "bairro": p.entrega.bairro, "cidade": p.entrega.cidade,
                        "obs": p.entrega.obs,
                        "janela_inicio": p.entrega.janela_inicio,
                        "janela_fim": p.entrega.janela_fim,
                        "chegada_estimada_s": int(p.chegada_estimada_s),
                    }
                    for p in nova.paradas
                ],
            }
            if k < len(rotas_lala_pos):
                rotas_dict_nova[rotas_lala_pos[k]] = d_nova
            else:
                rotas_dict_nova.append(d_nova)
        # Posições Lalamove antigas não usadas viram None — limpa
        rotas_dict_nova = [r for r in rotas_dict_nova if r is not None]

    # Recalcula valor
    valores = carregar_valores()
    if valores:
        calcular_valor_todas(rotas_dict_nova, valores)
        pagamento_total = round(sum(
            r["pagamento"]["valor_total"] for r in rotas_dict_nova
            if r.get("pagamento")
        ), 2)
    else:
        pagamento_total = None

    # Resumo agregado (mesmo formato de rotas_para_dict)
    rotas_normais = [r for r in rotas_dict_nova if not r.get("candidata_lalamove")]
    rotas_lala = [r for r in rotas_dict_nova if r.get("candidata_lalamove")]
    total_entregas = sum(r.get("n_paradas", 0) for r in rotas_dict_nova)
    total_km = round(sum(r.get("distancia_km", 0) for r in rotas_dict_nova), 1)
    resp = {
        "cd": {"lat": cd.lat, "lng": cd.lng, "nome": cd.nome},
        "rotas": rotas_dict_nova,
        "resumo": {
            "n_rotas": len(rotas_dict_nova),
            "n_rotas_entregadores": len(rotas_normais),
            "n_rotas_lalamove": len(rotas_lala),
            "total_entregas": total_entregas,
            "entregas_lalamove": sum(r.get("n_paradas", 0) for r in rotas_lala),
            "total_km": total_km,
            "km_lalamove": round(sum(r.get("distancia_km", 0) for r in rotas_lala), 1),
        },
        "n_entregas_entrada": total_entregas,
    }
    if pagamento_total is not None:
        resp["valores"] = valores
        resp["pagamento_total"] = pagamento_total

    resp["movimentos_aplicados"] = n_movidas
    return jsonify(resp)


@app.route("/api/entregadores/sincronizar", methods=["POST"])
def api_entregadores_sincronizar():
    """Baixa a planilha de cadastro de entregadores + valores, geocodifica
    e sobrescreve dados/config.json + dados/valores.json. Body: {url}.

    Resposta: {n_entregadores, falhas[], valor_km, valor_padrao, n_valores_bairro}.
    """
    d = request.get_json(silent=True) or {}
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"erro": "informe a URL da planilha"}), 400
    try:
        r = sincronizar_entregadores(url)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        log.exception("erro no sincronizar_entregadores")
        return jsonify({"erro": f"erro inesperado: {e}"}), 500
    return jsonify(r)


@app.route("/api/sheets/escrever", methods=["POST"])
def api_sheets_escrever():
    """Escreve as rotas de volta na planilha Google Sheets: coluna A com o
    nome do entregador, coluna B com a ordem da rota. Casamento pela coluna
    CÓDIGO. Exige Service Account configurada (ver motor/sheets_write.py).

    Body: {"url": "<sheets-url>", "rotas": [...]}  (rotas vem direto da resposta de /api/rotear)
    """
    d = request.get_json(silent=True) or {}
    url = (d.get("url") or "").strip()
    rotas = d.get("rotas") or []
    if not url:
        return jsonify({"erro": "informe a URL da planilha"}), 400
    if not rotas:
        return jsonify({"erro": "nenhuma rota pra escrever — rode /api/rotear antes"}), 400
    try:
        r = escrever_rotas(url, rotas)
    except SheetsWriteError as e:
        return jsonify({"erro": str(e)}), 503
    except Exception as e:
        log.exception("erro inesperado no /api/sheets/escrever")
        return jsonify({"erro": f"erro inesperado: {e}"}), 500
    return jsonify(r)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
