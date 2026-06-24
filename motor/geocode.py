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
import math
import os
import re
import time
import unicodedata

import requests

log = logging.getLogger(__name__)

NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
PHOTON_URL = os.environ.get(
    "PHOTON_URL", "https://photon.komoot.io"
).rstrip("/")
USER_AGENT = os.environ.get("GEOCODE_USER_AGENT", "roteirizacao-entregas/1.0")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
CACHE_PATH = os.environ.get(
    "GEOCODE_CACHE",
    os.path.join(os.path.dirname(__file__), "..", "dados", "geocode.cache.json"),
)
PAUSA_S = 1.5  # respeita o limite de ~1 req/s do Nominatim público (com folga)
# Tentativas em 429 com backoff exponencial. Em rodadas pesadas (purga em
# lote), o Nominatim publico rate-limita feio e cada retry custa
# 5+10+20s = 35s. Setar NOMINATIM_MAX_RETRY=1 (so 1 tentativa, sem retry)
# faz falhar rapido e cair pro Google Maps no fallback — bem mais eficiente
# quando GOOGLE_MAPS_API_KEY ta setada.
MAX_RETRY_429 = int(os.environ.get("NOMINATIM_MAX_RETRY", "3"))

# ── Validacao geografica (anti rua-homonima de OUTRA cidade) ──────────
# Todas as entregas sao na Regiao Metropolitana de BH. Um endereco que
# geocodifica para um ponto a mais de MAX_RAIO_KM do centro de BH e quase
# sempre rua homonima em outra cidade (ex.: "Rua Goias" tambem existe em
# Divinopolis, ~100km) — um provedor (em especial o Google, chamado com o
# texto BRUTO) pode devolver esse ponto e o motor o aceitava calado,
# ENVENENANDO o cache: a chave canonica ignora a cidade, entao o ponto
# errado passa a servir o endereco de BH pra sempre. Aqui a coord e
# validada ANTES de ser aceita/cacheada. Raio ajustavel por env.
BH_CENTRO_LAT = -19.9191
BH_CENTRO_LNG = -43.9387
MAX_RAIO_KM = float(os.environ.get("GEOCODE_MAX_RAIO_KM", "80"))

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
    """Normalizacao TRIVIAL — minusculo, espacos colapsados. Mantida pra
    compatibilidade interna e logs. NAO eh mais a chave de cache (que
    agora usa _chave_canonica)."""
    return " ".join((endereco or "").strip().lower().split())


# Cidades da RMBH normalizadas (sem acento, lower) — pra remover da chave
_CIDADES_NORM = set()
for _c in [
    "belo horizonte", "contagem", "nova lima", "sabara", "betim",
    "lagoa santa", "santa luzia", "ribeirao das neves", "vespasiano",
    "ibirite", "brumadinho", "confins", "pedro leopoldo", "esmeraldas",
    "mateus leme", "caete", "jaboticatubas", "itabirito",
]:
    _CIDADES_NORM.add(_c)

# Tipos de via que sao genericos — descartar na chave canonica
_TIPOS_VIA = {"rua", "r", "avenida", "av", "alameda", "al", "praca", "pca",
              "travessa", "tv", "estrada", "estr", "rodovia", "rod", "via",
              "largo", "ladeira"}

# Stopwords de UF/pais
_STOPWORDS_FIM = {"mg", "br", "brasil"}

# Regex pra complementos: "apto 302", "casa 5", "bl 2", "sala 101", etc.
# Captura ate o proximo token alfanumerico curto.
_COMP_RE = re.compile(
    r"\b(?:apto?\.?|apartamento|ap\.?|apart\.?|apro\.?|casa|cs|bloco|bl\.?|"
    r"sala|loja|box|cobertura|cob\.?|fundos|frente|conj\.?|edif\.?|edificio|"
    r"predio|prédio|andar)"
    r"[\s.:\-]*[\dA-Za-z][\dA-Za-z\-/]*\b",
    flags=re.IGNORECASE,
)

