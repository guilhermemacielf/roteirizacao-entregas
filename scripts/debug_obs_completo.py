"""Pra cada entrega da planilha, mostra nome, obs e o que extrair_janela retornou."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.io import baixar_sheet_csv, carregar_entregas_planilha
from motor.obs import extrair_janela

URL = sys.argv[1]
brutos = carregar_entregas_planilha(baixar_sheet_csv(URL))
print(f"total: {len(brutos)} entregas\n")

# Filtra so as que TÊM "ate" ou ":" ou "h" no nome ou obs (candidatas a janela)
import re
cand = []
for b in brutos:
    nome = b.get("nome") or ""
    obs = b.get("obs") or ""
    texto = f"{nome} {obs}"
    if re.search(r"\b(?:ate|entre|apos|depois|manh|tarde|noite|h\d|:\d{2})", texto.lower()):
        cand.append(b)

print(f"=== {len(cand)} entregas com palavra-chave de horario ===\n")
for b in cand:
    ini, fim = extrair_janela(f"{b['nome']} {b['obs']}")
    flag = "EXTRAIU" if (ini or fim) else "NAO_EXTRAIU"
    print(f"  [{flag}] {b['codigo']}")
    print(f"    nome: {b['nome']!r}")
    print(f"    obs:  {(b['obs'] or '')[:80]!r}")
    print(f"    janela: ini={ini} fim={fim}")
    print()
