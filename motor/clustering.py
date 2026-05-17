"""
Clustering geográfico das entregas (algoritmo Sweep adaptativo) +
atribuição cluster→entregador.

PASSO 1 — sweep_clusters(entregas, cd, m):
  - Calcula o ângulo de cada entrega em relação ao CD (atan2).
  - Encontra o MAIOR vazio angular entre entregas consecutivas — é onde
    começa a varredura (evita cortar bairros densos ao meio).
  - Varre do ponto de início no sentido anti-horário, agrupando entregas
    consecutivas até atingir o tamanho-alvo do cluster, então abre o próximo.
  - Tamanho-alvo: distribui n entregas em m clusters de forma balanceada.
    Cada cluster fica com floor(n/m) ou ceil(n/m) entregas (span máx 1).

Bairros densos viram fatias angulares pequenas; bairros esparsos viram
fatias grandes. Garante balanço de carga + coerência geográfica (vizinhos
angulares estão geograficamente próximos).

PASSO 2 — atribuir(clusters, entregadores):
  - Pareia cada cluster a um entregador (matching 1-pra-1).
  - Custo de parear cluster i ao entregador j:
      dist_haversine(centróide_i, casa_j) − peso × entregas_no_bairro_preferido
  - Assignment greedy: ordena todos os pares por custo crescente, atribui
    sequencialmente respeitando "cada cluster e entregador são únicos".

Pra m <= 15 entregadores (caso comum), greedy fica muito próximo do ótimo
e é O(m² log m). Pra escalas maiores, trocar por Hungarian (scipy).
"""

import math
from collections import Counter


def _haversine_km(a_lat, a_lng, b_lat, b_lng):
    """Distância em km — boa pra clustering radial em escala urbana."""
    R = 6371.0
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat))
         * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def sweep_clusters(entregas, cd, m: int) -> list[list]:
    """Sweep clustering: m clusters geograficamente coerentes e balanceados.

    Args:
      entregas: lista de Entrega (com .lat e .lng).
      cd: CD (com .lat e .lng).
      m: número de clusters desejado.

    Returns:
      Lista de m listas de Entrega. Cluster pode vir vazio se m > n.
    """
    n = len(entregas)
    if n == 0 or m <= 0:
        return [[] for _ in range(m)]

    # Ângulo polar de cada entrega ao CD. atan2(dy, dx) ∈ [-π, π].
    # Coords geográficas: lat = y, lng = x.
    def ang(e):
        return math.atan2(e.lat - cd.lat, e.lng - cd.lng)

    idx_ord = sorted(range(n), key=lambda i: ang(entregas[i]))
    angs = [ang(entregas[i]) for i in idx_ord]

    # Encontra o MAIOR vazio angular entre entregas consecutivas (wrap-around).
    # O ponto após esse gap é o início da varredura — assim não corta bairros.
    gaps = []
    for k in range(n):
        a_curr = angs[k]
        a_next = angs[(k + 1) % n]
        gap = (a_next - a_curr) % (2 * math.pi)
        gaps.append(gap)
    k_inicio = (gaps.index(max(gaps)) + 1) % n
    idx_ord = idx_ord[k_inicio:] + idx_ord[:k_inicio]

    # Tamanhos balanceados — alguns clusters com ceil(n/m), outros com floor.
    base = n // m
    resto = n % m
    tamanhos = [base + (1 if i < resto else 0) for i in range(m)]

    clusters = []
    pos = 0
    for tam in tamanhos:
        sub = [entregas[idx_ord[j]] for j in range(pos, pos + tam)]
        clusters.append(sub)
        pos += tam
    return clusters


def atribuir(clusters: list[list], entregadores, cd,
             peso_preferencia_km: float = 10.0) -> dict[int, int]:
    """Pareia cluster i a entregador j minimizando custo total. Greedy:
    ordena pares por custo crescente, atribui um por vez.

    Custo[i][j] = haversine(centroide_cluster_i, casa_entregador_j)
                  − peso_preferencia_km × n_entregas_no_cluster_em_bairro_de_pref_j

    Bairro normalizado (sem acento, lower). peso_preferencia_km=10 significa
    "1 entrega no bairro preferido vale 10km de desconto na distância" — peso
    razoável (forte mas não dominante).

    Returns:
      dict {cluster_idx: entregador_idx}. Tamanho == min(len(clusters), len(entregadores)).
    """
    import unicodedata
    def _norm(s):
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
        return " ".join(s.lower().split())

    m_c = len(clusters)
    m_e = len(entregadores)
    if m_c == 0 or m_e == 0:
        return {}

    # Centróide e Counter de bairros por cluster (clusters vazios = CD).
    centroides = []
    bairros_por_c = []
    for c in clusters:
        if c:
            lat = sum(e.lat for e in c) / len(c)
            lng = sum(e.lng for e in c) / len(c)
        else:
            lat, lng = cd.lat, cd.lng
        centroides.append((lat, lng))
        bairros_por_c.append(Counter(_norm(e.bairro) for e in c if e.bairro))

    # Preferências de cada entregador (set normalizado).
    prefs_por_e = [
        {_norm(p) for p in (ent.preferencias or []) if p}
        for ent in entregadores
    ]

    # Matriz de custos m_c × m_e. dist em km, bonus em "km equivalente".
    custos = []
    for i in range(m_c):
        ci_lat, ci_lng = centroides[i]
        linha = []
        for j in range(m_e):
            ent = entregadores[j]
            dist_km = _haversine_km(ci_lat, ci_lng, ent.lat, ent.lng)
            # Entregas do cluster cujo bairro está nas preferências do entregador
            n_match = sum(qtd for b, qtd in bairros_por_c[i].items()
                          if b in prefs_por_e[j])
            custo = dist_km - peso_preferencia_km * n_match
            linha.append(custo)
        custos.append(linha)

    # Greedy: ordena todos os pares (i, j) por custo crescente,
    # atribui respeitando "cada cluster 1 entregador e vice-versa".
    pares = sorted(
        ((custos[i][j], i, j) for i in range(m_c) for j in range(m_e)),
        key=lambda t: t[0],
    )
    atribuicao = {}
    usados_j = set()
    for _, i, j in pares:
        if i in atribuicao or j in usados_j:
            continue
        atribuicao[i] = j
        usados_j.add(j)
        if len(atribuicao) == min(m_c, m_e):
            break
    return atribuicao
