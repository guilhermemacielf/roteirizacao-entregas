"""
Motor de roteirização — CVRP (Capacitated Vehicle Routing Problem) com OR-Tools.

Resolve, de uma vez só:
  1. Agrupar ~120 entregas em rotas
  2. Atribuir cada rota a um entregador
  3. Ordenar as entregas dentro da rota (TSP)

Restrições modeladas:
  - Cada rota sai do CD e TERMINA na casa do entregador (open VRP, end node
    distinto por veículo). Como a perna final até a casa entra na conta,
    as entregas naturalmente "fluem" na direção da casa de cada entregador.
  - Capacidade: cada rota usada tem entre `min_paradas` e `max_paradas`
    entregas (default 10-18). Veículo não usado tem 0 paradas.
  - Tempo: todos saem do CD às 9h. Cada entrega gasta `servico_por_entrega_s`
    (10 min) além do deslocamento. Toda entrega tem que estar concluída até
    o `limite_rota_min` (default 240 min = 13h).
  - Janelas de horário opcionais por entrega (início e/ou fim).

Objetivo: minimizar a QUILOMETRAGEM total (metros, via matriz OSRM).

Entrada: lista de Entrega, lista de Entregador, CD.
Saída:   lista de Rota (ordenadas, com distância/duração).
"""

import logging
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from motor.modelos import Entrega, Entregador, CD, Parada, Rota
from motor.matriz import matriz as osrm_matriz

log = logging.getLogger(__name__)


