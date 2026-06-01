"""Mostra como obs/janelas estao chegando do Instabuy."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.io import baixar_sheet_csv, carregar_entregas_planilha
from motor.obs import extrair_janela

URL = sys.argv[1]
txt = baixar_sheet_csv(URL)
brutos = carregar_entregas_planilha(txt)
print(f"total entregas: {len(brutos)}")
com_obs = [b for b in brutos if b.get("obs", "").strip()]
print(f"com obs preenchida: {len(com_obs)}")
print()
print("=== amostra das 15 primeiras COM obs ===")
for b in com_obs[:15]:
    ini, fim = extrair_janela(f"{b['nome']} {b['obs']}")
    print(f"  ID: {b['codigo']!r}")
    print(f"    nome:  {b['nome']!r}")
    print(f"    obs:   {b['obs']!r}")
    print(f"    janela extraida: ini={ini}  fim={fim}")
    print()
print("=== entregas SEM obs (5 primeiras) ===")
sem_obs = [b for b in brutos if not b.get("obs", "").strip()]
for b in sem_obs[:5]:
    print(f"  ID={b['codigo']} nome={b['nome'][:40]!r}")
