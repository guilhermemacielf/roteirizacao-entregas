"""
Cliente OSRM — matriz de distância e tempo entre pontos.

OSRM (Open Source Routing Machine) sobre dados do OpenStreetMap.
Substitui a Distance Matrix API do Google, que custaria ~US$4.000/mês
nesse volume. OSRM self-hosted faz a mesma coisa de graça.

Endpoint configurável via env OSRM_URL:
- Default: servidor público de demonstração (https://router.project-osrm.org)
  → bom pra testar, MAS limita ~100 pontos por requisição e pede pra não
    usar em produção.
- Produção: rodar OSRM self-hosted (container Docker com OSM da região) e
  apontar OSRM_URL pra ele — sem limite de pontos, responde em ms.

A função principal `matriz()` é o único ponto de acoplamento com OSRM.
O resto do motor recebe as matrizes prontas — então trocar a fonte
(OSRM, Valhalla, GraphHopper, etc.) não toca em mais nada.
"""

import os
import logging
import requests

log = logging.getLogger(__name__)

OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")

# Limite prático do servidor público de demo. Self-hosted não tem esse teto.
LIMITE_PONTOS_DEMO = 100


class MatrizError(Exception):
    pass


def matriz(coords: list[tuple[float, float]], perfil: str = "driving") -> dict:
    """
    Recebe lista de (lat, lng) e devolve as matrizes NxN de distância (metros)
    e duração (segundos) entre todos os pontos.

    Retorno:
        {
            "distancia": [[m, ...], ...],   # metros
            "duracao":   [[s, ...], ...],   # segundos
            "n": N,
        }

    A ordem das linhas/colunas é a MESMA da lista `coords` de entrada —
    o índice i da matriz corresponde a coords[i].
    """
    n = len(coords)
    if n < 2:
        raise MatrizError("precisa de pelo menos 2 pontos")
    if n > LIMITE_PONTOS_DEMO and "router.project-osrm.org" in OSRM_URL:
        raise MatrizError(
            f"{n} pontos excede o limite ~{LIMITE_PONTOS_DEMO} do servidor OSRM público. "
            f"Suba um OSRM self-hosted e configure OSRM_URL — sem limite e mais rápido."
        )

    # OSRM espera lng,lat (ordem invertida do usual)
    pares = ";".join(f"{lng},{lat}" for lat, lng in coords)
    url = f"{OSRM_URL}/table/v1/{perfil}/{pares}"
    params = {"annotations": "distance,duration"}

    try:
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise MatrizError(f"falha ao consultar OSRM ({url}): {e}") from e

    if data.get("code") != "Ok":
        raise MatrizError(f"OSRM retornou code={data.get('code')}: {data.get('message')}")

    distancias = data.get("distances")
    duracoes   = data.get("durations")
    if not distancias or not duracoes:
        raise MatrizError("OSRM não retornou matrizes de distância/duração")

    # OSRM pode devolver null em pares inalcançáveis — troca por um valor alto
    # pra o otimizador simplesmente evitar (não quebrar).
    INALCANCAVEL = 10 ** 9

    def _limpar(m):
        return [
            [int(v) if v is not None else INALCANCAVEL for v in linha]
            for linha in m
        ]

    return {
        "distancia": _limpar(distancias),
        "duracao":   _limpar(duracoes),
        "n": n,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Smoke check — 3 pontos em BH
    pontos = [
        (-19.9245, -43.9352),  # Centro BH
        (-19.9386, -43.9445),  # Savassi
        (-19.8512, -43.9690),  # Pampulha
    ]
    res = matriz(pontos)
    print(f"Matriz {res['n']}x{res['n']} OK")
    for linha in res["distancia"]:
        print("  ", [f"{m/1000:.1f}km" for m in linha])
