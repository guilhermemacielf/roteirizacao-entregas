"""Lista os entregadores do config.json com flag de disponibilidade."""
import json
import os
import sys

caminho = sys.argv[1] if len(sys.argv) > 1 else "/app/dados/config.json"
cfg = json.load(open(caminho))
ent = cfg["entregadores"]
disp = sum(1 for e in ent if e["disponivel"])
print(f"total: {len(ent)}  disponiveis (SIM): {disp}")
print()
for e in ent:
    flag = "SIM" if e["disponivel"] else " - "
    nome = e["nome"]
    prefs = e.get("preferencias", [])
    print(f"  [{flag}] {nome:30}  prefs={len(prefs)}  ({e['lat']:.4f},{e['lng']:.4f})")
