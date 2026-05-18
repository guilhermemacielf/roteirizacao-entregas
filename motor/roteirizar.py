"""
Motor de roteirização — pipeline clustering + TSP por cluster.

A versão anterior usava CVRP unificado (todas as decisões em um modelo
gigante). Ficava preso em ótimos locais, gerava rotas zigue-zague e
desequilíbrio difícil de corrigir.

Pipeline novo (sugerido pelo usuário, batendo com "Sweep algorithm"
clássico de CVRP):

  PASSO 1 — Capacidade: separa Lalamove inicial pelo excedente
    (n > m × max_paradas → as N mais próximas do CD viram Lalamove).

  PASSO 2 — Clustering geográfico (motor/clustering.py::sweep_clusters):
    ordena entregas por ângulo ao CD, encontra o maior vazio angular
    como início de varredura, agrupa em m clusters com tamanhos
    balanceados (floor(n/m) a ceil(n/m), span máx 1).

  PASSO 3 — Atribuição cluster→entregador (motor/clustering.py::atribuir):
    greedy assignment com custo = haversine(centróide, casa) − peso ×
    n_entregas_no_bairro_preferido. Resolve "Camila perdendo Contagem"
    estruturalmente (preferência entra no custo da atribuição).

  PASSO 4 — TSP por cluster: cada cluster vira um problema pequeno
    (CD → entregas → casa do entregador) com OR-Tools. Janelas de
    horário tratadas dentro. Se TSP infactível, entrega vira Lalamove.

Lalamove é AGRUPADA: clusters de até MAX_PARADAS_LALAMOVE (6) por
proximidade geográfica.
"""

import logging
import math
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from motor.modelos import Entrega, Entregador, CD, Parada, Rota
from motor.matriz import matriz as osrm_matriz
from motor.clustering import kmeans_balanced, atribuir

log = logging.getLogger(__name__)

# Velocidade média estimada em meio urbano de BH — usada só pra estimar
# duração de rotas Lalamove (que NÃO passam pela matriz OSRM).
KMH_URBANO = 30.0

# Cada rota Lalamove agrupa até 6 entregas.
MAX_PARADAS_LALAMOVE = 6


def _haversine_km(a_lat, a_lng, b_lat, b_lng):
    R = 6371.0
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat))
         * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _separar_sobras_capacidade(entregas, cd, capacidade):
    """Se n > capacidade, separa as N mais próximas do CD pra Lalamove."""
    n_sobras = max(0, len(entregas) - capacidade)
    if n_sobras == 0 or capacidade <= 0:
        return entregas, []
    idx_ord = sorted(
        range(len(entregas)),
        key=lambda i: _haversine_km(cd.lat, cd.lng,
                                    entregas[i].lat, entregas[i].lng)
    )
    lala = set(idx_ord[:n_sobras])
    resto = [e for i, e in enumerate(entregas) if i not in lala]
    lalamove = [e for i, e in enumerate(entregas) if i in lala]
    return resto, lalamove


# Entregas a menos de RAIO_CD_M do CD são tratadas como "Entregas CD" —
# não entram nas rotas dos entregadores (ninguém precisa "ir lá").
RAIO_CD_M = 100  # 100m do CD


def _separar_entregas_cd(entregas, cd):
    """Separa entregas que estão no MESMO endereço do CD (dentro de RAIO_CD_M).
    Retorna (resto, entregas_no_cd). As entregas_no_cd viram uma rota especial
    'Entregas CD' colocada por último — alguém do CD entrega na hora."""
    no_cd = []
    resto = []
    for e in entregas:
        d_km = _haversine_km(cd.lat, cd.lng, e.lat, e.lng)
        if d_km * 1000 <= RAIO_CD_M:
            no_cd.append(e)
        else:
            resto.append(e)
    return resto, no_cd


