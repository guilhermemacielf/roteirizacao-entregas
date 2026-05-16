"""
Pipeline de sincronização da planilha de cadastro de entregadores + valores.

A planilha do usuário tem duas tabelas lado a lado na mesma aba:

  - Cols A/B = tabela de valores por bairro/cidade (R$ por entrega).
    Inclui "Valor por km" (R$/km), "Valor padrão" (BH e Vila da Serra)
    e linhas por bairro/cidade com preço diferenciado (Contagem, Vespasiano,
    Sabará, Lagoa Santa, Ipê, Ouro Velho, etc.).

  - Cols E..L = cadastro dos entregadores:
      E = Entregador (nome)
      F = Endereço residência (geocodificar)
      J = DISPONIBILIDADE ("SIM" ou vazio)
      K = CAPACIDADE MÁXIMA (int, default 18)
      L = ROTA PREFERENCIAL (bairros separados por vírgula OU quebra de
          linha dentro da célula)

O pipeline baixa o CSV, parseia as duas tabelas, geocodifica cada endereço
e sobrescreve dados/config.json + dados/valores.json.
"""

import csv
import io
import json
import logging
import os
import re

from motor.geocode import geocodificar_lista
from motor.io import baixar_sheet_csv

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "dados", "config.json")
VALORES_PATH = os.path.join(BASE_DIR, "dados", "valores.json")


def _parse_dinheiro(s: str) -> float | None:
    """'R$ 7,20' / '7.20' / '0,70' → 7.20. Retorna None se não dá pra parsear."""
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(s))
    if not s:
        return None
    # Heurística: se tem vírgula e ponto, vírgula é decimal (formato BR).
    # Se só tem vírgula, vírgula é decimal. Só ponto = decimal.
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _split_bairros(s: str) -> list[str]:
    """Coluna L pode ter bairros separados por vírgula OU quebra de linha."""
    if not s:
        return []
    partes = re.split(r"[,\n;]", s)
    return [p.strip() for p in partes if p and p.strip()]


def _eh_disponivel(s: str) -> bool:
    return (s or "").strip().lower() in ("sim", "true", "1", "yes", "s")


def sincronizar_entregadores(url: str, *, progresso=None) -> dict:
    """Baixa a planilha, parseia entregadores + tabela de valores, geocodifica
    os endereços e salva nos arquivos. Retorna estatísticas.

    `progresso(feito, total)` é repassado pro geocode.
    """
    csv_texto = baixar_sheet_csv(url)
    linhas = list(csv.reader(io.StringIO(csv_texto)))
    if not linhas:
        raise ValueError("planilha vazia")

    # ── Tabela de valores (cols A+B) ───────────────────────────
    # Cabeçalho na linha 0: "Bairro", "ValorEntrega". Dados começam na linha 1.
    # "Valor por km" e "Valor padrão" e "Sacolas" são chaves especiais.
    valor_km = None
    valor_padrao = None
    valores_bairro: dict[str, float] = {}
    for linha in linhas[1:]:  # pula cabeçalho
        if len(linha) < 2:
            continue
        chave = (linha[0] or "").strip()
        valor = _parse_dinheiro(linha[1])
        if not chave or valor is None:
            continue
        chave_lower = chave.lower()
        if "valor por km" in chave_lower:
            valor_km = valor
        elif "valor padr" in chave_lower:  # "padrão" / "padrao"
            valor_padrao = valor
        elif "sacola" in chave_lower:
            # campo separado; guardamos com a chave especial pra usar se quiser
            valores_bairro["_sacola"] = valor
        else:
            valores_bairro[chave] = valor

    valores = {
        "valor_km":       valor_km if valor_km is not None else 0.70,
        "valor_padrao":   valor_padrao if valor_padrao is not None else 7.20,
        "por_bairro":     valores_bairro,
    }

    # ── Cadastro de entregadores (cols E..L) ───────────────────
    # Cabeçalho na linha 0; dados começam na linha 1.
    cabecalho = linhas[0]
    def _idx_col(nome_norm):
        """Acha a coluna pelo cabeçalho (caso-insensitive, sem acento)."""
        import unicodedata
        def norm(s):
            s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
            return " ".join(s.lower().split())
        nome_norm = norm(nome_norm)
        for i, c in enumerate(cabecalho):
            if norm(c) == nome_norm:
                return i
        return None

    iE = _idx_col("entregador")
    iF = _idx_col("endereco residencia") or _idx_col("endereço residência")
    iJ = _idx_col("disponibilidade")
    iK = _idx_col("capacidade maxima") or _idx_col("capacidade máxima")
    iL = _idx_col("rota preferencial")

    if iE is None or iF is None:
        raise ValueError(
            "cabeçalho da planilha não tem 'Entregador' / 'Endereço residência'"
        )

    brutos = []
    for n, linha in enumerate(linhas[1:], start=1):
        def col(i):
            return linha[i].strip() if i is not None and i < len(linha) else ""
        nome = col(iE)
        endereco = col(iF)
        if not nome or not endereco:
            continue
        brutos.append({
            "id":           f"E{n}",   # estável dentro de uma sincronização
            "nome":         nome,
            "endereco":     endereco,
            "disponivel":   _eh_disponivel(col(iJ)),
            "capacidade":   int(_parse_dinheiro(col(iK)) or 18),
            "preferencias": _split_bairros(col(iL)),
        })

    if not brutos:
        raise ValueError("não encontrei nenhum entregador na planilha")

    # Geocodifica os endereços (cache em disco do motor.geocode reaproveita
    # endereços que já apareceram antes, então re-sync é rápido).
    enderecos = [b["endereco"] for b in brutos]
    coords = geocodificar_lista(enderecos, progresso=progresso)

    ok = []
    falhas = []
    for b in brutos:
        coord = coords.get(b["endereco"])
        if coord is None:
            falhas.append({"nome": b["nome"], "endereco": b["endereco"],
                           "motivo": "endereço não geocodificado"})
            continue
        ok.append({
            "id":           b["id"],
            "nome":         b["nome"],
            "lat":          coord[0],
            "lng":          coord[1],
            "endereco":     b["endereco"],
            "disponivel":   b["disponivel"],
            "preferencias": b["preferencias"],
        })

    # ── Persiste config.json (preserva o CD existente) e valores.json ──
    cfg_atual = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg_atual = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    cfg_novo = {
        "_comentario": cfg_atual.get("_comentario",
            "CD + entregadores. Sincronizado da planilha do Sheets."),
        "cd":           cfg_atual.get("cd", {"nome": "CD", "lat": 0, "lng": 0}),
        "entregadores": ok,
        "_sheet_url":   url,
    }

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg_novo, f, ensure_ascii=False, indent=2)

    with open(VALORES_PATH, "w", encoding="utf-8") as f:
        json.dump(valores, f, ensure_ascii=False, indent=2)

    log.info("sincronizados %d entregadores (%d falhas), %d valores por bairro",
             len(ok), len(falhas), len(valores_bairro))

    return {
        "n_entregadores": len(ok),
        "falhas":         falhas,
        "valor_km":       valores["valor_km"],
        "valor_padrao":   valores["valor_padrao"],
        "n_valores_bairro": len(valores_bairro),
    }


def carregar_valores() -> dict | None:
    """Carrega dados/valores.json se existir."""
    try:
        with open(VALORES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
