"""
Cálculo do valor pago a cada entregador por rota.

Fórmula:
    total = km × valor_km + soma(valor_por_entrega de cada parada)

Onde valor_por_entrega vem da tabela carregada da planilha:
    - lookup pela CIDADE da entrega (ex: "Contagem" → R$ 9,54)
    - se a cidade for BH (ou não tiver cidade), tenta pelo BAIRRO (ex: "Ipê" → R$ 8,76)
    - senão, valor padrão (R$ 7,20 — BH e Vila da Serra)

Cidades fora de BH têm prioridade sobre bairro porque a tabela do usuário
lista cidades como entradas próprias (Contagem, Vespasiano, Sabará, etc.)
e pra essas o valor é da cidade inteira, não importa o bairro.
"""

import unicodedata


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


def calcular_valor_rota(rota: dict, valores: dict) -> dict:
    """Recebe uma rota já no formato JSON de rotas_para_dict e a tabela de
    valores; devolve dict com {valor_total, valor_km, valor_entregas, memoria}.

    `memoria` é uma lista de linhas legíveis pro front exibir, ex:
        [
          "13.5 km × R$ 0,70/km = R$ 9,45",
          "5x R$ 7,20 (padrão BH/Vila da Serra) = R$ 36,00",
          "1x R$ 9,54 (Contagem) = R$ 9,54",
          ...
          "TOTAL = R$ 54,99"
        ]
    """
    valor_km        = float(valores.get("valor_km", 0.70))
    valor_padrao    = float(valores.get("valor_padrao", 7.20))
    por_bairro_raw  = valores.get("por_bairro", {}) or {}
    por_bairro      = {_norm(k): float(v) for k, v in por_bairro_raw.items()
                       if not k.startswith("_")}

    km = float(rota.get("distancia_km", 0))
    valor_km_total = round(km * valor_km, 2)

    # Agrupa contagem por valor (ex: 5x padrão, 1x Contagem)
    contagem_por_chave: dict[tuple[float, str], int] = {}
    for parada in rota.get("paradas") or []:
        bairro_norm = _norm(parada.get("bairro") or "")

        # Lookup: primeiro o bairro inteiro como veio (pode ser cidade ou bairro).
        v = por_bairro.get(bairro_norm)
        rotulo = parada.get("bairro") or "—"
        if v is None:
            v = valor_padrao
            rotulo = "padrão BH/Vila da Serra"

        chave = (v, rotulo)
        contagem_por_chave[chave] = contagem_por_chave.get(chave, 0) + 1

    valor_entregas = 0.0
    memoria = [f"{km:.1f} km × R$ {valor_km:.2f}/km = R$ {valor_km_total:.2f}"]
    for (v, rotulo), n in sorted(contagem_por_chave.items(),
                                  key=lambda kv: (-kv[1], kv[0][1])):
        subtotal = round(v * n, 2)
        valor_entregas += subtotal
        memoria.append(f"{n}× R$ {v:.2f} ({rotulo}) = R$ {subtotal:.2f}")

    total = round(valor_km_total + valor_entregas, 2)
    memoria.append(f"TOTAL = R$ {total:.2f}")

    return {
        "valor_total":    total,
        "valor_km":       valor_km_total,
        "valor_entregas": round(valor_entregas, 2),
        "memoria":        memoria,
    }


def calcular_valor_todas(rotas: list[dict], valores: dict) -> list[dict]:
    """Injeta 'pagamento' (dict do calcular_valor_rota) em cada rota e
    devolve a mesma lista. Pra Lalamove não calcula — esse pagamento é do
    app (não do entregador da empresa)."""
    for rota in rotas:
        if rota.get("candidata_lalamove"):
            rota["pagamento"] = None
            continue
        rota["pagamento"] = calcular_valor_rota(rota, valores)
    return rotas
