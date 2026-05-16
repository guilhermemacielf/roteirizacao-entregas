"""
Geocoding endereço → (lat, lng) via Nominatim (OpenStreetMap), com
fallback pra Photon (Komoot) que é mais tolerante a queries bagunçadas.

Endereços de cliente repetem MUITO entre os dias — o cache em disco
(dados/geocode.cache.json) faz quase toda requisição virar hit, então
o custo de rede praticamente some depois dos primeiros dias.

Os endereços que vêm do Instabuy não vêm normalizados — costumam ter o
formato `<rua> <numero> [complemento] [bairro] <CEP> <cidade>` sem
vírgulas, com complementos arbitrários ("Apt 302", "Casa de muro de
pedra", "ORGANICO DO CHICO") no meio. Esse texto solto confunde o
parser do Nominatim que então não acha o endereço. O `_gerar_variacoes`
limpa o complemento e gera 3 versões pra tentar em cascata:

  v1. Endereço limpo (sem complementos, com vírgulas estruturais)
  v2. v1 mas só rua/número/cidade (sem bairro)
  v3. Só CEP + cidade (último recurso — costuma cair no centroide da rua)

Nominatim público: 1 req/s, User-Agent identificável. Pra produção,
subir um Nominatim self-hosted (NOMINATIM_URL).
"""

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
PHOTON_URL = os.environ.get(
    "PHOTON_URL", "https://photon.komoot.io"
).rstrip("/")
USER_AGENT = os.environ.get("GEOCODE_USER_AGENT", "roteirizacao-entregas/1.0")
CACHE_PATH = os.environ.get(
    "GEOCODE_CACHE",
    os.path.join(os.path.dirname(__file__), "..", "dados", "geocode.cache.json"),
)
PAUSA_S = 1.1  # respeita o limite de ~1 req/s do Nominatim público

# Cidades da região metropolitana de BH (pra detectar o sufixo do endereço).
# Lista pequena de propósito — adiciona conforme aparecer endereço de fora.
CIDADES = [
    "Belo Horizonte", "Contagem", "Nova Lima", "Sabará", "Betim",
    "Lagoa Santa", "Santa Luzia", "Ribeirão das Neves", "Vespasiano",
    "Ibirité", "Brumadinho", "Confins", "Pedro Leopoldo", "Esmeraldas",
    "Mateus Leme", "Caeté", "Jaboticatubas", "Itabirito",
]


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


# ── Normalização & variações ─────────────────────────────────
def _expandir_abrev(s: str) -> str:
    """R. → Rua, Av. → Avenida, etc. Só faz sentido no INÍCIO da string
    ou após espaço, pra não trocar "Senhora R. Maria" indevidamente."""
    s = re.sub(r"(?:^|\s)R\.\s+",  r" Rua ",      s)
    s = re.sub(r"(?:^|\s)Av\.\s+", r" Avenida ",  s)
    s = re.sub(r"(?:^|\s)Pç\.\s+", r" Praça ",    s)
    s = re.sub(r"(?:^|\s)Pca\.\s+", r" Praça ",   s)
    s = re.sub(r"(?:^|\s)Al\.\s+",  r" Alameda ", s)
    return s.strip()


# Complementos típicos do Instabuy que confundem o Nominatim. Captura a
# palavra-chave + 1 token (número, letra simples ou combinação tipo "401",
# "A", "B2", "1701/B"). Propositalmente NÃO avança pra o próximo token —
# o que vem depois costuma ser o bairro (ex: "Apt 302 Jaraguá").
_COMPLEMENTO_RE = re.compile(
    r"\s+(?:Apt\.?|Apto\.?|Ap\.?|Apartamento|Casa|Bl\.?|Bloco|Andar|"
    r"Predio|Prédio|Loja|Sala|Box|Cobertura|Cob\.?|Fundos|Frente|Conj\.?)"
    r"\s+[\dA-Za-z][\dA-Za-z\-/º°]*",
    flags=re.IGNORECASE,
)

