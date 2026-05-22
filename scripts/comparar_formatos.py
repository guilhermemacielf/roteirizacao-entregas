"""Compara o formato dos enderecos no cache com os do CSV precarregado.
Ajuda a entender por que ha cache miss apesar de precarga."""
import json
import csv
import sys

CACHE = "/app/dados/geocode.cache.json"
CSV_PRE = "/app/dados/enderecos_rota.csv"

cache = json.load(open(CACHE, encoding="utf-8"))
keys = list(cache.keys())
print(f"=== CACHE ({len(keys)} entradas) — 5 amostras ===")
for k in keys[:5]:
    print(f"  {k!r}")
print(f"  ...")
for k in keys[-2:]:
    print(f"  {k!r}")

print(f"\n=== CSV PRECARREGADO — 5 amostras ===")
with open(CSV_PRE, encoding="utf-8") as f:
    leitor = csv.reader(f)
    next(leitor)  # header
    for i, linha in enumerate(leitor):
        if i >= 5:
            break
        end = linha[0]
        chave = " ".join(end.strip().lower().split())
        existe = "HIT" if chave in cache else "MISS"
        print(f"  [{existe}] {end!r}")

print(f"\n=== AMOSTRA DE PROCURA: substring 'goncalves dias' ===")
for k in keys:
    if "goncalves dias" in k.lower() or "gonçalves dias" in k.lower():
        print(f"  no cache: {k!r}")
