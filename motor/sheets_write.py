r"""
Escrita de volta no Google Sheets: preenche coluna A (entregador) e coluna
B (ordem da rota) em cada linha de entrega, casando pelo CÓDIGO do pedido.

Autenticação suporta 2 modos (detectados via env var):

  A) Service Account (GOOGLE_SHEETS_SA_FILE ou GOOGLE_APPLICATION_CREDENTIALS):
     Pro caso de conta Google sem a política iam.disableServiceAccountKeyCreation.
     Setup: cria Service Account no GCP, baixa JSON, compartilha planilha
     com o client_email do JSON como Editor.

  B) OAuth de usuário (GOOGLE_SHEETS_OAUTH_CLIENT_FILE):
     Pro caso da política bloquear chaves de SA (comum em Workspace
     empresarial e "secure-by-default" de contas com billing ativo). Cria
     um OAuth Client tipo "Desktop app" no GCP, baixa JSON, aponta a env
     var pra ele. Na primeira chamada, abre o browser pro usuário autorizar;
     o token autorizado é salvo em dados/sheets_oauth_token.json e
     reutilizado nas próximas vezes (sem precisar logar de novo).

Sem nenhum configurado, o endpoint retorna 503 com instruções.
"""

import logging
import os
import re
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _TZ_BR = ZoneInfo("America/Sao_Paulo")
except (ImportError, Exception):
    _TZ_BR = None   # cai pra hora local do servidor

log = logging.getLogger(__name__)

# Token persistente do OAuth de usuário (modo B). Salvo na 1ª autorização e
# reutilizado nas próximas. Tem refresh_token, então nunca expira na prática.
_OAUTH_TOKEN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dados", "sheets_oauth_token.json",
)

# Scopes mínimos: ler/escrever em planilhas + listar o Drive (necessário
# pro gspread localizar a planilha por ID).
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsWriteError(Exception):
    pass


def _sa_path() -> str | None:
    return (os.environ.get("GOOGLE_SHEETS_SA_FILE")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))


def _oauth_client_path() -> str | None:
    return os.environ.get("GOOGLE_SHEETS_OAUTH_CLIENT_FILE")


def _abrir_cliente_sa(caminho: str):
    """Modo A: Service Account."""
    if not os.path.exists(caminho):
        raise SheetsWriteError(
            f"Arquivo de Service Account não encontrado: {caminho}. "
            "Confira a env var GOOGLE_SHEETS_SA_FILE."
        )
    try:
        import gspread
    except ImportError as e:
        raise SheetsWriteError(
            "Dependência 'gspread' não instalada. Rode: pip install -r requirements.txt"
        ) from e
    try:
        return gspread.service_account(filename=caminho)
    except Exception as e:
        raise SheetsWriteError(f"falha autenticando Service Account: {e}") from e


def _abrir_cliente_oauth(client_secrets: str):
    """Modo B: OAuth de usuário. Na 1ª vez abre o browser pra autorizar; nas
    próximas usa o token salvo (refresh automático)."""
    if not os.path.exists(client_secrets):
        raise SheetsWriteError(
            f"OAuth Client JSON não encontrado: {client_secrets}. "
            "Confira GOOGLE_SHEETS_OAUTH_CLIENT_FILE."
        )
    try:
        import gspread
    except ImportError as e:
        raise SheetsWriteError(
            "Dependência 'gspread' não instalada. Rode: pip install -r requirements.txt"
        ) from e
    os.makedirs(os.path.dirname(_OAUTH_TOKEN_PATH), exist_ok=True)
    try:
        # gspread.oauth: usa client_secrets pra primeira autorização (abre browser
        # automaticamente), salva token em authorized_user_filename pra reuso.
        return gspread.oauth(
            scopes=_SCOPES,
            credentials_filename=client_secrets,
            authorized_user_filename=_OAUTH_TOKEN_PATH,
        )
    except Exception as e:
        raise SheetsWriteError(
            f"falha autenticando OAuth ({e}). Se o browser não abriu, "
            "tente rodar o app a partir de um terminal local (não SSH)."
        ) from e


