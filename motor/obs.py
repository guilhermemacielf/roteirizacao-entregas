"""
Extração da janela de horário a partir de texto livre.

As observações do Instabuy (e o sufixo do NOME FÓRMULA) misturam muita
coisa — preferência de produto, instrução de portaria, etc. — e só o
**horário de entrega** importa pra roteirização.

Regra de ouro: só extrai quando o padrão é claro. Na dúvida devolve
(None, None) — uma janela errada quebra a rota; nenhuma janela só deixa
a entrega livre. Por isso o horário precisa vir em formato de relógio
("10h", "9h30", "09:20"); número solto ("até 10 caixas") é ignorado.

Saída: minutos desde a saída do CD (9h por padrão). "até as 10h" → 60.
"""

import re
import unicodedata

# Entregadores saem do CD às 9h — esse é o "minuto 0" da roteirização.
HORA_BASE_MIN = 9 * 60

# Token de horário em formato de relógio: exige ":" ou "h" (não casa
# número solto). "10h" → (10, ""), "9h30" → (9, 30), "09:20" → (09, 20).
_HORA = r"(\d{1,2})\s*[:h]\s*(\d{0,2})"


def _norm(texto: str) -> str:
    """minúsculo, sem acento, espaços colapsados."""
    t = unicodedata.normalize("NFKD", texto or "")
    t = t.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", t).strip()


def _rel(h, m, base: int) -> int | None:
    """HH:MM absoluto → minutos desde `base`. None se a hora não fizer sentido."""
    try:
        h = int(h)
        m = int(m) if m else 0
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m - base


def extrair_janela(texto: str, base: int = HORA_BASE_MIN) -> tuple[int | None, int | None]:
    """Extrai (janela_inicio, janela_fim) em minutos desde a saída do CD.
    Devolve (None, None) quando não há horário claro no texto."""
    t = _norm(texto)
    if not t:
        return (None, None)

    inicio = fim = None

    # 1. Intervalo: "entre 8h e 12h", "das 8h as 12h"
    m = re.search(r"\b(?:entre|das?)\s+" + _HORA + r"\s*(?:e|as|a)\s+" + _HORA, t)
    if m:
        ini = _rel(m.group(1), m.group(2), base)
        f = _rel(m.group(3), m.group(4), base)
        if ini is not None and f is not None and f > ini:
            return (max(0, ini), f if f > 0 else None)

    # 2. Prazo: "ate as 10:00", "ate 9h30", "entregar ate 10h"
    # \bate\b evita casar "ate" dentro de "abacate", "tomate" etc.
    # Tenta TODAS as ocorrencias (re.finditer); pega a janela mais restritiva
    # (menor fim positivo). Cliente as vezes escreve "ate 9h" no nome E
    # "ate 09:30" no obs — queremos a mais restritiva entre as duas.
    candidatos_fim = []
    for m in re.finditer(r"\bate\b\s+(?:as\s+)?" + _HORA, t):
        f = _rel(m.group(1), m.group(2), base)
        if f is None or f < 0:
            continue  # "ate 8h" (antes da saída) e datas absurdas
        # Janela minima viavel: 15min (CD-ate-1a parada nao eh instantaneo).
        # "Ate 9h" (= 0 min) vira "janela apertada de 15min" — sinaliza
        # prioridade extrema sem fazer o TSP descartar a entrega.
        candidatos_fim.append(max(15, f))
    if candidatos_fim:
        fim = min(candidatos_fim)

    # 3. Início: "apos as 14h", "depois das 14h", "a partir de 14h"
    m = re.search(r"\b(?:apos|depois d[ao]s?|a partir d[ae]s?)\s+(?:as\s+)?" + _HORA, t)
    if m:
        ini = _rel(m.group(1), m.group(2), base)
        if ini is not None:
            inicio = max(0, ini)

    # 4. Período do dia — só se nada mais foi encontrado
    if inicio is None and fim is None:
        if re.search(r"\b(de manha|pela manha|manha)\b", t):
            fim = max(1, 12 * 60 - base)        # até o meio-dia
        elif re.search(r"\b(a tarde|de tarde|tarde)\b", t):
            inicio = max(0, 12 * 60 - base)

    # Sanidade: se por acaso início >= fim, descarta o par (texto ambíguo)
    if inicio is not None and fim is not None and inicio >= fim:
        return (None, None)
    return (inicio, fim)


if __name__ == "__main__":
    testes = [
        "Entregar até as 10:00 / PERMANENTE: enviar frutas maduras",
        "Se possível entregar até 9h30",
        "Tem como me entregar até as 09:20 de amanhã.",
        "ENTREGAR ATE AS 10H- ENVIAR MORANGOS EM SACOLAS SEPARADAS",
        "Diego Cioletti até 09:30",
        "gentileza manda o milho bem molinho, se tiver",
        "PERMANENTE: cliente exigente, conferir até 10 caixas",   # NÃO é horário
        "entregar só de manhã",
        "pode deixar após as 14h com o porteiro",
        "",
    ]
    for s in testes:
        print(f"{extrair_janela(s)!s:14}  ← {s!r}")
