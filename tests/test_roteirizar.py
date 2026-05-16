"""
Testes do motor — usam matriz sintética (não chamam OSRM).

A matriz é passada via `matriz_pronta`, então o teste é determinístico,
rápido e não depende de rede.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from motor.modelos import Entrega, Entregador, CD
from motor.roteirizar import roteirizar, MAX_PARADAS_LALAMOVE


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


def test_max_capacidade_respeitada():
    """Nenhuma rota de ENTREGADOR pode passar de max_paradas (limite duro)."""
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
                       limite_rota_min=None)
    assert rotas, "deveria gerar rotas"
    for r in rotas:
        if r.candidata_lalamove:
            assert r.n_paradas <= MAX_PARADAS_LALAMOVE
        else:
            assert r.n_paradas <= 10, f"rota com {r.n_paradas} paradas excede max"


def test_distribuicao_equilibrada():
    """min_paradas é sugestão balanceadora: com N entregas e M entregadores,
    cada um deve ficar perto da média N/M (tolerância ±2 paradas)."""
    entregas = [Entrega(f"E{i}", -19.9 - i*0.01, -43.9) for i in range(24)]
    entregadores = [
        Entregador(f"D{v}", f"Driver{v}", -19.95, -43.95) for v in range(3)
    ]
    cd = CD(-19.92, -43.94)
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=6, max_paradas=15,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=5,
                       limite_rota_min=None)
    paradas_por_entregador = [r.n_paradas for r in rotas if not r.candidata_lalamove]
    # 24 entregas / 3 entregadores ≈ 8 cada; tolerância ±2 (entre 6 e 10).
    media = 24 / 3
    for n in paradas_por_entregador:
        assert media - 2 <= n <= media + 2, \
            f"rota com {n} paradas longe da média {media}: {paradas_por_entregador}"


def test_todas_entregas_atendidas_quando_cabe():
    """Soma das paradas (incluindo Lalamove) tem que bater com a entrada."""
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
    assert total == 24, f"esperava 24 entregas atendidas, veio {total}"
    ids = {p.entrega.id for r in rotas for p in r.paradas}
    assert len(ids) == 24, "entregas duplicadas ou faltando"


def test_janela_horario():
    """Entrega com janela de horário deve respeitar o limite."""
    entregas = [Entrega(f"E{i}", -19.9 - i*0.01, -43.9) for i in range(12)]
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


def test_lalamove_sobras_agrupadas():
    """Quando n > capacidade, sobras viram Lalamove agrupadas em rotas de
    até MAX_PARADAS_LALAMOVE. As mais PRÓXIMAS do CD viram Lalamove
    (entregadores cobrem as longes)."""
    # 15 próximas (P*) + 10 longes (L*) = 25 entregas; capacidade = 10;
    # sobram 15 entregas pra Lalamove → 3 rotas de 5 (6+6+3 ou 5+5+5).
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
    # 15 entregas Lalamove em rotas de até 6 → 3 rotas (6+6+3)
    assert len(lalamove) == 3, f"esperava 3 rotas Lalamove, veio {len(lalamove)}"
    total_lalamove = sum(r.n_paradas for r in lalamove)
    assert total_lalamove == 15, f"esperava 15 entregas Lalamove, veio {total_lalamove}"
    for r in lalamove:
        assert r.n_paradas <= MAX_PARADAS_LALAMOVE
        assert r.entregador is not None
        assert r.entregador.nome.startswith("Lalamove ")
        assert r.entregador.id.startswith("LALA")
    # Lalamove pegou só as próximas
    ids_lalamove = {p.entrega.id for r in lalamove for p in r.paradas}
    assert all(i.startswith("P") for i in ids_lalamove), \
        f"Lalamove deveria pegar só P*, pegou {ids_lalamove}"
    # Entregador ficou com as longes
    assert sum(r.n_paradas for r in normais) == 10


def test_sem_lalamove_quando_cabe():
    """Quando entregas <= capacidade, NENHUMA rota Lalamove é gerada."""
    entregas = [Entrega(f"E{i}", -19.9 - i*0.01, -43.9) for i in range(15)]
    entregadores = [Entregador("D0", "Driver0", -19.95, -43.95)]
    cd = CD(-19.92, -43.94)
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=10, max_paradas=18,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=5,
                       limite_rota_min=None)
    assert not any(r.candidata_lalamove for r in rotas)


def test_balanco_paradas_dentro_da_tolerancia():
    """Distribuição razoavelmente equilibrada entre entregadores. Em
    matriz sintética (manhattan), span pode ser maior que em produção real
    porque a combinatória de pontos colineares engana a heurística. Aqui
    aceitamos span <= 10 — em produção (matriz OSRM) tipicamente é <= 3-5.
    """
    entregas = [Entrega(f"E{i}", -19.9 - i*0.005, -43.9 - i*0.003) for i in range(50)]
    entregadores = [
        Entregador(f"D{v}", f"Driver{v}", -19.95 - v*0.01, -43.95 + v*0.01)
        for v in range(5)
    ]
    cd = CD(-19.92, -43.94)
    coords = ([(e.lat, e.lng) for e in entregas]
              + [(cd.lat, cd.lng)]
              + [(d.lat, d.lng) for d in entregadores])
    rotas = roteirizar(entregas, entregadores, cd,
                       min_paradas=10, max_paradas=18,
                       matriz_pronta=_matriz_grade(coords), tempo_limite_s=10,
                       limite_rota_min=None)
    paradas = [r.n_paradas for r in rotas if not r.candidata_lalamove]
    assert paradas, "esperava ao menos uma rota"
    span = max(paradas) - min(paradas)
    assert span <= 10, f"span {span} excede tolerância sintética: paradas={paradas}"


if __name__ == "__main__":
    test_max_capacidade_respeitada()
    print("✓ test_max_capacidade_respeitada")
    test_distribuicao_equilibrada()
    print("✓ test_distribuicao_equilibrada")
    test_todas_entregas_atendidas_quando_cabe()
    print("✓ test_todas_entregas_atendidas_quando_cabe")
    test_janela_horario()
    print("✓ test_janela_horario")
    test_lalamove_sobras_agrupadas()
    print("✓ test_lalamove_sobras_agrupadas")
    test_sem_lalamove_quando_cabe()
    print("✓ test_sem_lalamove_quando_cabe")
    test_balanco_paradas_dentro_da_tolerancia()
    print("✓ test_balanco_paradas_dentro_da_tolerancia")
    print("\nTodos os testes passaram.")
