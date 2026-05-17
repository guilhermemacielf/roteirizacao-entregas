"""
Clustering geográfico das entregas (K-means equilibrado) + atribuição
cluster→entregador.

PASSO 1 — kmeans_balanced(entregas, cd, m):
  - K-means tradicional COM restrição de capacidade por cluster.
  - Inicialização k-means++ (centróides bem espalhados na 1ª iteração).
  - Atribuição capacitada: pra cada iteração, ordena (ponto, centróide)
    por distância crescente; atribui o ponto ao centróide mais próximo
    que ainda TENHA VAGA (cap = ceil(n/m)).
  - Recalcula centróides (média dos pontos do cluster).
  - Itera até convergir (atribuições estáveis) ou máx iterações.
  - Garante balanço (cada cluster ≤ ceil(n/m)) E compacidade (clusters
    naturalmente agrupam aglomerações).

Versão Sweep antiga (sweep_clusters) mantida pra referência/teste.

PASSO 1b — sweep_clusters(entregas, cd, m):
  - Versão antiga: divisão angular adaptativa.
  - Problema observado: corta bairros próximos que ficam na borda da fatia.
  - Mantido pra comparação.

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
import random
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


def _dist2(a_lat, a_lng, b_lat, b_lng):
    """Distância ao quadrado em coords lat/lng. Para clustering em escala
    urbana, equivalente à euclidiana — basta pra comparar/ordenar."""
    return (a_lat - b_lat) ** 2 + (a_lng - b_lng) ** 2


def _seed_kmeanspp(entregas, m, rng):
    """k-means++ seed: 1º centróide aleatório, próximos escolhidos com
    probabilidade proporcional a d²(ponto, centróide_mais_próximo).
    Reduz drasticamente chance de cair em ótimo local ruim."""
    n = len(entregas)
    if n == 0 or m == 0:
        return []
    seeds = [(entregas[rng.randrange(n)].lat, entregas[rng.randrange(n)].lng)]
    while len(seeds) < m:
        # Pra cada ponto, distância² ao seed mais próximo já escolhido
        d2 = [
            min(_dist2(e.lat, e.lng, s[0], s[1]) for s in seeds)
            for e in entregas
        ]
        total = sum(d2)
        if total <= 0:
            # Todos os pontos estão exatamente em seeds — escolhe aleatório
            i = rng.randrange(n)
        else:
            # Roleta proporcional a d²
            alvo = rng.random() * total
            acc = 0
            i = n - 1
            for k, v in enumerate(d2):
                acc += v
                if acc >= alvo:
                    i = k
                    break
        seeds.append((entregas[i].lat, entregas[i].lng))
    return seeds


def kmeans_balanced(entregas, cd, m: int, *,
                     max_iter: int = 30, seed: int = 42) -> list[list]:
    """K-means com restrição de capacidade por cluster.

    Args:
      entregas: lista de Entrega.
      cd: CD (usado só como tie-breaker em casos degenerados).
      m: número de clusters.
      max_iter: máximo de iterações (converge antes na maioria dos casos).
      seed: semente do RNG pra reprodutibilidade.

    Returns:
      Lista de m listas de Entrega, cada uma com floor(n/m) ou ceil(n/m).
    """
    n = len(entregas)
    if n == 0 or m <= 0:
        return [[] for _ in range(m)]
    if m >= n:
        # Cada entrega vira seu próprio cluster (clusters extras vazios).
        return [[e] for e in entregas] + [[] for _ in range(m - n)]

    # Tamanhos-alvo FIXOS: garante span máx 1 entre clusters.
    # Ex: n=107, m=8 → base=13, resto=3 → tamanhos=[14,14,14,13,13,13,13,13].
    base = n // m
    resto = n % m
    tamanhos = [base + (1 if i < resto else 0) for i in range(m)]

    rng = random.Random(seed)

    # Inicialização k-means++
    centroides = _seed_kmeanspp(entregas, m, rng)

    atribuicao_prev = None
    for _iter in range(max_iter):
        # Atribuição com tamanho-alvo: ordena (ponto, centróide) por d²
        # crescente, atribui respeitando tamanhos[j] EXATO por cluster.
        # Garante que todo cluster atinge seu tamanho-alvo (não fica
        # subutilizado como aconteceria só com cap máximo).
        candidatos = sorted(
            (
                (_dist2(e.lat, e.lng, c[0], c[1]), i, j)
                for i, e in enumerate(entregas)
                for j, c in enumerate(centroides)
            ),
            key=lambda t: t[0],
        )
        atribuicao = [-1] * n
        tamanho = [0] * m
        n_atrib = 0
        for _d, i, j in candidatos:
            if atribuicao[i] != -1 or tamanho[j] >= tamanhos[j]:
                continue
            atribuicao[i] = j
            tamanho[j] += 1
            n_atrib += 1
            if n_atrib == n:
                break

        # Convergência: atribuição idêntica à iteração anterior
        if atribuicao == atribuicao_prev:
            break
        atribuicao_prev = atribuicao

        # Recalcula centróides (média dos pontos do cluster).
        # Cluster vazio → mantém centróide anterior (não acontece com cap≥1).
        novos = []
        for j in range(m):
            pts = [entregas[i] for i in range(n) if atribuicao[i] == j]
            if pts:
                lat = sum(p.lat for p in pts) / len(pts)
                lng = sum(p.lng for p in pts) / len(pts)
                novos.append((lat, lng))
            else:
                novos.append(centroides[j])
        centroides = novos

    return [
        [entregas[i] for i in range(n) if atribuicao[i] == j]
        for j in range(m)
    ]


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