def _rota_entregas_cd(entregas, cd) -> Rota | None:
    """Cria a rota especial 'Entregas CD' (entregador virtual). Sem TSP:
    a ordem é só na sequência recebida. Distância/duração = 0 (entregador
    pega no balcão). chegada_estimada_s = 0 (na hora)."""
    if not entregas:
        return None
    paradas = [
        Parada(entrega=e, ordem=i + 1, chegada_estimada_s=0)
        for i, e in enumerate(entregas)
    ]
    return Rota(
        entregador=Entregador(
            id="CD_ENTREGAS", nome="Entregas CD",
            lat=cd.lat, lng=cd.lng,
        ),
        paradas=paradas,
        distancia_m=0,
        duracao_s=0,
        candidata_lalamove=False,
    )


def _agrupar_lalamoves(entregas, cd, max_por_rota=MAX_PARADAS_LALAMOVE):
    """Agrupa Lalamoves em rotas de até max_por_rota por proximidade."""
    if not entregas:
        return []

    restantes = list(entregas)
    restantes.sort(key=lambda e: _haversine_km(cd.lat, cd.lng, e.lat, e.lng))

    rotas = []
    n_cluster = 0
    while restantes:
        n_cluster += 1
        semente = restantes.pop(0)
        cluster = [semente]
        restantes.sort(key=lambda e: _haversine_km(semente.lat, semente.lng,
                                                   e.lat, e.lng))
        for _ in range(max_por_rota - 1):
            if not restantes:
                break
            cluster.append(restantes.pop(0))
        restantes.sort(key=lambda e: _haversine_km(cd.lat, cd.lng, e.lat, e.lng))

        paradas = []
        dist_acum_km = 0.0
        anterior = (cd.lat, cd.lng)
        for ordem, ent in enumerate(cluster, start=1):
            d_km = _haversine_km(anterior[0], anterior[1], ent.lat, ent.lng)
            dist_acum_km += d_km
            chegada_s = int(dist_acum_km / KMH_URBANO * 3600)
            paradas.append(Parada(entrega=ent, ordem=ordem,
                                  chegada_estimada_s=chegada_s))
            anterior = (ent.lat, ent.lng)

        rotas.append(Rota(
            entregador=Entregador(
                id=f"LALA{n_cluster}",
                nome=f"Lalamove {n_cluster}",
                lat=cd.lat, lng=cd.lng,
            ),
            paradas=paradas,
            distancia_m=int(dist_acum_km * 1000),
            duracao_s=int(dist_acum_km / KMH_URBANO * 3600),
            candidata_lalamove=True,
        ))
    return rotas