def roteirizar(
    entregas: list[Entrega],
    entregadores: list[Entregador],
    cd: CD,
    *,
    min_paradas: int = 10,
    max_paradas: int = 18,
    matriz_pronta: dict | None = None,
    tempo_limite_s: int = 30,
    servico_por_entrega_s: int = 600,    # 10 min parado em cada entrega
    limite_rota_min: int | None = 240,   # entregas concluídas até 13h (240min após 9h)
) -> list[Rota]:
    """
    Resolve a roteirização. Retorna uma lista de Rota (uma por entregador
    que saiu; entregadores sem entregas ficam de fora do resultado).

    `matriz_pronta`: se fornecida, usa essa matriz em vez de chamar o OSRM
    (útil pra testes e pra desacoplar a fonte da matriz). Formato igual ao
    retorno de motor.matriz.matriz().

    `servico_por_entrega_s`: tempo parado em cada entrega (entra na dimensão
    de tempo, não na distância).
    `limite_rota_min`: minutos desde a saída do CD em que toda entrega tem
    que estar concluída (None = sem limite).
    """
    n = len(entregas)
    m = len(entregadores)
    if n == 0:
        return []
    if m == 0:
        raise ValueError("nenhum entregador disponível")

    # ── 1. Layout dos nós ────────────────────────────────────
    # [0 .. n-1]      = entregas
    # [n]             = CD
    # [n+1 .. n+m]    = casas dos entregadores
    IDX_CD = n
    idx_casa = {v: n + 1 + v for v in range(m)}

    coords = (
        [(e.lat, e.lng) for e in entregas]
        + [(cd.lat, cd.lng)]
        + [(ent.lat, ent.lng) for ent in entregadores]
    )

    if matriz_pronta is not None:
        mat = matriz_pronta
    else:
        mat = osrm_matriz(coords)
    duracao = mat["duracao"]
    distancia = mat["distancia"]
    n_nos = len(coords)

    # ── 2. Quantos veículos realmente forçar a sair ──────────
    # Se há entregas demais pra todos respeitarem o mínimo, ou de menos,
    # ajusta. Veículos "sobrando" ficam disponíveis (rota vazia) — e a
    # camada Lalamove decide o que fazer com eles depois.
    veiculos_necessarios_min = -(-n // max_paradas)   # ceil(n / max)
    veiculos_necessarios_max = n // min_paradas       # floor(n / min)
    if veiculos_necessarios_max < veiculos_necessarios_min:
        # Não dá pra respeitar [min,max] com nenhum nº de veículos —
        # afrouxa o mínimo (raro; só com poucos pontos).
        min_paradas = max(1, n // m)
        log.warning("Afrouxando min_paradas para %d (n=%d, m=%d)", min_paradas, n, m)

    # ── 3. Modelo OR-Tools ───────────────────────────────────
    starts = [IDX_CD] * m
    ends = [idx_casa[v] for v in range(m)]
    manager = pywrapcp.RoutingIndexManager(n_nos, m, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    # Custo do arco = DISTÂNCIA (metros). O objetivo é minimizar a
    # quilometragem total — não o tempo.
    def cb_distancia(i, j):
        return distancia[manager.IndexToNode(i)][manager.IndexToNode(j)]

    cb_dist_idx = routing.RegisterTransitCallback(cb_distancia)
    routing.SetArcCostEvaluatorOfAllVehicles(cb_dist_idx)

    # Callback de TEMPO = deslocamento + serviço do nó de origem. Pôr o
    # serviço no arco de SAÍDA faz o cumul de tempo numa entrega bater com
    # a hora de CHEGADA nela (o serviço dela é "gasto" ao sair).
    def cb_tempo(i, j):
        no_i = manager.IndexToNode(i)
        no_j = manager.IndexToNode(j)
        serv = servico_por_entrega_s if no_i < n else 0   # CD/casa não têm serviço
        return duracao[no_i][no_j] + serv

    cb_tempo_idx = routing.RegisterTransitCallback(cb_tempo)

    # ── Dimensão de contagem (nº de paradas por rota) ────────
    def cb_uma_parada(i):
        no = manager.IndexToNode(i)
        return 1 if no < n else 0   # só entregas contam

    cnt_idx = routing.RegisterUnaryTransitCallback(cb_uma_parada)
    routing.AddDimensionWithVehicleCapacity(
        cnt_idx,
        0,                       # sem folga
        [max_paradas] * m,       # capacidade máxima por veículo
        True,                    # começa em zero
        "Contagem",
    )
    cnt_dim = routing.GetDimensionOrDie("Contagem")

    # Cada rota: ou está vazia (0 paradas) ou tem pelo menos min_paradas.
    # solver.Max de duas condições booleanas == 1  ⇒  pelo menos uma é verdade.
    solver = routing.solver()
    for v in range(m):
        cnt_fim = cnt_dim.CumulVar(routing.End(v))
        solver.Add(solver.Max(cnt_fim == 0, cnt_fim >= min_paradas) == 1)

    # ── Dimensão de tempo (deslocamento + serviço) ───────────
    # Horizonte generoso: 24h em segundos. Slack alto = pode esperar
    # (necessário quando uma entrega tem janela de início).
    HORIZONTE = 24 * 3600
    routing.AddDimension(cb_tempo_idx, HORIZONTE, HORIZONTE, True, "Tempo")
    tempo_dim = routing.GetDimensionOrDie("Tempo")

    # Limite da rota: toda ENTREGA tem que estar concluída até `limite_rota_min`.
    # cumul numa entrega = hora de chegada; concluída = chegada + serviço.
    # Então chegada ≤ limite − serviço. A perna de volta pra casa do
    # entregador (commute) fica de fora desse limite, de propósito.
    cap_chegada = None
    if limite_rota_min is not None:
        cap_chegada = max(0, limite_rota_min * 60 - servico_por_entrega_s)

    for i, e in enumerate(entregas):
        cv = tempo_dim.CumulVar(manager.NodeToIndex(i))
        if e.janela_inicio is not None:
            cv.SetMin(e.janela_inicio * 60)
        if e.janela_fim is not None:
            cv.SetMax(e.janela_fim * 60)
        if cap_chegada is not None:
            cv.SetMax(cap_chegada)   # intersecta com a janela, se houver

    # Custo fixo por veículo usado (em metros, mesma unidade do arco):
    # empurra o solver a encher rotas em vez de espalhar fino. Como o
    # mínimo de paradas já evita rotas minúsculas, isso é mais um
    # desempate — vale ~3,6 km por veículo a mais.
    routing.SetFixedCostOfAllVehicles(3600)

    # ── 4. Resolver ──────────────────────────────────────────
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(tempo_limite_s)

    sol = routing.SolveWithParameters(params)
    if sol is None:
        raise RuntimeError(
            "OR-Tools não encontrou solução possível. Causas comuns: "
            "entregadores de menos pro volume; min/max de paradas apertado "
            "demais; ou o limite de horário (13h) + os 10 min por entrega "
            "não cabem. Tente liberar mais entregadores ou afrouxar os limites."
        )

    # ── 5. Extrair rotas ─────────────────────────────────────
    rotas: list[Rota] = []
    for v in range(m):
        idx = routing.Start(v)
        if routing.IsEnd(sol.Value(routing.NextVar(idx))):
            continue   # veículo não saiu

        paradas: list[Parada] = []
        dist_total = 0
        ordem = 0
        anterior = manager.IndexToNode(idx)
        while not routing.IsEnd(idx):
            prox = sol.Value(routing.NextVar(idx))
            no_atual = manager.IndexToNode(idx)
            no_prox = manager.IndexToNode(prox)
            dist_total += distancia[no_atual][no_prox]
            if no_prox < n:   # é uma entrega
                ordem += 1
                chegada = sol.Value(tempo_dim.CumulVar(prox))
                paradas.append(Parada(
                    entrega=entregas[no_prox],
                    ordem=ordem,
                    chegada_estimada_s=chegada,
                ))
            idx = prox

        dur_total = sol.Value(tempo_dim.CumulVar(routing.End(v)))
        rotas.append(Rota(
            entregador=entregadores[v],
            paradas=paradas,
            distancia_m=dist_total,
            duracao_s=dur_total,
        ))

    return rotas
