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


def _diff_angular(a_lat, a_lng, b_lat, b_lng, ref_lat, ref_lng):
    """Diferença angular (radianos) entre 2 pontos vistos do ponto de
    referência. Retorna em [0, π]."""
    ang_a = math.atan2(a_lat - ref_lat, a_lng - ref_lng)
    ang_b = math.atan2(b_lat - ref_lat, b_lng - ref_lng)
    diff = ((ang_a - ang_b + math.pi) % (2 * math.pi)) - math.pi
    return abs(diff)


def _custo_atribuicao(e_lat, e_lng, c_lat, c_lng, cd_lat, cd_lng, peso_ang):
    """Custo pra atribuir entrega a centróide:
      dist²(entrega, centróide) + peso_ang × diff_angular(entrega, centróide vistos do CD)²

    Sem penalty (peso_ang=0), vira K-means clássico. Com peso_ang > 0,
    entregas em direções diferentes do centróide (a partir do CD) pagam
    custo extra — evita o problema de cluster perto do CD virar uma
    'estrela' que pega pontos em direções opostas."""
    d2 = _dist2(e_lat, e_lng, c_lat, c_lng)
    if peso_ang <= 0:
        return d2
    diff = _diff_angular(e_lat, e_lng, c_lat, c_lng, cd_lat, cd_lng)
    return d2 + peso_ang * diff * diff


def _seed_setorial(entregas, cd, m):
    """Inicializa m centróides em DIREÇÕES diferentes a partir do CD.

    Divide o espaço angular ao redor do CD em m setores iguais (cada um
    de 360°/m). Pra cada setor, pega a entrega MAIS DISTANTE do CD nesse
    setor como seed. Setores vazios usam um ponto sintético na direção
    média do setor (raio = média das distâncias do CD).

    Vantagem sobre k-means++: garante diversidade angular dos clusters,
    evita o problema "centróide cai no meio de várias direções e cluster
    fica espalhado pegando extremos opostos da cidade".
    """
    n = len(entregas)
    if n == 0 or m == 0:
        return []
    if n <= m:
        # Cada entrega vira seed (e setores extras ficam vazios — tratados
        # pelo caller)
        return [(e.lat, e.lng) for e in entregas]

    # Ângulo + distância² de cada entrega ao CD
    pts = []
    for i, e in enumerate(entregas):
        ang = math.atan2(e.lat - cd.lat, e.lng - cd.lng)  # [-π, π]
        d2 = _dist2(e.lat, e.lng, cd.lat, cd.lng)
        pts.append((ang, d2, i))

    setor = 2 * math.pi / m
    seeds = []
    dist_media = (sum(p[1] for p in pts) / n) ** 0.5  # pra setores vazios
    for k in range(m):
        ang_min = -math.pi + k * setor
        ang_max = -math.pi + (k + 1) * setor
        no_setor = [(d2, i) for ang, d2, i in pts if ang_min <= ang < ang_max]
        if no_setor:
            _, i_mais_dist = max(no_setor, key=lambda t: t[0])
            seeds.append((entregas[i_mais_dist].lat, entregas[i_mais_dist].lng))
        else:
            # Setor vazio: ponto sintético na direção média
            ang_meio = (ang_min + ang_max) / 2
            seeds.append((
                cd.lat + dist_media * math.sin(ang_meio),
                cd.lng + dist_media * math.cos(ang_meio),
            ))
    return seeds


def _diametro_km(cluster, cd):
    """Estimativa do tamanho geográfico do cluster: maior distância haversine
    entre 2 paradas do cluster (km). Cluster vazio ou 1 ponto → 0."""
    if len(cluster) < 2:
        return 0.0
    coords = [(e.lat, e.lng) for e in cluster]
    d_max = 0.0
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            d = _haversine_km(coords[i][0], coords[i][1], coords[j][0], coords[j][1])
            if d > d_max:
                d_max = d
    return d_max