def _abrir_cliente():
    """Tenta Service Account primeiro, OAuth depois. Erros de setup viram
    SheetsWriteError com instruções."""
    sa = _sa_path()
    if sa:
        return _abrir_cliente_sa(sa)

    oauth_client = _oauth_client_path()
    if oauth_client:
        return _abrir_cliente_oauth(oauth_client)

    raise SheetsWriteError(
        "Autenticação não configurada. Escolha UM dos modos:\n\n"
        "[A] Service Account: $env:GOOGLE_SHEETS_SA_FILE = 'C:\\path\\sa.json'\n"
        "    (só funciona se sua conta GCP não tiver bloqueio de SA keys)\n\n"
        "[B] OAuth de usuário (recomendado p/ contas Workspace ou com "
        "secure-by-default): $env:GOOGLE_SHEETS_OAUTH_CLIENT_FILE = "
        "'C:\\path\\oauth_client.json'. Na 1ª chamada abre o browser pra "
        "autorizar e salva o token; depois é automático."
    )


def _aba_por_nome(planilha, nome: str):
    """Acha aba pelo nome (caso-insensitive)."""
    alvo = (nome or "").strip().lower()
    for w in planilha.worksheets():
        if (w.title or "").strip().lower() == alvo:
            return w
    return None


def _atualizar_aba_km(planilha, rotas: list[dict]) -> int:
    """Adiciona 1 linha por entregador na aba 'KM' do mesmo arquivo do Sheets:
       A=Entregador, B=KM total da rota, C=Data, D=Hora, E=Qtd entregas.

    KM ja inclui o trecho final ate a casa do entregador (vem do OSRM).
    Qtd entregas eh n_paradas (NAO inclui o trecho da casa).
    Skip rotas Lalamove e Entregas CD (so entregadores reais).
    Append abaixo da ultima linha preenchida da aba. Header (linha 1) deve
    existir; nao mexemos nele.

    Retorna o numero de linhas adicionadas.
    """
    ws_km = _aba_por_nome(planilha, "KM")
    if ws_km is None:
        log.warning("aba 'KM' nao encontrada — pulando registro de quilometragem")
        return 0

    agora = datetime.now(_TZ_BR) if _TZ_BR else datetime.now()
    data_str = agora.strftime("%d/%m/%Y")
    hora_str = agora.strftime("%H:%M")

    novas = []
    for r in rotas:
        if r.get("candidata_lalamove"):
            continue
        ent = r.get("entregador") or {}
        nome_ent = (ent.get("nome") or "").strip()
        # Ignora rota especial "Entregas CD" (entregador virtual sem casa)
        if not nome_ent or nome_ent.lower() == "entregas cd":
            continue
        km_total = float(r.get("distancia_km") or 0)
        n_entregas = int(r.get("n_paradas") or len(r.get("paradas") or []))
        novas.append([nome_ent, km_total, data_str, hora_str, n_entregas])

    if not novas:
        return 0

    # Acha proxima linha livre — pega a 1a linha sem nada na col A apos cabecalho.
    valores_a = ws_km.col_values(1)  # ['Entregador', 'Joao', ...]
    proxima_linha = len(valores_a) + 1   # 1-based
    if proxima_linha < 2:
        proxima_linha = 2   # garante que nao sobrescreve o header

    fim_linha = proxima_linha + len(novas) - 1
    range_str = f"A{proxima_linha}:E{fim_linha}"
    try:
        ws_km.update(range_str, novas, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning("falha gravando aba KM: %s", e)
        return 0

    return len(novas)


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

    # Ordena rotas: entregadores reais em ordem alfabética por nome, depois
    # Lalamoves (ordenados pelo número). Numeração da coluna B é GLOBAL
    # contínua — Camila 1-15, Cristina 16-33, ..., Lalamove1 N+1-N+6, etc.
    # Assim, ordenar a planilha por col B (Ordem) ascending já dá tudo na
    # sequência certa: entregadores agrupados, dentro de cada um a rota
    # certa, e Lalamoves no final.
    def _chave_ordem(rota):
        ent = rota.get("entregador") or {}
        nome = ent.get("nome", "") or ""
        if rota.get("candidata_lalamove"):
            # Pega o nº do "Lalamove N" pra ordenar 1,2,...,10 (não 1,10,2)
            m = re.search(r"(\d+)", nome)
            return (1, int(m.group(1)) if m else 0, nome)
        return (0, 0, nome.lower())

    rotas_ord = sorted(rotas, key=_chave_ordem)

    # Mapa CÓDIGO → (nome_entregador, ordem_global). Ordem incrementa
    # CONTÍNUO entre entregadores.
    cod_para_rota: dict[str, tuple[str, int]] = {}
    ordem_global = 0
    for rota in rotas_ord:
        ent = rota.get("entregador") or {}
        nome_ent = ent.get("nome") or "—"
        for parada in rota.get("paradas") or []:
            cod = str(parada.get("id") or "").strip()
            if cod:
                ordem_global += 1
                cod_para_rota[cod] = (nome_ent, ordem_global)

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

    # Ordena a planilha por col B (Ordem) ascending DEPOIS de escrever.
    # Como Ordem é contínua e segue ordem alfabética dos entregadores,
    # ordenar só por B já agrupa entregadores + sequência correta dentro.
    # Lalamoves vêm depois naturalmente (ordens mais altas).
    if n_atualizadas > 0:
        n_linhas = len(valores)
        n_cols = max(len(l) for l in valores) if valores else 0
        try:
            planilha.batch_update({
                "requests": [{
                    "sortRange": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": cab_idx + 1,   # pula cabeçalho
                            "endRowIndex": n_linhas,
                            "startColumnIndex": 0,
                            "endColumnIndex": n_cols,
                        },
                        "sortSpecs": [
                            {"dimensionIndex": 1, "sortOrder": "ASCENDING"},  # col B = Ordem
                        ],
                    }
                }]
            })
        except Exception as e:
            # Não bloqueia — escrita já foi feita, só não conseguiu ordenar.
            log.warning("escrita OK, mas sort A-Z falhou: %s", e)

        # Banding: cor de fundo alternada por entregador (branco / cinza claro)
        # pra dar visibilidade de onde uma rota termina e a proxima comeca.
        # Como Ordem ja eh contigua e a planilha foi ordenada por B, basta
        # saber quantas paradas cada entregador tem e ir avancando linhas.
        # Cores condicionais (verde/vermelho de observacoes) ficam POR CIMA
        # no Google Sheets, entao continuam visiveis.
        try:
            COR_BRANCO = {"red": 1.0, "green": 1.0, "blue": 1.0}
            COR_CINZA  = {"red": 0.937, "green": 0.937, "blue": 0.937}  # #EFEFEF
            requests_banding = []
            linha_atual = cab_idx + 1   # 0-based row da 1a entrega apos cabecalho
            for idx_ent, rota in enumerate(rotas_ord):
                paradas = rota.get("paradas") or []
                if not paradas:
                    continue
                cor = COR_BRANCO if idx_ent % 2 == 0 else COR_CINZA
                requests_banding.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": linha_atual,
                            "endRowIndex": linha_atual + len(paradas),
                            "startColumnIndex": 0,
                            "endColumnIndex": n_cols,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": cor}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                })
                linha_atual += len(paradas)
            if requests_banding:
                planilha.batch_update({"requests": requests_banding})
        except Exception as e:
            log.warning("banding por entregador falhou: %s", e)

    # Registro na aba KM (1 linha por entregador, com KM/Data/Hora/Qtd).
    # Nao bloqueia o retorno se falhar — escrita principal ja foi feita.
    try:
        n_km = _atualizar_aba_km(planilha, rotas)
    except Exception as e:
        log.warning("nao gravou aba KM: %s", e)
        n_km = 0

    return {
        "linhas_atualizadas": n_atualizadas,
        "nao_encontradas":    nao_encontradas,
        "linhas_km":          n_km,
        "modo_auth":          "service_account" if _sa_path() else "oauth_usuario",
    }