# Regex pra CEP
_CEP_RE = re.compile(r"\b\d{5}-?\d{3}\b")


def _chave_canonica(endereco: str) -> str:
    """Chave de cache ROBUSTA a variacoes de formato do mesmo endereco fisico.

    Mesmo predio em formatos diferentes (Instabuy raw vs export XLSX vs
    formatos com bairro no inicio etc.) colapsa pra mesma chave.

    Algoritmo:
      1. Tira acento + minusculo
      2. Remove CEP, complementos (apto X, sala Y, casa, bl, etc.), cidade,
         estado (mg/br/brasil)
      3. Remove tipos genericos de via (rua, av, alameda, etc.)
      4. Extrai o PRIMEIRO numero significativo (1-5 digitos) como numero
         da rua. Tudo o que vier como numero depois eh descartado (sao
         complementos sem palavra-chave).
      5. Tokens restantes (nome de rua + bairro embaralhados) sao ORDENADOS
         alfabeticamente — descarta a ordem (que varia entre formatos).
      6. Chave final: tokens-ordenados + "|n" + numero.

    Trade-off: ruas homonimas em bairros diferentes (raro) com mesmo numero
    poderiam colapsar — mas como incluimos bairro nos tokens, na pratica
    nao acontece.
    """
    if not endereco:
        return ""
    s = endereco.strip().lower()
    # Remove acentos
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    # Tira CEP, complementos
    s = _CEP_RE.sub(" ", s)
    s = _COMP_RE.sub(" ", s)
    # Tira pontuacao
    s = re.sub(r"[,;.\-/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Remove cidades (de mais longa pra mais curta pra nao casar "santa" em "santa luzia")
    for cidade in sorted(_CIDADES_NORM, key=len, reverse=True):
        s = re.sub(r"\b" + re.escape(cidade) + r"\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Tokens
    tokens = s.split()
    numero = ""
    tokens_sem_num = []
    for t in tokens:
        if t.isdigit():
            if not numero and 1 <= len(t) <= 5:
                numero = t
            # numeros secundarios descartados (complementos sem palavra-chave)
            continue
        if t in _TIPOS_VIA or t in _STOPWORDS_FIM:
            continue
        # Tira tokens com 1 letra (provavelmente ruido)
        if len(t) <= 1:
            continue
        tokens_sem_num.append(t)
    tokens_sem_num.sort()
    chave_texto = " ".join(tokens_sem_num)
    if numero:
        return f"{chave_texto}|n{numero}"
    return chave_texto


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
# Aceita separador `:`, `.`, `-` OU espaço entre palavra-chave e número
# (ex: "Apt:801", "Apto.5", "Ap-12B").
_COMPLEMENTO_RE = re.compile(
    r"\s+(?:Apt\.?|Apto\.?|Ap\.?|Apartamento|Apart\.?|Apro\.?|Casa|Bl\.?|Bloco|Andar|"
    r"Predio|Prédio|Loja|Sala|Box|Cobertura|Cob\.?|Fundos|Frente|Conj\.?)"
    r"[\s:.\-]+[\dA-Za-z][\dA-Za-z\-/º°]*",
    flags=re.IGNORECASE,
)

# Captura "número/número" (apartamento) ou "número-letra/número" — comum no
# Instabuy, ex: "253/401" significa "número 253, apto 401".
_NUM_BARRA_RE = re.compile(r"(\d+)/[\dA-Za-z]+", flags=re.IGNORECASE)

# Número duplicado: "718 718" ou "6 235" (rua,número  número-extra) —
# o segundo é complemento/apartamento sem palavra-chave. Pega só o primeiro.
_NUM_DUP_RE = re.compile(r"(\b\d{1,5})(\s+\d{2,5}\b)+")

# "35 402B" — número (rua) + número-letra (apartamento) sem palavra-chave.
# Comum no Instabuy. Pega só o primeiro número.
_NUM_APT_LETRA_RE = re.compile(r"(\b\d{1,5})\s+\d{1,5}[A-Za-z]\b")

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
    # "253/401" → "253" (pega só o número da rua; depois da barra é apto)
    s_limpo = _NUM_BARRA_RE.sub(r"\1", s_limpo)
    # "Rua X 718 718 2301" → "Rua X 718" (Instabuy duplica número + apto)
    s_limpo = _NUM_DUP_RE.sub(r"\1", s_limpo)
    # "Rua X 35 402B" → "Rua X 35" (número da rua + apto sem palavra-chave)
    s_limpo = _NUM_APT_LETRA_RE.sub(r"\1", s_limpo)
    s_limpo = re.sub(r"\s+", " ", s_limpo).strip(" ,;")

    # Extrai CEP (padrão XXXXX-XXX ou XXXXXXXX)
    cep_match = re.search(r"\b(\d{5})-?(\d{3})\b", s_limpo)
    cep = f"{cep_match.group(1)}-{cep_match.group(2)}" if cep_match else None

    # Extrai cidade — primeiro tira UF/CEP do fim ("...Nova Lima MG 34006-043"
    # vira "...Nova Lima") pra a regex ancorada no fim conseguir achar
    # cidades diferentes de Belo Horizonte. Sem isso, todo endereco fora de
    # BH caia no fallback "Belo Horizonte".
    s_pra_cidade = _strip_uf_cep_fim(s_limpo)
    # Match acento-insensitivo: compara versao sem acento (Instabuy as vezes
    # vem "Sabara" sem acento; sem isso, "Sabará" da lista nao casa).
    s_norm = _sem_acento(s_pra_cidade)
    cidade = None
    for c in CIDADES:
        if re.search(r"\b" + re.escape(_sem_acento(c)) + r"\b\s*$",
                     s_norm, flags=re.IGNORECASE):
            cidade = c
            break
    cidade = cidade or "Belo Horizonte"

    # Tira CEP e cidade pra ficar com "rua/número/bairro"
    nucleo = s_limpo
    if cep_match:
        nucleo = nucleo[:cep_match.start()] + " " + nucleo[cep_match.end():]
    # Remove cidade (em qualquer posicao perto do fim, nao so ancorada
    # estritamente — UF/CEP entre cidade e fim ja foram limpos acima).
    # Acento-insensitivo: usa pattern que casa com e sem acento.
    nucleo = _strip_uf_cep_fim(nucleo)
    for c in CIDADES:
        pat = re.escape(_sem_acento(c))
        # substitui na versao sem-acento mas guarda os indices pra cortar
        # a string original. Mais simples: roda re.sub na string normalizada
        # e ao mesmo tempo na original com o mesmo offset.
        nucleo_norm = _sem_acento(nucleo)
        m = re.search(r"\b" + pat + r"\b\s*$", nucleo_norm, flags=re.IGNORECASE)
        if m:
            nucleo = nucleo[:m.start()].rstrip(" ,;")
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
def _consultar_nominatim(endereco: str,
                          exigir_rua: bool = True,
                          cep_esperado: str | None = None) -> tuple[float, float] | None:
    """Uma consulta ao Nominatim. Devolve (lat, lng) ou None se não achar.

    Quando `exigir_rua=True` (default), REJEITA resultados sem `road` no
    address (centroides de cidade). Sem isso, queries tipo
    "30575-365, Belo Horizonte, MG, Brasil" (CEP desconhecido) retornavam
    o centroide de BH (~-19.92, -43.94) — todos endereços assim viravam
    o mesmo ponto.

    Quando `cep_esperado` é passado, REJEITA resultados cujo postcode não
    bate (compara só os 5 primeiros dígitos pra tolerar formato). Usado
    pra queries só-CEP onde o resultado tem que estar realmente no CEP.

    Pra queries que SÃO de centroide de bairro/cidade (fallbacks
    intencionais), passar `exigir_rua=False`.
    """
    data = None
    for tentativa in range(MAX_RETRY_429):
        try:
            r = requests.get(
                f"{NOMINATIM_URL}/search",
                params={"q": endereco, "format": "json", "limit": 1,
                        "countrycodes": "br", "addressdetails": 1},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if r.status_code == 429:
                # Rate limit — espera Retry-After do header ou backoff exp
                espera = float(r.headers.get("Retry-After", 0)) or (2 ** tentativa * 5)
                espera = min(espera, 60)  # cap em 60s
                log.warning("Nominatim 429 (tentativa %d/%d), esperando %.1fs",
                            tentativa + 1, MAX_RETRY_429, espera)
                time.sleep(espera)
                continue
            r.raise_for_status()
            data = r.json()
            break
        except requests.RequestException as e:
            if tentativa + 1 >= MAX_RETRY_429:
                raise GeocodeError(f"falha ao consultar Nominatim: {e}") from e
            time.sleep(2 ** tentativa * 2)  # backoff curto pra outros erros
    if not data:
        return None
    try:
        item = data[0]
        addr = item.get("address") or {}
        if exigir_rua:
            if not any(addr.get(k) for k in ("road", "pedestrian", "path",
                                              "residential", "footway")):
                return None
        if cep_esperado:
            cep_query = re.sub(r"\D", "", cep_esperado)[:5]
            cep_result = re.sub(r"\D", "", addr.get("postcode") or "")[:5]
            if cep_query and cep_result != cep_query:
                return None
        return float(item["lat"]), float(item["lon"])
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


def _consultar_google_maps(endereco: str) -> tuple[float, float] | None:
    """Google Maps Geocoding API. Precisa de GOOGLE_MAPS_API_KEY na env.
    Cobertura excelente no Brasil; vale a pena quando Nominatim falha
    (endereços com ruído, complementos não-padrão, etc.). 10k requests/mês
    grátis no Google Cloud (pra volume maior, US$5/1000).

    Validações:
      - status == "OK" e há ao menos um result
      - tipo do match contém 'street_address', 'premise', 'subpremise',
        'point_of_interest' (não aceita só 'locality' ou 'route' sem
        número — equivalente ao exigir_rua=True do Nominatim).
    """
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": endereco, "region": "br",
                    "key": GOOGLE_MAPS_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning("google maps falhou em %s: %s", endereco[:50], e)
        return None
    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        # OVER_QUERY_LIMIT, REQUEST_DENIED, INVALID_REQUEST — log + None
        msg = data.get("error_message") or "(sem msg)"
        log.warning("google maps status=%s: %s — query: %s", status, msg, endereco[:50])
        return None
    results = data.get("results") or []
    if not results:
        return None
    item = results[0]
    # Validação de qualidade: só aceita resultados precisos (não centroide
    # de cidade/região). Tipos aceitos: street_address, premise (prédio),
    # subpremise (apartamento), point_of_interest (estabelecimento).
    tipos_validos = {"street_address", "premise", "subpremise",
                      "point_of_interest", "establishment"}
    if not (set(item.get("types") or []) & tipos_validos):
        return None
    try:
        loc = item["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])
    except (KeyError, TypeError, ValueError):
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
    deixar a entrega de fora). Não exige rua — é centroide de bairro
    por definição."""
    if not bairro:
        return None
    q = f"{bairro}, {cidade or 'Belo Horizonte'}, MG, Brasil"
    return _consultar_nominatim(q, exigir_rua=False)


def _extrair_cep(endereco: str) -> str | None:
    m = re.search(r"\b(\d{5})-?(\d{3})\b", endereco or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


# Padrao "MG"/"Minas Gerais" + CEP no fim — Instabuy gera
# "<rua> <num> <bairro> <CIDADE> <UF> <CEP>" e a deteccao da cidade
# precisa que UF + CEP estejam fora pra ancorar a cidade no fim.
_UF_CEP_FIM_RE = re.compile(
    r"\s*[,;.\-]?\s*(?:mg|minas gerais)\b\s*[,;.\-]?\s*"
    r"(?:\d{5}-?\d{3})?\s*$",
    flags=re.IGNORECASE,
)


def _sem_acento(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")


def _strip_uf_cep_fim(s: str) -> str:
    """Remove do FIM da string o UF (MG/Minas Gerais) + CEP se estiverem
    la. Sem isso, '... Nova Lima MG 34006-043' nao casa com a regex de
    cidade ancorada em '\\s*$' e cai no fallback 'Belo Horizonte'.

    Aplica em loop pra cobrir variantes ('Nova Lima, MG' + '34006-043',
    'Nova Lima 34006-043 MG', etc).
    """
    prev = None
    while s != prev:
        prev = s
        s = _UF_CEP_FIM_RE.sub("", s).rstrip(" ,;.-")
        # Tenta remover CEP solto no fim
        s = re.sub(r"\s*[,;.\-]?\s*\b\d{5}-?\d{3}\b\s*$", "", s).rstrip(" ,;.-")
    return s


def _extrair_bairro_cidade(endereco: str) -> tuple[str | None, str]:
    """Pega o bairro (token entre número e CEP/cidade) e a cidade do
    endereço bruto. Heurística simples — usada só pro fallback do
    centroide do bairro quando tudo mais falhou."""
    s = _expandir_abrev(endereco or "").strip()
    s = _CD_REF_RE.sub(" ", s)
    s = _COMPLEMENTO_RE.sub(" ", s)
    s = _REFERENCIA_RE.sub(" ", s)

    # Cidade no fim — primeiro tira UF/CEP pra cidade ficar ancorada no fim
    # ("...Nova Lima MG 34006-043" vira "...Nova Lima"). Sem isso, todo
    # endereco fora de BH caia no default "Belo Horizonte".
    # Match tambem acento-insensitivo ("Sabara" sem acento na string casa
    # com "Sabará" da lista CIDADES).
    s = _strip_uf_cep_fim(s)
    cidade = "Belo Horizonte"
    for c in CIDADES:
        s_norm = _sem_acento(s)
        m = re.search(r"\b" + re.escape(_sem_acento(c)) + r"\b\s*$",
                      s_norm, flags=re.IGNORECASE)
        if m:
            cidade = c
            s = s[:m.start()].rstrip(" ,;")
            break
    # Remove CEP (pode sobrar algum CEP fora do padrao do _strip_uf_cep_fim)
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


def _dist_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distancia aproximada (haversine) em km entre dois pontos."""
    raio = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * raio * math.asin(min(1.0, math.sqrt(a)))


def _coord_plausivel(coord: tuple[float, float] | None) -> bool:
    """True se a coord esta dentro do raio da RMBH (a partir do centro de
    BH). Rejeita pontos absurdamente longe — rua homonima em OUTRA cidade
    (ex.: Divinopolis). None nao e plausivel (quem chama trata antes)."""
    if not coord:
        return False
    try:
        d = _dist_km(BH_CENTRO_LAT, BH_CENTRO_LNG, float(coord[0]), float(coord[1]))
    except (TypeError, ValueError):
        return False
    return d <= MAX_RAIO_KM


def _consultar_em_cascata(endereco: str) -> tuple[float, float] | None:
    """Tenta o endereço em vários provedores até achar uma coordenada.

    Ordem:
      1. Nominatim variações v1 (rua+bairro) e v2 (rua sem bairro) — grátis
         e preciso quando o endereço está limpo
      2. Google Maps Geocoding (se GOOGLE_MAPS_API_KEY setada) — pago,
         resolve casos com ruído que confundem o Nominatim
      3. Nominatim v3 (só CEP) — valida postcode pra evitar centroide BH
      4. BrasilAPI v2 pelo CEP — centroide do CEP (~50m de erro)
      5. Nominatim por bairro + cidade — centroide do bairro (~500m de erro,
         último recurso pra entrega não ficar de fora)

    A precisão cai conforme avança nos fallbacks. O CVRP em escala de
    cidade tolera erros de até ~500m sem mudança prática nas rotas.
    Pausa entre consultas pra ser amigável com os servidores públicos."""
    variacoes = _gerar_variacoes(endereco)
    if not variacoes:
        return None

    cep = _extrair_cep(endereco)

    # Variações do Nominatim (tira a v3 só-CEP). A 1ª (v1) inclui o BAIRRO; as demais
    # (v2) são SEM bairro — e SEM bairro o Nominatim casa RUA HOMÔNIMA em outro ponto
    # da cidade (ex.: "Felipe Drumond" no Lajedo/norte em vez do Luxemburgo/sul). Por
    # isso a v2 só é tentada DEPOIS do Google (que é mais preciso e não cai no homônimo).
    nominatim_vars = [v for v in variacoes
                      if not (cep is not None and v.startswith(cep))]
    nominatim_v1 = nominatim_vars[:1]   # rua + BAIRRO (precisa)
    nominatim_v2 = nominatim_vars[1:]   # rua SEM bairro (arriscada: rua homônima)

    def _tentar_nominatim(lista):
        for v in lista:
            try:
                coord = _consultar_nominatim(v)
            except GeocodeError as e:
                log.warning("Nominatim falhou em variação (%s): %s", v[:50], e)
                coord = None
            time.sleep(PAUSA_S)
            if coord is not None:
                return coord
        return None

    # 1. Nominatim v1 (rua + BAIRRO) — preciso quando o endereço está limpo.
    coord = _tentar_nominatim(nominatim_v1)
    if coord is not None and _coord_plausivel(coord):
        return coord
    if coord is not None:
        log.warning("geocode Nominatim v1 FORA da RMBH (descartado): %s -> %s",
                    endereco[:60], coord)

    # 2. Google Maps — ANTES da v2 sem bairro: resolve ruído/erro de digitação
    # (ex.: "Drumond" x "Drummond") e NÃO cai em rua homônima. Só com a chave setada.
    if GOOGLE_MAPS_API_KEY:
        coord = _consultar_google_maps(endereco)
        if coord is not None and _coord_plausivel(coord):
            log.info("geocode via Google Maps: %s", endereco)
            return coord
        if coord is not None:
            log.warning("geocode Google FORA da RMBH (descartado, provavel rua "
                        "homonima de outra cidade): %s -> %s", endereco[:60], coord)

    # 3. Nominatim v2 (rua SEM bairro) — só agora, como fallback (pode cair em rua
    # homônima; por isso vem DEPOIS do Google).
    coord = _tentar_nominatim(nominatim_v2)
    if coord is not None and _coord_plausivel(coord):
        return coord
    if coord is not None:
        log.warning("geocode Nominatim v2 FORA da RMBH (descartado): %s -> %s",
                    endereco[:60], coord)

    # 3. Nominatim v3 (só CEP) — valida postcode pra evitar fallback BH
    if cep:
        v3 = f"{cep}, Belo Horizonte, MG, Brasil"
        try:
            coord = _consultar_nominatim(v3, cep_esperado=cep)
        except GeocodeError as e:
            log.warning("Nominatim CEP falhou: %s", e)
            coord = None
        time.sleep(PAUSA_S)
        if coord is not None and _coord_plausivel(coord):
            return coord

    # 4. BrasilAPI v2 pelo CEP (centroide ~50m)
    # 4. BrasilAPI v2 pelo CEP (centroide ~50m). Sem rate limit.
    if cep:
        coord = _consultar_brasilapi_cep(cep)
        if coord is not None and _coord_plausivel(coord):
            log.info("geocode aproximado via CEP (BrasilAPI): %s", endereco)
            return coord

    # 5. Centroide do bairro (~500m)
    bairro, cidade = _extrair_bairro_cidade(endereco)
    if bairro:
        try:
            coord = _consultar_centroide_bairro(bairro, cidade)
        except GeocodeError as e:
            log.warning("Centroide do bairro falhou em %s: %s", bairro, e)
            coord = None
        time.sleep(PAUSA_S)
        if coord is not None and _coord_plausivel(coord):
            log.warning("geocode APROXIMADO pelo bairro %s/%s: %s",
                        bairro, cidade, endereco)
            return coord

    # Fallback de "centroide da cidade" REMOVIDO — causava todos os
    # endereços não-resolvidos virarem o MESMO ponto no mapa (centroide
    # de BH ~ -19.92, -43.94), bagunçando o cluster e o TSP. Melhor
    # retornar None e deixar o endereço aparecer na lista de falhas pra
    # o usuário corrigir manualmente via editor de ponto (busca por
    # endereço ou coords).
    return None


# ── API pública ─────────────────────────────────────────────
def geocodificar(endereco: str, cache: dict | None = None) -> tuple[float, float] | None:
    """Endereço → (lat, lng), passando pelo cache em disco. Devolve None
    quando nenhuma das variações (Nominatim em cascata + fallback Photon)
    encontra o endereço.

    Se `cache` for passado, opera nesse dict e NÃO salva (o chamador
    salva no fim — útil pra geocodificar uma lista sem I/O por item).
    Só dorme quando bate na rede; cache hit é instantâneo."""
    chave = _chave_canonica(endereco)
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
    enderecos: list[str], progresso=None,
    salvar_a_cada: int = 10,
) -> dict[str, tuple[float, float] | None]:
    """Geocodifica uma lista de endereços: carrega o cache uma vez, consulta
    só os que faltam (com pausa entre requisições de rede), salva no fim
    E incrementalmente a cada `salvar_a_cada` consultas pra não perder
    progresso se o processo for interrompido.

    `progresso(feito, total)` é chamado a cada item, se fornecido.
    Retorno: {endereco_original: (lat,lng) | None}."""
    cache = carregar_cache()
    resultado: dict = {}
    total = len(enderecos)
    n_consultas_pendentes = 0
    houve_consulta = False
    for i, end in enumerate(enderecos, start=1):
        chave = _chave_canonica(end)
        if chave and chave not in cache:
            houve_consulta = True
            n_consultas_pendentes += 1
        resultado[end] = geocodificar(end, cache=cache)
        if progresso:
            progresso(i, total)
        # Salva incrementalmente pra não perder progresso em sync longo
        if n_consultas_pendentes >= salvar_a_cada:
            salvar_cache(cache)
            n_consultas_pendentes = 0
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


def purgar_centroides_genericos(
    cache: dict | None = None,
    tolerancia_metros: float = 50,
) -> tuple[int, list]:
    """Remove do cache entradas que caíram em centroides genéricos de cidade
    (ex: todos endereços em BH apontando pra (-19.919, -43.938) que é a
    Praça Sete genérica). Detecta valores que se repetem muito — qualquer
    coord usada por > 1 endereço dentro de `tolerancia_metros` é suspeita
    e vai pra purga.

    Retorna (n_removidas, [(lat, lng, n_apontamentos), ...]).
    """
    proprio = cache is None
    if proprio:
        cache = carregar_cache()

    # Agrupa coords por proximidade (chave quantizada ~50m).
    # 50m ≈ 0.00045° de latitude. Usamos 0.0005° = ~55m de bucket.
    bucket = max(0.0005, tolerancia_metros / 111000)
    from collections import defaultdict
    grupos = defaultdict(list)
    for k, v in cache.items():
        if not v:
            continue
        try:
            lat, lng = float(v[0]), float(v[1])
        except (TypeError, ValueError, IndexError):
            continue
        chave_q = (round(lat / bucket), round(lng / bucket))
        grupos[chave_q].append((k, lat, lng))

    # Qualquer bucket com >= 2 endereços apontando pra ele é suspeito.
    duplicados = []
    chaves_remover = set()
    for q, lista in grupos.items():
        if len(lista) >= 2:
            lat0, lng0 = lista[0][1], lista[0][2]
            duplicados.append((round(lat0, 6), round(lng0, 6), len(lista)))
            for k, _, _ in lista:
                chaves_remover.add(k)

    for k in chaves_remover:
        del cache[k]
    if proprio:
        salvar_cache(cache)
    duplicados.sort(key=lambda t: -t[2])
    return len(chaves_remover), duplicados


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