def _rebalancear_por_km(clusters, cd,
                        fator_acima_media: float = 1.2,
                        span_paradas_max: int = 6,
                        max_movimentos: int = 60):
    """Pós-processamento: rota MUITO mais longa em km que a média perde
    suas paradas mais externas (mais distantes do centróide) pra rotas
    com menos km que aceitem (mais próximas da casa daquela parada).

    Aceita span maior em paradas (até span_paradas_max) em troca de
    reduzir km da rota gigante. Estado-objetivo: nenhuma rota com
    diâmetro > fator_acima_media × média_dos_outros.
    """
    if not clusters or len(clusters) < 2:
        return clusters

    for _ in range(max_movimentos):
        diams = [_diametro_km(c, cd) for c in clusters]
        # Cluster vazio ou 1 ponto não conta na média
        diams_validos = [d for d, c in zip(diams, clusters) if len(c) >= 2]
        if not diams_validos:
            break
        media = sum(diams_validos) / len(diams_validos)

        # Cluster maior em diâmetro
        i_grande = max(range(len(clusters)), key=lambda i: diams[i])
        if diams[i_grande] <= media * fator_acima_media:
            break  # já equilibrado

        cluster_grande = clusters[i_grande]
        if len(cluster_grande) < 3:
            break  # não vale tirar de cluster tão pequeno

        # Identifica a parada mais "fora" (mais distante do centróide)
        lat_c = sum(e.lat for e in cluster_grande) / len(cluster_grande)
        lng_c = sum(e.lng for e in cluster_grande) / len(cluster_grande)
        idx_fora = max(
            range(len(cluster_grande)),
            key=lambda i: _haversine_km(cluster_grande[i].lat, cluster_grande[i].lng, lat_c, lng_c)
        )
        parada_fora = cluster_grande[idx_fora]

        # Escolhe cluster destino: tem que ter (a) menos km que a média;
        # (b) ser o cluster mais próximo da parada_fora dentro dessa filtragem;
        # (c) span de paradas resultante ≤ span_paradas_max
        candidatos = []
        for j, c in enumerate(clusters):
            if j == i_grande:
                continue
            # Cluster destino acumula 1 parada extra
            tam_novo = len(c) + 1
            tam_grande_novo = len(cluster_grande) - 1
            tams_novos = [len(cl) for cl in clusters]
            tams_novos[j] = tam_novo
            tams_novos[i_grande] = tam_grande_novo
            span_novo = max(tams_novos) - min(tams_novos)
            if span_novo > span_paradas_max:
                continue
            # Distância do destino: usa centróide do cluster destino
            if c:
                lat_d = sum(e.lat for e in c) / len(c)
                lng_d = sum(e.lng for e in c) / len(c)
                d_dest = _haversine_km(parada_fora.lat, parada_fora.lng, lat_d, lng_d)
            else:
                d_dest = float('inf')
            candidatos.append((d_dest, j))

        if not candidatos:
            break  # nenhum destino aceita

        candidatos.sort(key=lambda t: t[0])
        d_dest, j_dest = candidatos[0]
        # Só move se o destino está consideravelmente mais próximo da parada
        # do que o cluster atual (senão piora geral)
        d_atual = _haversine_km(parada_fora.lat, parada_fora.lng, lat_c, lng_c)
        if d_dest >= d_atual:
            break  # nenhum destino melhora — para o rebalanceamento

        # Move
        clusters[i_grande].pop(idx_fora)
        clusters[j_dest].append(parada_fora)

    return clusters


def kmeans_balanced(entregas, cd, m: int, *,
                     max_iter: int = 30,
                     peso_angular: float = 0.1,
                     rebalancear_km: bool = True) -> list[list]:
    """K-means com tamanho-alvo fixo + init setorial + penalty angular +
    rebalanceamento opcional por km (move paradas externas de rotas
    gigantes pra rotas concentradas).

    Args:
      entregas: lista de Entrega.
      cd: CD (referência pra init setorial e penalty angular).
      m: número de clusters.
      max_iter: máximo de iterações do K-means.
      peso_angular: peso da diferença angular ao CD no custo de atribuição.
      rebalancear_km: se True, depois do K-means roda pós-processamento
        que tira paradas mais externas de clusters com diâmetro acima de
        1.4× a média e move pra clusters mais próximos da parada (com
        cap de span de paradas em 6).

    Returns:
      Lista de m listas de Entrega.
    """
    n = len(entregas)
    if n == 0 or m <= 0:
        return [[] for _ in range(m)]
    if m >= n:
        return [[e] for e in entregas] + [[] for _ in range(m - n)]

    base = n // m
    resto = n % m
    tamanhos = [base + (1 if i < resto else 0) for i in range(m)]

    centroides = _seed_setorial(entregas, cd, m)

    atribuicao_prev = None
    for _iter in range(max_iter):
        candidatos = sorted(
            (
                (_custo_atribuicao(e.lat, e.lng, c[0], c[1],
                                    cd.lat, cd.lng, peso_angular), i, j)
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

        if atribuicao == atribuicao_prev:
            break
        atribuicao_prev = atribuicao

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

    clusters = [
        [entregas[i] for i in range(n) if atribuicao[i] == j]
        for j in range(m)
    ]
    if rebalancear_km:
        clusters = _rebalancear_por_km(clusters, cd)
    return clusters


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
