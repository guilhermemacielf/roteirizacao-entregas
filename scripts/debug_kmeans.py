"""Roda kmeans_balanced com logs em cada passo do pos-processo."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.io import sheets_para_csv_motor, carregar_entregas_texto, carregar_config
from motor.clustering import (_seed_setorial, _custo_atribuicao,
                                _rebalancear_por_km,
                                _mover_paradas_isoladas,
                                _mover_paradas_via_vizinho_mais_proximo,
                                _balancear_por_tempo,
                                _reduzir_clusters_longos,
                                _reduzir_entrelacamento,
                                _swap_outliers_entre_clusters)

URL = sys.argv[1]
csv_t, ok, fal = sheets_para_csv_motor(URL)
entregas = carregar_entregas_texto(csv_t)
cd, todos = carregar_config("/app/dados/config.json")

m = 6
n = len(entregas)
print(f"n={n}, m={m}")

# Reproduz K-means manualmente
base = n // m
resto = n % m
tamanhos = [base + (1 if i < resto else 0) for i in range(m)]
print(f"tamanhos-alvo: {tamanhos}")

centroides = _seed_setorial(entregas, cd, m)
atribuicao_prev = None
for _iter in range(30):
    candidatos = sorted(
        ((_custo_atribuicao(e.lat, e.lng, c[0], c[1],
                              cd.lat, cd.lng, 0.1), i, j)
         for i, e in enumerate(entregas)
         for j, c in enumerate(centroides)),
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
            novos.append((sum(p.lat for p in pts)/len(pts),
                          sum(p.lng for p in pts)/len(pts)))
        else:
            novos.append(centroides[j])
    centroides = novos

clusters = [[entregas[i] for i in range(n) if atribuicao[i] == j] for j in range(m)]
print(f"apos K-means iterativo (sem pos-processos): {[len(c) for c in clusters]}  sum={sum(len(c) for c in clusters)}")

nmin, nmax = 10, 18
for passada in range(5):
    snapshot = [list(c) for c in clusters]
    clusters = _rebalancear_por_km(clusters, cd)
    print(f"  P{passada} apos _rebalancear_por_km:               {[len(c) for c in clusters]}")
    clusters = _mover_paradas_isoladas(clusters, cd, n_max_paradas=nmax)
    print(f"  P{passada} apos _mover_paradas_isoladas:           {[len(c) for c in clusters]}")
    clusters = _mover_paradas_via_vizinho_mais_proximo(clusters, cd, n_min_paradas=nmin, n_max_paradas=nmax)
    print(f"  P{passada} apos _mover_paradas_via_vizinho:        {[len(c) for c in clusters]}")
    clusters = _balancear_por_tempo(clusters, cd, n_min_paradas=nmin, n_max_paradas=nmax)
    print(f"  P{passada} apos _balancear_por_tempo:              {[len(c) for c in clusters]}")
    clusters = _reduzir_clusters_longos(clusters, cd, n_min_paradas=nmin, n_max_paradas=nmax)
    print(f"  P{passada} apos _reduzir_clusters_longos:          {[len(c) for c in clusters]}")
    clusters = _reduzir_entrelacamento(clusters, cd)
    print(f"  P{passada} apos _reduzir_entrelacamento:           {[len(c) for c in clusters]}")
    clusters = _swap_outliers_entre_clusters(clusters, cd)
    print(f"  P{passada} apos _swap_outliers_entre_clusters:     {[len(c) for c in clusters]}")
    sets_antes = [set(id(e) for e in c) for c in snapshot]
    sets_depois = [set(id(e) for e in c) for c in clusters]
    if sets_antes == sets_depois:
        print(f"  P{passada} estabilizou. break.")
        break
print(f"\nFINAL: {[len(c) for c in clusters]}  min={min(len(c) for c in clusters)}  max={max(len(c) for c in clusters)}")