# Textos de referência ("ao lado de", "perto de", "casa de muro de pedra")
# que o cliente coloca como pista visual — irrelevante pro geocoder.
_REFERENCIA_RE = re.compile(
    r"\s+(?:proximo\s+a?|próximo\s+a?|perto\s+de|ao\s+lado\s+de|em\s+frente|"
    r"sem\s+saída|sem\s+saida|no\s+final\s+da\s+rua|casa\s+de\s+muro)"
    r".*?(?=\s+\d{5}-?\d{3}|\s+(?:" + "|".join(CIDADES) + r")\b|$)",
    flags=re.IGNORECASE,
)

# Nome do CD que às vezes vem no endereço como referência interna.
_CD_REF_RE = re.compile(r"\s+ORGANICO\s+DO\s+CHICO", flags=re.IGNORECASE)


def _gerar_variacoes(endereco: str) -> list[str]:
    """Retorna 1-3 variações do endereço pra tentar em cascata.

    Da mais completa (v1, com bairro) pra mais simples (v3, só CEP +
    cidade). Cada variação é uma query independente pro geocoder. Quando
    o endereço bruto está limpo, todas as variações podem coincidir —
    a função deduplica antes de devolver.
    """
    if not endereco:
        return []

    s = _expandir_abrev(endereco.strip())
    s = _CD_REF_RE.sub(" ", s)
    s_limpo = _COMPLEMENTO_RE.sub(" ", s)
    s_limpo = _REFERENCIA_RE.sub(" ", s_limpo)
    s_limpo = re.sub(r"\s+", " ", s_limpo).strip(" ,;")

    # Extrai CEP (padrão XXXXX-XXX ou XXXXXXXX)
    cep_match = re.search(r"\b(\d{5})-?(\d{3})\b", s_limpo)
    cep = f"{cep_match.group(1)}-{cep_match.group(2)}" if cep_match else None

    # Extrai cidade (último match conhecido na string)
    cidade = None
    for c in CIDADES:
        if re.search(r"\b" + re.escape(c) + r"\b\s*$", s_limpo, flags=re.IGNORECASE):
            cidade = c
            break
    cidade = cidade or "Belo Horizonte"

    # Tira CEP e cidade pra ficar com "rua/número/bairro"
    nucleo = s_limpo
    if cep_match:
        nucleo = nucleo[:cep_match.start()] + " " + nucleo[cep_match.end():]
    for c in CIDADES:
        nucleo = re.sub(r"\b" + re.escape(c) + r"\b\s*$", "", nucleo, flags=re.IGNORECASE)
    nucleo = re.sub(r"\s+", " ", nucleo).strip(" ,;")

    # Tenta separar "rua/número" do "bairro" pelo primeiro número de prédio
    rua_num, bairro = nucleo, None
    m = re.search(r"^(.+?\b\d{1,5}[A-Za-z]?)\s+(.+)$", nucleo)
    if m and not re.search(r"\d", m.group(2)):
        # O resto NÃO tem número → provavelmente é o bairro
        rua_num = m.group(1)
        bairro = m.group(2).strip()

    # Versão 1: rua/número, bairro, cidade, MG
    partes = [rua_num]
    if bairro:
        partes.append(bairro)
    partes += [cidade, "MG", "Brasil"]
    v1 = ", ".join(partes)

    # Versão 2: sem bairro
    v2 = ", ".join([rua_num, cidade, "MG", "Brasil"])

    # Versão 3: só CEP + cidade (último recurso — costuma cair no
    # centroide da rua; impreciso mas melhor que nada).
    v3 = f"{cep}, {cidade}, MG, Brasil" if cep else None

    out = []
    for v in (v1, v2, v3):
        if v and v not in out:
            out.append(v)
    return out


# ── Consultas aos provedores ─────────────────────────────────
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


