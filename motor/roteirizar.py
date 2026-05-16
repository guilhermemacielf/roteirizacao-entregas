"""
Motor de roteirização — CVRP (Capacitated Vehicle Routing Problem) com OR-Tools.

Resolve, de uma vez só:
  1. Agrupar ~120 entregas em rotas
  2. Atribuir cada rota a um entregador
  3. Ordenar as entregas dentro da rota (TSP)

Restrições modeladas:
  - Cada rota sai do CD e TERMINA na casa do entregador (open VRP, end node
    distinto por veículo).
  - Capacidade: cada rota vai até `max_paradas` entregas (default 18).
    `min_paradas` é hint pra distribuição equilibrada (via
    GlobalSpanCostCoefficient), não restrição dura.
  - Forçar todos entregadores selecionados a saírem: tenta primeiro com
    restrição dura `>= 1 parada por veículo`; se infactível, refaz sem.
  - Tempo: todos saem do CD às 9h. Serviço de 10 min em cada entrega. Toda
    entrega concluída até `limite_rota_min` (default 300 min = 14h).
  - Janelas de horário opcionais por entrega.
  - Disjunction: cada entrega pode ser "droppada" pagando penalidade alta —
    usado quando o tempo não cabe pra todo mundo. Droppadas viram Lalamove.

Lalamove (entregador virtual):
  - Sobras de capacidade (n > m × max_paradas): as N mais próximas do CD
    saem do CVRP ANTES como Lalamove.
  - Droppadas pelo solver (estouro de tempo) entram pra mesma fila.
  - Tudo agrupado em rotas de até `MAX_PARADAS_LALAMOVE` (6) por proximidade.
  - Cada cluster vira Rota com `Entregador(id="LALA1", nome="Lalamove 1",
    lat=cd.lat, lng=cd.lng)` + `candidata_lalamove=True`.

Objetivo: minimizar a QUILOMETRAGEM total (metros, via matriz OSRM).
"""

import logging
import math
import unicodedata
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from motor.modelos import Entrega, Entregador, CD, Parada, Rota
from motor.matriz import matriz as osrm_matriz

log = logging.getLogger(__name__)

# Velocidade média estimada em meio urbano de BH — usada só pra estimar
# duração de rotas Lalamove (que NÃO passam pela matriz OSRM).
KMH_URBANO = 30.0

# Cada rota Lalamove agrupa até 6 entregas.
MAX_PARADAS_LALAMOVE = 6

# Bônus (em metros) descontado do custo do arco quando a entrega cai num
# bairro de preferência do entregador. 3000m = secundária mas com peso real,
# inverte alocação quando 2 entregadores estão similar; balanço (10M) ainda
# domina nos casos de conflito.
BONUS_PREFERENCIA_M = 3000


