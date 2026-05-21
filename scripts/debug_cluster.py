"""Debug clustering pra uma URL de Sheets + lista de entregadores.

Uso:
  python scripts/debug_cluster.py <URL> --entregadores "Karina,Tamara,Leia,Cristina,Ana Carolina,Camila"

Imprime cada cluster com:
  - num paradas
  - diâmetro (km)
  - dist média ao centróide
  - lista das paradas mais distantes do centróide (outliers internos)
"""
import argparse
import math
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.io import sheets_para_csv_motor, carregar_entregas_texto, carregar_config
from motor.clustering import kmeans_balanced, _haversine_km, _diametro_km


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="URL do Google Sheets")
    ap.add_argument("--entregadores", required=True,
                    help="Nomes separados por virgula")
    ap.add_argument("--config", default="dados/config.json")
    args = ap.parse_args()

    nomes_selec = {n.strip().lower() for n in args.entregadores.split(",")}
    cd, todos_ent = carregar_config(args.config)
    ent_selec = [e for e in todos_ent if e.nome.lower() in nomes_selec]
    nao_achados = nomes_selec - {e.nome.lower() for e in ent_selec}
    if nao_achados:
        print(f"AVISO: entregadores nao encontrados: {nao_achados}")
    print(f"Entregadores selecionados ({len(ent_selec)}):")
    for e in ent_selec:
        print(f"  {e.nome}  ({e.lat:.5f},{e.lng:.5f})  prefs={len(e.preferencias)}")

    print(f"\nBaixando planilha...")
    csv_texto, ok, falhas = sheets_para_csv_motor(args.url)
    print(f"  {len(ok)} OK, {len(falhas)} falhas")
    if falhas:
        print(f"  Falhas: {[f['endereco'][:50] for f in falhas[:3]]}")

    entregas = carregar_entregas_texto(csv_texto)
    print(f"  {len(entregas)} entregas no CSV final")

    m = len(ent_selec)
    print(f"\nClusterizando em {m} clusters (min=10, max=18)...")
    clusters = kmeans_balanced(entregas, cd, m,
                                min_paradas_hard=10, max_paradas_hard=18)

    print(f"\n{'='*70}")
    for i, cl in enumerate(clusters):
        if not cl:
            print(f"Cluster {i}: VAZIO")
            continue
        lat_c = sum(e.lat for e in cl) / len(cl)
        lng_c = sum(e.lng for e in cl) / len(cl)
        diam = _diametro_km(cl, cd)
        dists = [_haversine_km(e.lat, e.lng, lat_c, lng_c) for e in cl]
        d_max = max(dists)
        d_med = sum(dists) / len(dists)
        print(f"\nCluster {i}: {len(cl)} paradas  centroide=({lat_c:.5f},{lng_c:.5f})  "
              f"diam={diam:.1f}km  dist_max={d_max:.1f}km  dist_med={d_med:.1f}km")
        # Paradas ordenadas por distancia ao centroide DESC (outliers no topo)
        with_dist = sorted(zip(cl, dists), key=lambda t: -t[1])
        for parada, d in with_dist:
            marcador = " <- OUTLIER" if d > d_med * 2 else ""
            bairro = parada.bairro or "?"
            print(f"    {d:5.2f}km  {parada.id:<14} {parada.nome[:25]:25}  "
                  f"({parada.lat:.5f},{parada.lng:.5f})  bairro={bairro}{marcador}")


if __name__ == "__main__":
    main()
