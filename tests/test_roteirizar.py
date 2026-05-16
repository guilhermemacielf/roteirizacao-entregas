"""
Testes do motor — usam matriz sintética (não chamam OSRM).

A matriz é passada via `matriz_pronta`, então o teste é determinístico,
rápido e não depende de rede.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from motor.modelos import Entrega, Entregador, CD
from motor.roteirizar import roteirizar


def _matriz_grade(coords):
    """Matriz sintética: distância = (|dlat| + |dlng|) escalado.
    Suficiente pra validar a lógica de agrupamento/atribuição."""
    n = len(coords)
    dist = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            d = abs(coords[i][0] - coords[j][0]) + abs(coords[i][1] - coords[j][1])
            dist[i][j] = int(d * 100000)
    return {"distancia": dist, "duracao": dist, "n": n}


def test_capacidade_respeitada():
    """Nenhuma rota pode passar de max_paradas."""
    entregas = [Entrega(f"E{i}", -19.9 - i*0.01, -43.9) for i in range(30)]
    entregadores = [
        Entregador(f"D{v}", f"Driver{v}", -19.9 - v*0.05, -43.95)
        for v in range(4)
    ]
    cd = CD(-19.92, -43.94)
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=5, max_paradas=10,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=5,
                       limite_rota_min=None)  # matriz sintética não respeita tempo real
    assert rotas, "deveria gerar rotas"
    for r in rotas:
        assert r.n_paradas <= 10, f"rota com {r.n_paradas} paradas excede o máximo"
        assert r.n_paradas >= 5, f"rota com {r.n_paradas} paradas abaixo do mínimo"


def test_todas_entregas_atendidas():
    """A soma das paradas tem que bater com o total de entregas."""
    entregas = [Entrega(f"E{i}", -19.9 - i*0.01, -43.9 - i*0.005) for i in range(24)]
    entregadores = [
        Entregador(f"D{v}", f"Driver{v}", -19.95, -43.95) for v in range(3)
    ]
    cd = CD(-19.92, -43.94)
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=6, max_paradas=10,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=5,
                       limite_rota_min=None)
    total = sum(r.n_paradas for r in rotas)
    assert total == 24, f"esperava 24 entregas roteirizadas, veio {total}"
    ids = {p.entrega.id for r in rotas for p in r.paradas}
    assert len(ids) == 24, "entregas duplicadas ou faltando"


def test_janela_horario():
    """Entrega com janela de horário deve respeitar o limite."""
    entregas = [Entrega(f"E{i}", -19.9 - i*0.01, -43.9) for i in range(12)]
    # E5 só pode ser entregue entre 180 e 300 min
    entregas[5].janela_inicio = 180
    entregas[5].janela_fim = 300
    entregadores = [Entregador("D0", "Driver0", -19.95, -43.95)]
    cd = CD(-19.92, -43.94)
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=10, max_paradas=12,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=5,
                       limite_rota_min=None)
    for r in rotas:
        for p in r.paradas:
            if p.entrega.id == "E5":
                assert 180*60 <= p.chegada_estimada_s <= 300*60, \
                    f"E5 chegou fora da janela: {p.chegada_estimada_s}s"


def test_lalamove_sobras_viram_candidatas():
    """Quando há mais entregas do que cabe nos entregadores (n > m * max),
    as sobras viram rotas Lalamove (candidata_lalamove=True, 1 entrega
    cada, sem entregador). As Lalamove são as MAIS PRÓXIMAS do CD."""
    # 15 próximas (P*) + 10 longes (L*) = 25 entregas; capacidade = 10;
    # sobram 15 entregas pra Lalamove (todas as próximas).
    cd = CD(-19.92, -43.94)
    proximas = [Entrega(f"P{i}", -19.93, -43.94 + i * 0.001) for i in range(15)]
    longes   = [Entrega(f"L{i}", -19.98 - i * 0.001, -43.94) for i in range(10)]
    entregas = proximas + longes
    entregadores = [Entregador("D0", "Driver0", -19.96, -43.94)]
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=5, max_paradas=10,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=5,
                       limite_rota_min=None)
    lalamove = [r for r in rotas if r.candidata_lalamove]
    normais  = [r for r in rotas if not r.candidata_lalamove]
    assert len(lalamove) == 15, f"esperava 15 Lalamove, vieram {len(lalamove)}"
    # Cada Lalamove tem exatamente 1 parada
    assert all(r.n_paradas == 1 for r in lalamove)
    # Lalamove pegou só as próximas — todas com prefixo P
    ids_lalamove = {r.paradas[0].entrega.id for r in lalamove}
    assert all(i.startswith("P") for i in ids_lalamove), \
        f"Lalamove deveria pegar só as 'próximas' (P*), pegou {ids_lalamove}"
    # Entregador ficou com as longes (10 das 10)
    assert sum(r.n_paradas for r in normais) == 10
    # Rotas Lalamove não têm entregador
    assert all(r.entregador is None for r in lalamove)


def test_sem_lalamove_quando_cabe():
    """Quando entregas <= capacidade dos entregadores, NENHUMA rota
    Lalamove é gerada — todas vão pros entregadores normais."""
    entregas = [Entrega(f"E{i}", -19.9 - i*0.01, -43.9) for i in range(15)]
    entregadores = [Entregador("D0", "Driver0", -19.95, -43.95)]
    cd = CD(-19.92, -43.94)
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    # 1 entregador × max 18 = 18 capacidade; 15 entregas cabem
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=10, max_paradas=18,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=5,
                       limite_rota_min=None)
    assert not any(r.candidata_lalamove for r in rotas), \
        "não deveria gerar Lalamove quando entregas cabem nos entregadores"


if __name__ == "__main__":
    test_capacidade_respeitada()
    print("✓ test_capacidade_respeitada")
    test_todas_entregas_atendidas()
    print("✓ test_todas_entregas_atendidas")
    test_janela_horario()
    print("✓ test_janela_horario")
    test_lalamove_sobras_viram_candidatas()
    print("✓ test_lalamove_sobras_viram_candidatas")
    test_sem_lalamove_quando_cabe()
    print("✓ test_sem_lalamove_quando_cabe")
    print("\nTodos os testes passaram.")
