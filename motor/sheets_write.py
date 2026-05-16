r"""
Escrita de volta no Google Sheets: preenche coluna A (entregador) e coluna
B (ordem da rota) em cada linha de entrega, casando pelo CÓDIGO do pedido.

Diferente do fluxo de LEITURA (que usa o export?format=csv público, sem
autenticação), escrita exige OAuth — aqui usamos Service Account.

Setup uma vez (~10min):
  1. Cria projeto no Google Cloud Console (console.cloud.google.com)
  2. Habilita "Google Sheets API"
  3. Cria Service Account → "Keys" → "Add Key" → JSON → baixa o arquivo
  4. Pega o client_email do JSON (algo tipo
     rotear@projeto.iam.gserviceaccount.com)
  5. Compartilha a planilha com esse email (permissão "Editor")
  6. Salva o JSON local e configura env var:
       $env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\sa.json"
     (ou GOOGLE_SHEETS_SA_FILE — checamos as duas)

Sem isso configurado, o endpoint retorna erro 503 com instruções.
"""

import logging
import os
import re

log = logging.getLogger(__name__)


class SheetsWriteError(Exception):
    pass


def _credenciais_path() -> str | None:
    return (os.environ.get("GOOGLE_SHEETS_SA_FILE")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))


def _abrir_cliente():
    """Carrega gspread + Service Account. Erros de setup viram SheetsWriteError
    com instrução útil em vez de stacktrace."""
    sa = _credenciais_path()
    if not sa:
        raise SheetsWriteError(
            "Service Account não configurada. Setup em ~10min: "
            "1) GCP Console → cria projeto + habilita Sheets API. "
            "2) Cria Service Account, baixa JSON. "
            "3) Compartilha a planilha com o client_email do JSON (Editor). "
            "4) $env:GOOGLE_SHEETS_SA_FILE = 'caminho/sa.json' e reinicia o app."
        )
    if not os.path.exists(sa):
        raise SheetsWriteError(
            f"Arquivo de Service Account não encontrado: {sa}. "
            "Confira a env var GOOGLE_SHEETS_SA_FILE."
        )
    try:
        import gspread
    except ImportError as e:
        raise SheetsWriteError(
            "Dependência 'gspread' não instalada. Rode: pip install -r requirements.txt"
        ) from e
    try:
        return gspread.service_account(filename=sa)
    except Exception as e:
        raise SheetsWriteError(f"falha autenticando com a Service Account: {e}") from e


def _id_planilha(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url or "")
    if not m:
        raise SheetsWriteError("URL não parece ser de uma planilha Google Sheets")
    return m.group(1)


def _gid(url: str) -> int:
    m = re.search(r"[#&?]gid=(\d+)", url or "")
    return int(m.group(1)) if m else 0


def _norm_col(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


def escrever_rotas(url_planilha: str, rotas: list[dict]) -> dict:
    """Para cada entrega das rotas, preenche A=nome_entregador, B=ordem na
    própria planilha (linhas casam pelo CÓDIGO).

    `rotas` no formato de `rotas_para_dict` (lista de dict com chaves
    `entregador.nome`, `candidata_lalamove`, `paradas[].id`, `paradas[].ordem`).

    Retorna {"linhas_atualizadas": N, "nao_encontradas": [codigos...]}.
    """
    cliente = _abrir_cliente()
    sheet_id = _id_planilha(url_planilha)
    gid = _gid(url_planilha)

    try:
        planilha = cliente.open_by_key(sheet_id)
    except Exception as e:
        raise SheetsWriteError(
            f"não consegui abrir a planilha — compartilhou com a Service Account "
            f"como Editor? ({e})"
        ) from e

    ws = None
    for w in planilha.worksheets():
        if w.id == gid:
            ws = w
            break
    if ws is None:
        ws = planilha.get_worksheet(0)

    # Lê a planilha pra achar a linha do cabeçalho e a coluna do CÓDIGO.
    valores = ws.get_all_values()
    if not valores:
        raise SheetsWriteError("planilha vazia")

    cab_idx = None
    for i, linha in enumerate(valores[:10]):
        if any(_norm_col(c) == "endereco" for c in linha):
            cab_idx = i
            break
    if cab_idx is None:
        raise SheetsWriteError("não achei a coluna ENDEREÇO no cabeçalho")

    cab = [_norm_col(c) for c in valores[cab_idx]]
    try:
        i_cod = cab.index("codigo")
    except ValueError as e:
        raise SheetsWriteError("não achei a coluna CÓDIGO no cabeçalho") from e

    # Mapa CÓDIGO → (nome_entregador, ordem) a partir das rotas. Ordem 1-N
    # POR ENTREGADOR (não global): o usuário ordena a planilha A-Z e a
    # sequência de cada rota fica certa.
    cod_para_rota: dict[str, tuple[str, int]] = {}
    for rota in rotas:
        ent = rota.get("entregador") or {}
        nome_ent = ent.get("nome") or "—"
        for parada in rota.get("paradas") or []:
            cod = str(parada.get("id") or "").strip()
            if cod:
                cod_para_rota[cod] = (nome_ent, parada["ordem"])

    # Monta updates em batch (mais rápido que update célula a célula). Inclui
    # cabeçalho "Entregador" / "Ordem" na linha do cab_idx.
    updates = [
        {"range": f"A{cab_idx + 1}", "values": [["Entregador"]]},
        {"range": f"B{cab_idx + 1}", "values": [["Ordem"]]},
    ]
    nao_encontradas: list[str] = []
    n_atualizadas = 0
    for offset, linha in enumerate(valores[cab_idx + 1:], start=1):
        if i_cod >= len(linha):
            continue
        cod = (linha[i_cod] or "").strip()
        if not cod:
            continue
        linha_planilha = cab_idx + 1 + offset   # 1-based
        rota_info = cod_para_rota.get(cod)
        if rota_info is None:
            # Limpa A e B se a entrega não tá nas rotas (pode ter sido removida).
            updates.append({"range": f"A{linha_planilha}", "values": [[""]]})
            updates.append({"range": f"B{linha_planilha}", "values": [[""]]})
            nao_encontradas.append(cod)
            continue
        nome_ent, ordem = rota_info
        updates.append({"range": f"A{linha_planilha}", "values": [[nome_ent]]})
        updates.append({"range": f"B{linha_planilha}", "values": [[ordem]]})
        n_atualizadas += 1

    try:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    except Exception as e:
        raise SheetsWriteError(f"falha escrevendo na planilha: {e}") from e

    return {
        "linhas_atualizadas": n_atualizadas,
        "nao_encontradas":    nao_encontradas,
        "service_account":    _credenciais_path(),
    }
