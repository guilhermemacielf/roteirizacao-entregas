"""Roda o pipeline completo e verifica se as janelas de horario foram
respeitadas. Sai inteiro = OK; lista entregas com violacao = problema."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.io import sheets_para_csv_motor, carregar_entregas_texto, carregar_config
from motor.roteirizar import roteirizar


URL = sys.argv[1]
NOMES = ["Cristina", "Tamara", "Leia", "Karina", "Camila", "Ana Carolina"]

cd, todos = carregar_config("/app/dados/config.json")
ent = [e for e in todos if e.nome in NOMES]

csv_t, ok, _ = sheets_para_csv_motor(URL)
entregas = carregar_entregas_texto(csv_t)

# Quantas tem janela?
com_ini = sum(1 for e in entregas if e.janela_inicio is not None)
com_fim = sum(1 for e in entregas if e.janela_fim is not None)
print(f"entregas: {len(entregas)}")
print(f"com janela_inicio: {com_ini}")
print(f"com janela_fim:    {com_fim}")
print()

# Mostra as com janela
print("=== entregas com janela ===")
for e in entregas:
    if e.janela_inicio is not None or e.janela_fim is not None:
        print(f"  id={e.id}  nome={e.nome[:40]!r}")
        print(f"     obs={e.obs[:80]!r}")
        print(f"     janela: {e.janela_inicio} -> {e.janela_fim} (min desde 9h)")

print()
print("Roteirizando...")
rotas = roteirizar(entregas, ent, cd)
print(f"{len(rotas)} rotas")

# Verifica violacoes
print()
print("=== verificacao de respeito das janelas ===")
violacoes = 0
respeitos = 0
for r in rotas:
    nome = r.entregador.nome if r.entregador else "?"
    for p in r.paradas:
        e = p.entrega
        if e.janela_inicio is None and e.janela_fim is None:
            continue
        chegada_min = p.chegada_estimada_s / 60
        violado = False
        if e.janela_inicio is not None and chegada_min < e.janela_inicio:
            violado = "antes do janela_inicio"
        if e.janela_fim is not None and chegada_min > e.janela_fim:
            violado = f"DEPOIS do janela_fim ({chegada_min:.0f} > {e.janela_fim})"
        flag = "VIOLOU" if violado else "ok    "
        print(f"  [{flag}] {nome:13} parada {p.ordem:2d}: chega={chegada_min:5.0f}min "
              f"(janela {e.janela_inicio}-{e.janela_fim})  {e.nome[:35]} | {violado or ''}")
        if violado:
            violacoes += 1
        else:
            respeitos += 1
print(f"\nResumo: {respeitos} ok, {violacoes} VIOLACOES")
