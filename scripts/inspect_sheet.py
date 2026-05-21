"""Baixa a planilha e imprime as colunas relevantes (E, F, J, L) cruas."""
import sys
import os
import csv
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from motor.io import baixar_sheet_csv

url = sys.argv[1]
txt = baixar_sheet_csv(url)
linhas = list(csv.reader(io.StringIO(txt)))
if not linhas:
    print("vazia")
    sys.exit(1)

print(f"total linhas: {len(linhas)}")
print(f"cabecalho (15 primeiras cols): {linhas[0][:15]}")
print()
print(f"{'E (nome)':25} | {'F (endereco)':35} | {'J (disp)':12} | {'L (prefs)':40}")
print("-" * 130)
for i, linha in enumerate(linhas[1:20], start=1):
    e = (linha[4] if len(linha) > 4 else "").strip()
    f = (linha[5] if len(linha) > 5 else "").strip()
    j = (linha[9] if len(linha) > 9 else "").strip()
    l = (linha[11] if len(linha) > 11 else "").strip()
    if not e and not f:
        continue
    print(f"{e[:25]:25} | {f[:35]:35} | {repr(j):12} | {l[:40]}")