def _normalizar_bairro(s: str) -> str:
    """Lower + sem acento + espaços colapsados — pra comparar bairros sem
    se preocupar com 'Pampulha' vs 'pampulha' vs 'Pampulha '."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


def _haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    R = 6371.0
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat))
         * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _separar_sobras_capacidade(
    entregas: list[Entrega], cd: CD, capacidade: int
) -> tuple[list[Entrega], list[Entrega]]:
    """Se n > capacidade, separa as N mais próximas do CD pra Lalamove."""
    n_sobras = max(0, len(entregas) - capacidade)
    if n_sobras == 0 or capacidade <= 0:
        return entregas, []
    indices_ord = sorted(
        range(len(entregas)),
        key=lambda i: _haversine_km(cd.lat, cd.lng,
                                    entregas[i].lat, entregas[i].lng)
    )
    lalamove_idx = set(indices_ord[:n_sobras])
    resto    = [e for i, e in enumerate(entregas) if i not in lalamove_idx]
    lalamove = [e for i, e in enumerate(entregas) if i in lalamove_idx]
    return resto, lalamove


def _agrupar_lalamoves(entregas: list[Entrega], cd: CD,
                       max_por_rota: int = MAX_PARADAS_LALAMOVE) -> list[Rota]:
    """Agrupa Lalamove em rotas de até `max_por_rota` por proximidade
    (greedy: semente mais próxima do CD, vizinhos mais próximos da semente)."""
    if not entregas:
        return []

    restantes = list(entregas)
    restantes.sort(key=lambda e: _haversine_km(cd.lat, cd.lng, e.lat, e.lng))

    rotas: list[Rota] = []
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

        paradas: list[Parada] = []
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


def _construir_e_resolver(
    *, entregas, entregadores, cd, distancia, duracao, n, m, IDX_CD,
    idx_casa, n_nos, max_paradas, servico_por_entrega_s, limite_rota_min,
    gerar_lalamove, tempo_limite_s, forcar_todos_saem,
):
    """Constrói o modelo OR-Tools e resolve. Retorna (sol, manager, routing,
    tempo_dim) ou (None, None, None, None) se infeasible.

    `forcar_todos_saem`: se True, adiciona restrição dura "≥1 parada por
    veículo". É a primeira tentativa; se falhar, o caller chama de novo com
    False (fallback).
    """
    manager = pywrapcp.RoutingIndexManager(n_nos, m, [IDX_CD] * m,
                                           [idx_casa[v] for v in range(m)])
    routing = pywrapcp.RoutingModel(manager)

    # Preferências de bairro por veículo (normalizadas). Se algum entregador
    # tem preferências, usa custo POR VEÍCULO (descontando BONUS_PREFERENCIA_M
    # quando o bairro da entrega bate). Senão, custo único pra todos (mais
    # leve — registra só 1 callback).
    prefs_por_v = [
        {_normalizar_bairro(p) for p in ent.preferencias if p}
        for ent in entregadores
    ]
    tem_preferencias = any(prefs_por_v)

    if tem_preferencias:
        # Um callback por veículo, com lookup nas preferências dele.
        for v in range(m):
            prefs_v = prefs_por_v[v]
            def _cb_factory(prefs=prefs_v):
                def cb(i, j):
                    no_i = manager.IndexToNode(i)
                    no_j = manager.IndexToNode(j)
                    custo = distancia[no_i][no_j]
                    if no_j < n and prefs:
                        bairro = _normalizar_bairro(entregas[no_j].bairro)
                        if bairro and bairro in prefs:
                            custo = max(0, custo - BONUS_PREFERENCIA_M)
                    return custo
                return cb
            cb_idx_v = routing.RegisterTransitCallback(_cb_factory())
            routing.SetArcCostEvaluatorOfVehicle(cb_idx_v, v)
    else:
        def cb_distancia(i, j):
            return distancia[manager.IndexToNode(i)][manager.IndexToNode(j)]
        routing.SetArcCostEvaluatorOfAllVehicles(
            routing.RegisterTransitCallback(cb_distancia))

    def cb_tempo(i, j):
        no_i = manager.IndexToNode(i)
        no_j = manager.IndexToNode(j)
        serv = servico_por_entrega_s if no_i < n else 0
        return duracao[no_i][no_j] + serv
    cb_tempo_idx = routing.RegisterTransitCallback(cb_tempo)

    def cb_uma_parada(i):
        return 1 if manager.IndexToNode(i) < n else 0
    cnt_idx = routing.RegisterUnaryTransitCallback(cb_uma_parada)
    routing.AddDimensionWithVehicleCapacity(
        cnt_idx, 0, [max_paradas] * m, True, "Contagem",
    )
    cnt_dim = routing.GetDimensionOrDie("Contagem")
    # PRIORIDADE 1: balanceamento de paradas é DOMINANTE. 100M por unidade
    # de span (max - min de paradas entre veículos) força ~13/13/13 a 14
    # mesmo que entregadores morem longe (caso Tamara/Camila em Betim).
    cnt_dim.SetGlobalSpanCostCoefficient(100_000_000)

    if forcar_todos_saem:
        # TUDO SOFT pra evitar fallback (que destruía o balanço quando uma
        # janela de horário apertada deixava a restrição hard infeasible).
        # Hierarquia de penalidades:
        # - Veículo vazio: 10B → nunca acontece a menos que IMPOSSÍVEL
        # - Rota abaixo da média: 500M por parada faltando → forte incentivo
        # - Upper hard: média_alta+2 → limite superior pra span máx 3
        media_baixa = n // m
        media_alta = -(-n // m)
        for v in range(m):
            cv = cnt_dim.CumulVar(routing.End(v))
            cnt_dim.SetCumulVarSoftLowerBound(routing.End(v), 1, 10_000_000_000)
            cnt_dim.SetCumulVarSoftLowerBound(routing.End(v),
                                              media_baixa,
                                              500_000_000)
            cv.SetMax(min(max_paradas, media_alta + 2))

    routing.SetFixedCostOfAllVehicles(0)

    HORIZONTE = 24 * 3600
    routing.AddDimension(cb_tempo_idx, HORIZONTE, HORIZONTE, True, "Tempo")
    tempo_dim = routing.GetDimensionOrDie("Tempo")

    # PRIORIDADE 2: coerência geográfica. Rota "extremo-a-extremo" tem
    # km muito maior que rota concentrada. Dimensão Distância acumula
    # metros por veículo; SpanCost penaliza desequilíbrio de km (rotas com
    # km muito diferentes). Indiretamente força entregas próximas em mesma
    # rota — solver prefere agrupar geograficamente.
    def cb_distancia_pura(i, j):
        return distancia[manager.IndexToNode(i)][manager.IndexToNode(j)]
    dist_cb_idx = routing.RegisterTransitCallback(cb_distancia_pura)
    routing.AddDimension(dist_cb_idx, 0, 10**9, True, "Distancia")
    dist_dim = routing.GetDimensionOrDie("Distancia")
    # Coef 100 = cada 1km de desbalanço de rota custa 100. Calibrado pra ser
    # ~10% do peso do balanço de paradas (que é o critério principal).
    dist_dim.SetGlobalSpanCostCoefficient(100)

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

    # Disjunction: solver pode dropar entregas pagando penalidade. Existe
    # SÓ pra cobrir entregas com janela de horário apertada que realmente
    # não cabem em rota nenhuma (sem Disjunction, solver ficaria infeasible).
    # Penalidade ASTRONÔMICA (10^15): nunca vale a pena dropar a menos que
    # a entrega seja literalmente impossível de atender. Evita drops
    # oportunísticos.
    if gerar_lalamove and limite_rota_min is not None and not forcar_todos_saem:
        for i in range(n):
            routing.AddDisjunction([manager.NodeToIndex(i)], 10**15)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
    )
    # GUIDED_LOCAL_SEARCH é mais conservador (não escapa tanto de ótimos locais
    # quanto SIMULATED_ANNEALING, mas é mais robusto pra encontrar solução
    # factível inicial com restrições estritas de balanceamento).
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(tempo_limite_s)

    sol = routing.SolveWithParameters(params)
    if sol is None:
        return None, None, None, None
    return sol, manager, routing, tempo_dim


def roteirizar(
    entregas: list[Entrega],
    entregadores: list[Entregador],
    cd: CD,
    *,
    min_paradas: int = 10,
    max_paradas: int = 18,
    matriz_pronta: dict | None = None,
    tempo_limite_s: int = 30,
    servico_por_entrega_s: int = 600,
    limite_rota_min: int | None = 300,
    gerar_lalamove: bool = True,
) -> list[Rota]:
    """Resolve a roteirização. Retorna lista de Rota (entregadores + Lalamoves).

    `min_paradas`: hint pra balanceamento; o solver tende à média n/m.
    `max_paradas`: limite duro por rota.
    `limite_rota_min`: minutos máx por entrega (None = sem limite). Default 300.
    `gerar_lalamove`: se True, excesso vira Lalamoves agrupadas (≤6 cada).
    """
    n_total = len(entregas)
    m = len(entregadores)
    if n_total == 0:
        return []
    if m == 0:
        raise ValueError("nenhum entregador disponível")

    # ── 0. Separação Lalamove inicial (sobras por capacidade) ─────────
    capacidade = m * max_paradas
    if gerar_lalamove:
        entregas, lalamove_pre = _separar_sobras_capacidade(entregas, cd, capacidade)
        if lalamove_pre:
            log.info("Lalamove (sobras de capacidade): %d/%d entregas — capacidade %d",
                     len(lalamove_pre), n_total, capacidade)
    else:
        lalamove_pre = []

    n = len(entregas)
    if n == 0:
        return _agrupar_lalamoves(lalamove_pre, cd) if gerar_lalamove else []

    # ── 1. Layout dos nós + matriz ────────────────────────────────────
    IDX_CD = n
    idx_casa = {v: n + 1 + v for v in range(m)}
    coords = (
        [(e.lat, e.lng) for e in entregas]
        + [(cd.lat, cd.lng)]
        + [(ent.lat, ent.lng) for ent in entregadores]
    )
    mat = matriz_pronta if matriz_pronta is not None else osrm_matriz(coords)
    distancia, duracao = mat["distancia"], mat["duracao"]
    n_nos = len(coords)

    # ── 2. Resolver com fallback ──────────────────────────────────────
    # Tenta primeiro forçando todos saírem (≥1 parada por veículo). Se
    # infactível (cenário: 13 veículos × 36 entregas com tempo apertado +
    # disjunction confunde o solver), refaz sem a restrição: solver pode
    # deixar alguns veículos vazios, mas pelo menos retorna algo.
    forcar = n >= m
    sol, manager, routing, tempo_dim = _construir_e_resolver(
        entregas=entregas, entregadores=entregadores, cd=cd,
        distancia=distancia, duracao=duracao, n=n, m=m,
        IDX_CD=IDX_CD, idx_casa=idx_casa, n_nos=n_nos,
        max_paradas=max_paradas, servico_por_entrega_s=servico_por_entrega_s,
        limite_rota_min=limite_rota_min, gerar_lalamove=gerar_lalamove,
        tempo_limite_s=tempo_limite_s, forcar_todos_saem=forcar,
    )
    if sol is None and forcar:
        log.warning("Solver infactível com restrição ≥1 por veículo — refazendo sem ela")
        sol, manager, routing, tempo_dim = _construir_e_resolver(
            entregas=entregas, entregadores=entregadores, cd=cd,
            distancia=distancia, duracao=duracao, n=n, m=m,
            IDX_CD=IDX_CD, idx_casa=idx_casa, n_nos=n_nos,
            max_paradas=max_paradas, servico_por_entrega_s=servico_por_entrega_s,
            limite_rota_min=limite_rota_min, gerar_lalamove=gerar_lalamove,
            tempo_limite_s=tempo_limite_s, forcar_todos_saem=False,
        )
    if sol is None:
        raise RuntimeError(
            "OR-Tools não encontrou solução. Causas possíveis: matriz inválida "
            "(verifique o OSRM), ou parâmetros inconsistentes."
        )

    # ── 3. Extrair rotas dos entregadores ─────────────────────────────
    rotas: list[Rota] = []
    entregues_idx: set[int] = set()
    for v in range(m):
        idx = routing.Start(v)
        if routing.IsEnd(sol.Value(routing.NextVar(idx))):
            continue

        paradas: list[Parada] = []
        dist_total = 0
        ordem = 0
        while not routing.IsEnd(idx):
            prox = sol.Value(routing.NextVar(idx))
            no_atual = manager.IndexToNode(idx)
            no_prox = manager.IndexToNode(prox)
            dist_total += distancia[no_atual][no_prox]
            if no_prox < n:
                ordem += 1
                chegada = sol.Value(tempo_dim.CumulVar(prox))
                paradas.append(Parada(
                    entrega=entregas[no_prox],
                    ordem=ordem,
                    chegada_estimada_s=chegada,
                ))
                entregues_idx.add(no_prox)
            idx = prox

        dur_total = sol.Value(tempo_dim.CumulVar(routing.End(v)))
        rotas.append(Rota(
            entregador=entregadores[v],
            paradas=paradas,
            distancia_m=dist_total,
            duracao_s=dur_total,
        ))

    # ── 4. Lalamove: sobras-capacidade + droppadas, agrupadas ─────────
    droppadas = [entregas[i] for i in range(n) if i not in entregues_idx]
    if droppadas:
        log.info("Lalamove (droppadas pelo solver por estouro de tempo): %d",
                 len(droppadas))

    if gerar_lalamove:
        rotas.extend(_agrupar_lalamoves(lalamove_pre + droppadas, cd))

    return rotas