def _consultar_photon(endereco: str) -> tuple[float, float] | None:
    """Fallback: Photon (Komoot) — usa OSM mas tem parser mais tolerante
    pra queries bagunçadas. Sem rate limit fixo, mas "be nice"."""
    try:
        r = requests.get(
            f"{PHOTON_URL}/api/",
            params={"q": endereco, "limit": 1, "lang": "pt"},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning("photon falhou: %s", e)
        return None
    feats = (data or {}).get("features") or []
    if not feats:
        return None
    try:
        # GeoJSON: coordinates = [lng, lat]
        lng, lat = feats[0]["geometry"]["coordinates"]
        return float(lat), float(lng)
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _consultar_brasilapi_cep(cep: str) -> tuple[float, float] | None:
    """Fallback automático pelo CEP: BrasilAPI v2 retorna coordenadas do
    centroide do CEP quando disponível. Precisão típica: ~50m (faixa do
    CEP), boa o suficiente pro CVRP. Sem rate limit / sem cadastro.
    Retorna None se o CEP não existir ou se a v2 não tiver coordenadas
    (algumas faixas de CEP ainda não foram mapeadas)."""
    cep_num = re.sub(r"\D", "", cep or "")
    if len(cep_num) != 8:
        return None
    try:
        r = requests.get(
            f"https://brasilapi.com.br/api/cep/v2/{cep_num}",
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning("brasilapi cep %s falhou: %s", cep_num, e)
        return None
    try:
        loc = (data or {}).get("location", {}).get("coordinates", {})
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None or lat == "" or lng == "":
            return None
        return float(lat), float(lng)
    except (KeyError, ValueError, TypeError):
        return None


def _consultar_centroide_bairro(bairro: str, cidade: str) -> tuple[float, float] | None:
    """Último recurso automático: pega o centroide do BAIRRO pelo Nominatim
    ("Belvedere, Belo Horizonte, MG, Brasil"). Erro típico: 300-800m, mas
    o entregador conhece o bairro e a casa fica perto disso. Pro CVRP de
    120 paradas em escala de cidade, esse erro é tolerável (melhor que
    deixar a entrega de fora)."""
    if not bairro:
        return None
    q = f"{bairro}, {cidade or 'Belo Horizonte'}, MG, Brasil"
    return _consultar_nominatim(q)


def _extrair_cep(endereco: str) -> str | None:
    m = re.search(r"\b(\d{5})-?(\d{3})\b", endereco or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _extrair_bairro_cidade(endereco: str) -> tuple[str | None, str]:
    """Pega o bairro (token entre número e CEP/cidade) e a cidade do
    endereço bruto. Heurística simples — usada só pro fallback do
    centroide do bairro quando tudo mais falhou."""
    s = _expandir_abrev(endereco or "").strip()
    s = _CD_REF_RE.sub(" ", s)
    s = _COMPLEMENTO_RE.sub(" ", s)
    s = _REFERENCIA_RE.sub(" ", s)

    # Cidade no fim
    cidade = "Belo Horizonte"
    for c in CIDADES:
        if re.search(r"\b" + re.escape(c) + r"\b\s*$", s, flags=re.IGNORECASE):
            cidade = c
            s = re.sub(r"\b" + re.escape(c) + r"\b\s*$", "", s, flags=re.IGNORECASE)
            break
    # Remove CEP
    cep_m = re.search(r"\b\d{5}-?\d{3}\b", s)
    if cep_m:
        s = s[:cep_m.start()] + " " + s[cep_m.end():]
    # O bairro é o que sobra depois do último número (rua/casa)
    nucleo = re.sub(r"\s+", " ", s).strip(" ,;")
    m = re.search(r".*\b\d{1,5}[A-Za-z]?\s+(.+)$", nucleo)
    bairro = m.group(1).strip(" ,;") if m else None
    if bairro and re.search(r"\d", bairro):
        bairro = None   # ainda tem número, não é bairro puro
    return bairro, cidade


def _consultar_em_cascata(endereco: str) -> tuple[float, float] | None:
    """Tenta o endereço em vários provedores até achar uma coordenada.

    Ordem:
      1. Nominatim com variações limpas (v1, v2, v3) — preciso quando funciona
      2. Photon (Komoot) — parser mais tolerante pra queries bagunçadas
      3. BrasilAPI v2 pelo CEP — centroide do CEP (~50m de erro)
      4. Nominatim por bairro + cidade — centroide do bairro (~500m de erro,
         último recurso pra entrega não ficar de fora)

    A precisão cai conforme avança nos fallbacks. O CVRP em escala de
    cidade tolera erros de até ~500m sem mudança prática nas rotas.
    Pausa entre consultas pra ser amigável com os servidores públicos."""
    variacoes = _gerar_variacoes(endereco)
    if not variacoes:
        return None

    # 1. Nominatim com variações
    for i, v in enumerate(variacoes):
        coord = _consultar_nominatim(v)
        time.sleep(PAUSA_S)
        if coord is not None:
            return coord

    # 2. Photon
    coord = _consultar_photon(variacoes[0])
    if coord is not None:
        log.info("geocode via Photon: %s", endereco)
        return coord
    time.sleep(PAUSA_S / 2)

    # 3. BrasilAPI v2 pelo CEP (centroide ~50m)
    cep = _extrair_cep(endereco)
    if cep:
        coord = _consultar_brasilapi_cep(cep)
        if coord is not None:
            log.info("geocode aproximado via CEP (BrasilAPI): %s", endereco)
            return coord

    # 4. Centroide do bairro (~500m)
    bairro, cidade = _extrair_bairro_cidade(endereco)
    if bairro:
        coord = _consultar_centroide_bairro(bairro, cidade)
        time.sleep(PAUSA_S)
        if coord is not None:
            log.warning("geocode APROXIMADO pelo bairro %s/%s: %s",
                        bairro, cidade, endereco)
            return coord

    # 5. Centroide da cidade (~1-2km) — último recurso. Pega TUDO que
    # tem cidade conhecida e evita ficar de fora da rota. Erro maior:
    # o entregador pode precisar conferir o endereço na mão depois.
    if cidade:
        coord = _consultar_nominatim(f"{cidade}, MG, Brasil")
        time.sleep(PAUSA_S)
        if coord is not None:
            log.warning("geocode MUITO APROXIMADO pela cidade %s (verificar!): %s",
                        cidade, endereco)
            return coord

    return None


# ── API pública ─────────────────────────────────────────────
def geocodificar(endereco: str, cache: dict | None = None) -> tuple[float, float] | None:
    """Endereço → (lat, lng), passando pelo cache em disco. Devolve None
    quando nenhuma das variações (Nominatim em cascata + fallback Photon)
    encontra o endereço.

    Se `cache` for passado, opera nesse dict e NÃO salva (o chamador
    salva no fim — útil pra geocodificar uma lista sem I/O por item).
    Só dorme quando bate na rede; cache hit é instantâneo."""
    chave = _normalizar(endereco)
    if not chave:
        return None
    proprio = cache is None
    if proprio:
        cache = carregar_cache()
    if chave in cache:
        v = cache[chave]
        return tuple(v) if v else None

    coord = _consultar_em_cascata(endereco)
    cache[chave] = list(coord) if coord else None
    if proprio:
        salvar_cache(cache)
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


def limpar_falhas(cache: dict | None = None) -> int:
    """Remove do cache as entradas que falharam (valor=None). Útil quando
    a lógica de normalização melhorou e queremos forçar re-tentativa dos
    endereços que tinham falhado antes. Retorna o número de chaves removidas."""
    proprio = cache is None
    if proprio:
        cache = carregar_cache()
    removidas = [k for k, v in cache.items() if v is None]
    for k in removidas:
        del cache[k]
    if proprio:
        salvar_cache(cache)
    return len(removidas)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    testes = [
        # Endereços bagunçados do Instabuy reais
        "Rua Cacuera 846 Apt 302 Jaraguá 31270-520 Belo Horizonte",
        "R. Min. Hermenegildo de Barros, 785 785 Itapoã 31710-120 Belo Horizonte",
        "Rua Adilson Paulo de Souza 133 casa de muro de pedra São João Batista 31515-270 Belo Horizonte",
        "Ferreira Viana 65 ORGANICO DO CHICO Salgado Filho 30550-150 Belo Horizonte",
        # Normalizado, deve funcionar direto
        "Rua Ferreira Viana, 65, Salgado Filho, Belo Horizonte, MG",
    ]
    for end in testes:
        variacoes = _gerar_variacoes(end)
        print(f"\n📍 {end}")
        for v in variacoes:
            print(f"   → {v!r}")
        print(f"   coord: {geocodificar(end)}")