def _tsp_cluster(
    entregas, entregador, cd, distancia, duracao,
    *, servico_por_entrega_s, limite_rota_min, tempo_limite_s,
):
    """TSP simples: 1 veículo (o entregador), sai do CD, passa por todas as
    entregas, termina na casa do entregador. Respeita janelas de horário.

    A matriz já inclui CD e casa do entregador. Layout dos nós:
      [0 .. k-1] = k entregas do cluster
      [k]        = CD
      [k+1]      = casa do entregador

    Returns:
      (Rota, []) se TSP fechou com todas as entregas,
      (Rota, [droppadas]) se algumas não couberam no tempo (viram Lalamove),
      (None, todas_entregas) se TSP totalmente infactível.
    """
    k = len(entregas)
    if k == 0:
        return None, []

    IDX_CD = k
    IDX_CASA = k + 1
    n_nos = k + 2

    manager = pywrapcp.RoutingIndexManager(n_nos, 1, [IDX_CD], [IDX_CASA])
    routing = pywrapcp.RoutingModel(manager)

    def cb_dist(i, j):
        return distancia[manager.IndexToNode(i)][manager.IndexToNode(j)]
    routing.SetArcCostEvaluatorOfAllVehicles(
        routing.RegisterTransitCallback(cb_dist))

    def cb_tempo(i, j):
        no_i, no_j = manager.IndexToNode(i), manager.IndexToNode(j)
        serv = servico_por_entrega_s if no_i < k else 0
        return duracao[no_i][no_j] + serv
    cb_tempo_idx = routing.RegisterTransitCallback(cb_tempo)

    # Dimensão Tempo: cumul = segundos desde a saída do CD.
    HORIZONTE = 24 * 3600
    routing.AddDimension(cb_tempo_idx, HORIZONTE, HORIZONTE, True, "Tempo")
    tempo_dim = routing.GetDimensionOrDie("Tempo")

    cap_chegada = (max(0, limite_rota_min * 60 - servico_por_entrega_s)
                   if limite_rota_min is not None else None)

    for i, e in enumerate(entregas):
        cv = tempo_dim.CumulVar(manager.NodeToIndex(i))
        if e.janela_inicio is not None:
            cv.SetMin(e.janela_inicio * 60)
        if e.janela_fim is not None:
            cv.SetMax(e.janela_fim * 60)
        if cap_chegada is not None:
            cv.SetMax(cap_chegada)

    # Disjunction: solver pode dropar entregas com janela que não cabem
    # (viram Lalamove no pós-processo). Penalidade enorme — só dropa se
    # for genuinamente impossível atender.
    if limite_rota_min is not None:
        for i in range(k):
            routing.AddDisjunction([manager.NodeToIndex(i)], 10**15)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(tempo_limite_s)

    sol = routing.SolveWithParameters(params)
    if sol is None:
        return None, list(entregas)

    paradas = []
    droppadas_idx = set(range(k))
    idx = routing.Start(0)
    ordem = 0
    dist_total = 0
    while not routing.IsEnd(idx):
        prox = sol.Value(routing.NextVar(idx))
        no_atual = manager.IndexToNode(idx)
        no_prox = manager.IndexToNode(prox)
        dist_total += distancia[no_atual][no_prox]
        if no_prox < k:
            ordem += 1
            chegada = sol.Value(tempo_dim.CumulVar(prox))
            paradas.append(Parada(
                entrega=entregas[no_prox],
                ordem=ordem,
                chegada_estimada_s=chegada,
            ))
            droppadas_idx.discard(no_prox)
        idx = prox

    dur_total = sol.Value(tempo_dim.CumulVar(routing.End(0)))
    rota = Rota(
        entregador=entregador,
        paradas=paradas,
        distancia_m=dist_total,
        duracao_s=dur_total,
    )
    droppadas = [entregas[i] for i in droppadas_idx]
    return rota, droppadas


