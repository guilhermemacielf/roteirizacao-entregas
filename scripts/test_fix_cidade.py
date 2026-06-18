"""Teste do fix de cidade no fim da string."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.geocode import _gerar_variacoes, _extrair_bairro_cidade, _strip_uf_cep_fim

testes = [
    # Caso reportado pelo user
    "R. Jatoba 400 2602 Vale do Sereno Nova Lima MG 34006-043",
    # Outras cidades RMBH
    "Rua X 100 Centro Contagem MG 32000-000",
    "R. Y 50 Bairro Z Santa Luzia MG 33000-000",
    "Rua A 1 Bairro B Sabara MG 34500-000",
    "R. K 10 Industrial Betim MG 32600-000",
    # BH continua certo
    "R. Gonçalves Dias 2316 Lourdes Belo Horizonte MG 30140-072",
    "Rua Padrao 50 Lourdes 30140-070 Belo Horizonte MG",
    # Sem UF, sem CEP
    "Rua Centro 1 Belo Horizonte",
    # Variantes
    "Rua A 10 Bairro X Vespasiano - MG, 33200-000",
    "Rua B 20 Centro, Lagoa Santa, MG, 33400-000",
]

print("=== _strip_uf_cep_fim ===")
for t in testes:
    print(f"  {t!r}")
    print(f"  -> {_strip_uf_cep_fim(t)!r}")
    print()

print("=== _extrair_bairro_cidade ===")
for t in testes:
    b, c = _extrair_bairro_cidade(t)
    print(f"  {t[:60]:60} -> bairro={b!r}  cidade={c!r}")

print()
print("=== _gerar_variacoes ===")
for t in testes:
    print(f"INPUT:  {t}")
    for v in _gerar_variacoes(t):
        print(f"  v: {v}")
    print()
