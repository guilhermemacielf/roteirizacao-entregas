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

from motor.modelos import CD, Entregador
from motor.io import carregar_entregas_texto, rotas_para_dict, sheets_para_csv_motor
from motor.roteirizar import roteirizar
from motor.matriz import MatrizError
from motor.geocode import GeocodeError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "dados", "config.json")
EXEMPLO_CSV = os.path.join(BASE_DIR, "dados", "exemplo_entregas.csv")

app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    return app.send_static_file("index.html")


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
    return jsonify(resultado)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