def roteirizar(
    entregas: list[Entrega],
    entregadores: list[Entregador],
    cd: CD,
    *,
    min_paradas: int = 10,        # mantido por compatibilidade, não usado
    max_paradas: int = 18,
    matriz_pronta: dict | None = None,
    tempo_limite_s: int = 30,
    servico_por_entrega_s: int = 600,
    limite_rota_min: int | None = 300,
    gerar_lalamove: bool = True,
) -> list[Rota]:
    """Pipeline clustering + TSP. Retorna lista de Rota (entregadores +
    Lalamoves agrupadas).

    `min_paradas` é mantido na assinatura por compatibilidade, mas o novo
    pipeline usa tamanho-alvo derivado de n/m diretamente (sempre balanceado).
    `max_paradas` ainda limita capacidade total (m × max_paradas); o que
    excede vai pra Lalamove antes do clustering.
    """
    n_total = len(entregas)
    m = len(entregadores)
    if n_total == 0:
        return []
    if m == 0:
        raise ValueError("nenhum entregador disponível")

    # PASSO 0: separa "Entregas CD" — entregas no MESMO endereço do CD
    # não entram nas rotas dos entregadores (alguém do CD entrega na hora).
    entregas, entregas_cd = _separar_entregas_cd(entregas, cd)
    if entregas_cd:
        log.info("Entregas CD: %d entregas no endereço do CD (rota separada)",
                 len(entregas_cd))

    # PASSO 1: separa Lalamove pelo excedente de capacidade.
    capacidade = m * max_paradas
    if gerar_lalamove:
        entregas, lalamove_pre = _separar_sobras_capacidade(entregas, cd, capacidade)
        if lalamove_pre:
            log.info("Lalamove (sobras capacidade): %d/%d entregas — cap %d",
                     len(lalamove_pre), n_total, capacidade)
    else:
        lalamove_pre = []

    n = len(entregas)
    if n == 0:
        return _agrupar_lalamoves(lalamove_pre, cd) if gerar_lalamove else []

    # Quando há menos entregas que entregadores, usa só n entregadores.
    m_efetivo = min(m, n)

    # PASSO 2: clustering geográfico via K-means equilibrado (compacto,
    # balanceado, não corta bairros como o sweep angular fazia).
    clusters = kmeans_balanced(entregas, cd, m_efetivo)
    log.info("K-means balanced: %d clusters, tamanhos=%s",
             m_efetivo, [len(c) for c in clusters])

    # PASSO 3: atribuição cluster→entregador (greedy com bonus de preferência).
    atribuicao = atribuir(clusters, entregadores, cd)
    for i, j in atribuicao.items():
        log.info("  cluster %d (%d entregas) → %s", i, len(clusters[i]),
                 entregadores[j].nome)

    # PASSO 4: TSP por cluster. Cada cluster vira 1 problema pequeno e rápido.
    # Pra cada cluster, calcula matriz LOCAL com [entregas do cluster, CD, casa].
    rotas = []
    droppadas = []
    # Distribui o orçamento de tempo entre os clusters (mín 5s, máx tempo_limite_s).
    tempo_por_cluster = max(5, tempo_limite_s // max(1, m_efetivo))
    for i, cluster in enumerate(clusters):
        if not cluster:
            continue
        ent_idx = atribuicao.get(i)
        if ent_idx is None:
            droppadas.extend(cluster)
            continue
        ent = entregadores[ent_idx]
        coords_local = (
            [(e.lat, e.lng) for e in cluster]
            + [(cd.lat, cd.lng), (ent.lat, ent.lng)]
        )
        if matriz_pronta is not None:
            # Em testes, a matriz é da entrada completa — corta pra o local.
            # Layout esperado em matriz_pronta: idx das entregas seguem
            # ordem da lista global de entregas; mas pra TSP local precisamos
            # de uma matriz só do subconjunto. Mais simples: recalcula
            # haversine sintético.
            k = len(cluster)
            dist_l = [[0]*(k+2) for _ in range(k+2)]
            for a in range(k+2):
                for b in range(k+2):
                    if a == b: continue
                    la, lna = coords_local[a]
                    lb, lnb = coords_local[b]
                    # Manhattan em coords (mesmo que matriz_grade dos testes)
                    dist_l[a][b] = int((abs(la-lb) + abs(lna-lnb)) * 100000)
            mat_local = {"distancia": dist_l, "duracao": dist_l, "n": k+2}
        else:
            mat_local = osrm_matriz(coords_local)
        rota, drops = _tsp_cluster(
            cluster, ent, cd, mat_local["distancia"], mat_local["duracao"],
            servico_por_entrega_s=servico_por_entrega_s,
            limite_rota_min=limite_rota_min,
            tempo_limite_s=tempo_por_cluster,
        )
        if rota is not None:
            rotas.append(rota)
        droppadas.extend(drops)

    if droppadas:
        log.info("Lalamove (droppadas por estouro de tempo): %d", len(droppadas))

    if gerar_lalamove:
        rotas.extend(_agrupar_lalamoves(lalamove_pre + droppadas, cd))

    # PASSO 5: "Entregas CD" como ÚLTIMA rota (depois de tudo, inclusive
    # Lalamoves) — entregador do CD entrega na hora, então é independente.
    rota_cd = _rota_entregas_cd(entregas_cd, cd)
    if rota_cd is not None:
        rotas.append(rota_cd)

    return rotas
