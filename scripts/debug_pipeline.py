"""Debug do pipeline completo (clustering + TSP) pra entender por que cluster
fica abaixo do min_paradas."""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from motor.io import sheets_para_csv_motor, carregar_entregas_texto, carregar_config
from motor.clustering import (kmeans_balanced, _rebalancear_por_km,
                                _mover_paradas_isoladas,
                                _mover_paradas_via_vizinho_mais_proximo,
                                _balancear_por_tempo,
                                _reduzir_clusters_longos,
                                _reduzir_entrelacamento,
                                _swap_outliers_entre_clusters,
                                atribuir, _seed_setorial,
                                _custo_atribuicao, _haversine_km)
from motor.roteirizar import roteirizar, _separar_entregas_cd, _separar_sobras_capacidade


URL = sys.argv[1]
nomes_sel = {n.strip().lower() for n in sys.argv[2].split(",")}

print(f"\n=== INPUT ===")
cd, todos = carregar_config("/app/dados/config.json")
ent = [e for e in todos if e.nome.lower() in nomes_sel]
print(f"entregadores selecionados ({len(ent)}): {[e.nome for e in ent]}")

csv_t, ok, fal = sheets_para_csv_motor(URL)
entregas = carregar_entregas_texto(csv_t)
print(f"entregas baixadas: {len(entregas)}, falhas: {len(fal)}")

print(f"\n=== PASSO 0: separa entregas CD ===")
entregas2, cd_ent = _separar_entregas_cd(entregas, cd)
print(f"entregas restantes: {len(entregas2)} (cd_separadas: {len(cd_ent)})")

print(f"\n=== PASSO 1: separa sobra capacidade ===")
cap = len(ent) * 18
entregas3, lala_pre = _separar_sobras_capacidade(entregas2, cd, cap)
print(f"capacidade: {cap}, sobras Lalamove: {len(lala_pre)}, restantes: {len(entregas3)}")

print(f"\n=== PASSO 2: kmeans_balanced ===")
clusters = kmeans_balanced(entregas3, cd, len(ent),
                            min_paradas_hard=10, max_paradas_hard=18)
print(f"clusters: {[len(c) for c in clusters]}")
print(f"min: {min(len(c) for c in clusters)}, max: {max(len(c) for c in clusters)}")

print(f"\n=== PASSO 3: atribuir ===")
atribuicao = atribuir(clusters, ent, cd)
for i, j in atribuicao.items():
    print(f"  cluster {i} ({len(clusters[i])} entregas) -> {ent[j].nome}")

print(f"\n=== PIPELINE COMPLETO via roteirizar() ===")
rotas = roteirizar(entregas, ent, cd)
print(f"rotas geradas: {len(rotas)}")
for r in rotas:
    nome = r.entregador.nome if r.entregador else "?"
    print(f"  {nome:25}  {r.n_paradas:2d} entregas  {r.distancia_m/1000:6.1f}km  cand_lalamove={r.candidata_lalamove}")
