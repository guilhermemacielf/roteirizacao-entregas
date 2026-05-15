"""
Geocoding endereço → (lat, lng) via Nominatim (OpenStreetMap).

Endereços de cliente repetem MUITO entre os dias — o cache em disco
(dados/geocode.cache.json) faz quase toda requisição virar hit, então
o custo de rede praticamente some depois dos primeiros dias.

Nominatim público: a política de uso pede um User-Agent identificável e
no máximo ~1 requisição por segundo. Pra volume alto/produção, vale subir
um Nominatim self-hosted (ou Photon) e apontar a env NOMINATIM_URL.
"""

import json
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
USER_AGENT = os.environ.get("GEOCODE_USER_AGENT", "roteirizacao-entregas/1.0")
CACHE_PATH = os.environ.get(
    "GEOCODE_CACHE",
    os.path.join(os.path.dirname(__file__), "..", "dados", "geocode.cache.json"),
)
PAUSA_S = 1.1  # respeita o limite de ~1 req/s do Nominatim público


class GeocodeError(Exception):
    pass


def _normalizar(endereco: str) -> str:
    """Chave de cache — minúsculo, espaços colapsados. Sem isso, variações
    triviais ('R.' vs 'Rua', espaço duplo) furariam o cache."""
    return " ".join((endereco or "").strip().lower().split())


def carregar_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def salvar_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_PATH)   # escrita atômica
    except OSError as e:
        log.warning("não consegui salvar o cache de geocoding: %s", e)


def _consultar_nominatim(endereco: str) -> tuple[float, float] | None:
    """Uma consulta ao Nominatim. Devolve (lat, lng) ou None se não achar."""
    try:
        r = requests.get(
            f"{NOMINATIM_URL}/search",
            params={"q": endereco, "format": "json", "limit": 1,
                    "countrycodes": "br"},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise GeocodeError(f"falha ao consultar Nominatim: {e}") from e
    if not data:
        return None
    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def geocodificar(endereco: str, cache: dict | None = None) -> tuple[float, float] | None:
    """Endereço → (lat, lng), passando pelo cache em disco. Devolve None se
    o Nominatim não encontrar o endereço.

    Se `cache` for passado, opera nesse dict e NÃO salva (o chamador salva
    no fim — útil pra geocodificar uma lista sem I/O por item). Só dorme
    PAUSA_S quando realmente bate na rede (cache hit é instantâneo)."""
    chave = _normalizar(endereco)
    if not chave:
        return None
    proprio = cache is None
    if proprio:
        cache = carregar_cache()
    if chave in cache:
        v = cache[chave]
        return tuple(v) if v else None

    coord = _consultar_nominatim(endereco)
    cache[chave] = list(coord) if coord else None
    if proprio:
        salvar_cache(cache)
    time.sleep(PAUSA_S)
    return coord


def geocodificar_lista(
    enderecos: list[str], progresso=None
) -> dict[str, tuple[float, float] | None]:
    """Geocodifica uma lista de endereços: carrega o cache uma vez, consulta
    só os que faltam (com pausa entre requisições de rede), salva no fim.

    `progresso(feito, total)` é chamado a cada item, se fornecido.
    Retorno: {endereco_original: (lat,lng) | None}."""
    cache = carregar_cache()
    resultado: dict = {}
    total = len(enderecos)
    houve_consulta = False
    for i, end in enumerate(enderecos, start=1):
        chave = _normalizar(end)
        if chave and chave not in cache:
            houve_consulta = True
        resultado[end] = geocodificar(end, cache=cache)
        if progresso:
            progresso(i, total)
    if houve_consulta:
        salvar_cache(cache)
    return resultado


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    testes = [
        "Rua Ferreira Viana, 65, Salgado Filho, Belo Horizonte, MG",
        "Praça Sete de Setembro, Belo Horizonte, MG",
    ]
    for end in testes:
        print(f"{end!r:60} → {geocodificar(end)}")
