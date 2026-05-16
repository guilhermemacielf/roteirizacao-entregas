"""
Entrada e saída do motor.

Entrada:
  - entregas: CSV com colunas id, lat, lng [, obs, janela_inicio, janela_fim]
  - config:   JSON com o CD e o cadastro de entregadores

Saída:
  - formatação legível das rotas (terminal) + export CSV

Geocoding (endereço → lat/lng) NÃO está no v1 — o CSV de entregas já vem
com coordenadas. Quando entrar, vai ser Nominatim + cache (endereços
repetem muito entre dias, então o cache mata quase todo o custo).
"""

import csv
import io
import json
import logging
import re
import unicodedata

import requests

from motor.modelos import Entrega, Entregador, CD, Rota
from motor.geocode import geocodificar_lista, _extrair_bairro_cidade
from motor.obs import extrair_janela
from motor.matriz import rota_geometria

log = logging.getLogger(__name__)


# ── Planilha oficial (formato Instabuy) ──────────────────────
def _norm_col(s: str) -> str:
    """Normaliza nome de coluna: sem acento, minúsculo, espaços colapsados."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


# Valores de erro de fórmula do Sheets — linha com isso no endereço é vazia.
_ERROS_PLANILHA = {"#n/a", "#value!", "#ref!", "#name?", "#div/0!", "#null!", "#num!", ""}


def carregar_entregas_planilha(csv_texto: str) -> list[dict]:
    """Lê a planilha de entregas no formato oficial (Instabuy).

    O cabeçalho real pode não estar na 1ª linha (há linha(s) de meta acima),
    então localiza a linha que contém a coluna ENDEREÇO. Mapeia as colunas
    relevantes por nome normalizado (sem acento) e usa o módulo csv de
    verdade — algumas observações têm quebra de linha dentro da célula.

    Devolve [{codigo, nome, endereco, obs}] — SEM geocodificar (o caller faz).
    Linhas sem endereço ou com erro de fórmula (#N/A) são puladas."""
    linhas = list(csv.reader(io.StringIO(csv_texto)))
    if not linhas:
        return []

    cab_idx = None
    for i, linha in enumerate(linhas[:10]):
        if any(_norm_col(c) == "endereco" for c in linha):
            cab_idx = i
            break
    if cab_idx is None:
        raise ValueError(
            "não encontrei a coluna ENDEREÇO na planilha — confira se a aba/URL está certa"
        )

    cab = [_norm_col(c) for c in linhas[cab_idx]]

    def _idx(*nomes):
        for nome in nomes:
            if nome in cab:
                return cab.index(nome)   # primeira ocorrência
        return None

    i_end = _idx("endereco")
    i_nome = _idx("nome (formula)", "nome(formula)", "nome formula", "nome")
    i_obs = _idx("observacoes do instabuy", "observacoes", "observacao", "obs")
    i_cod = _idx("codigo")

    def _campo(linha, idx):
        return linha[idx].strip() if idx is not None and idx < len(linha) else ""

    entregas = []
    for linha in linhas[cab_idx + 1:]:
        endereco = _campo(linha, i_end)
        if endereco.lower() in _ERROS_PLANILHA:
            continue
        entregas.append({
            "codigo":   _campo(linha, i_cod),
            "nome":     _campo(linha, i_nome),
            "endereco": endereco,
            "obs":      _campo(linha, i_obs),
        })
    return entregas


# ── Google Sheets ────────────────────────────────────────────
def url_export_csv(url: str) -> str:
    """Converte a URL normal de uma planilha Google Sheets na URL de export
    CSV. Aceita .../edit#gid=123, .../edit?gid=123, etc. e devolve
    .../export?format=csv&gid=123. Sem gid → assume a primeira aba (gid=0)."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url or "")
    if not m:
        raise ValueError("a URL não parece ser de uma planilha do Google Sheets")
    sheet_id = m.group(1)
    gm = re.search(r"[#&?]gid=(\d+)", url)
    gid = gm.group(1) if gm else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def baixar_sheet_csv(url: str) -> str:
    """Baixa o conteúdo CSV de uma planilha Google Sheets pública (ou
    'qualquer pessoa com o link'). Devolve o texto do CSV."""
    export = url_export_csv(url)
    try:
        r = requests.get(export, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        raise ValueError(
            f"não consegui baixar a planilha — confira se está compartilhada "
            f"como 'qualquer pessoa com o link pode ver'. ({e})"
        ) from e
    return r.content.decode("utf-8-sig", errors="replace")


# ── Pipeline: Sheet → CSV pronto pro motor ─────────────────────
def _csv_motor(linhas: list[dict]) -> str:
    """Serializa entregas já resolvidas (com lat/lng, bairro e janela) no CSV
    que `carregar_entregas_texto` consome. Mantém o mesmo formato do upload
    manual pra reaproveitar o fluxo da UI."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "nome", "lat", "lng", "bairro", "obs", "janela_inicio", "janela_fim"])
    for e in linhas:
        w.writerow([
            e["id"], e["nome"], e["lat"], e["lng"],
            e.get("bairro", ""), e["obs"],
            "" if e["janela_inicio"] is None else e["janela_inicio"],
            "" if e["janela_fim"]    is None else e["janela_fim"],
        ])
    return buf.getvalue()


def sheets_para_csv_motor(
    url: str, *, progresso=None
) -> tuple[str, list[dict], list[dict]]:
    """Pipeline completo: URL do Sheets → CSV pronto pro motor + relatórios.

    Etapas:
      1. baixa o CSV da planilha;
      2. parseia o formato Instabuy (cabeçalho fora da 1ª linha);
      3. geocodifica cada endereço via Nominatim (com cache em disco);
      4. extrai janela de horário do NOME (FÓRMULA) + OBSERVAÇÕES (concatenados,
         já que o sufixo "até 10h" às vezes só aparece num dos dois);
      5. monta o CSV no formato `id,nome,lat,lng,obs,janela_inicio,janela_fim`.

    Devolve `(csv_texto, ok, falhas)`:
      - `csv_texto`: pronto pra `carregar_entregas_texto` (ou pro front rotear).
      - `ok`: dicts {id,nome,endereco,lat,lng,obs,janela_inicio,janela_fim}
        — útil pra a UI mostrar prévia/contagem sem reparsear o CSV.
      - `falhas`: dicts {id,nome,endereco,motivo} dos que NÃO geocodificaram.

    O `id` é o CÓDIGO do pedido (ex: "99Y0-YLFE"); na falta dele, um fallback
    sequencial. `progresso(feito,total)` é repassado pro geocoding."""
    csv_texto = baixar_sheet_csv(url)
    brutos = carregar_entregas_planilha(csv_texto)
    if not brutos:
        return ("", [], [])

    enderecos = [b["endereco"] for b in brutos]
    coords = geocodificar_lista(enderecos, progresso=progresso)

    ok: list[dict] = []
    falhas: list[dict] = []
    for i, b in enumerate(brutos, start=1):
        cod = b.get("codigo") or f"L{i}"
        coord = coords.get(b["endereco"])
        if coord is None:
            falhas.append({
                "id": cod, "nome": b["nome"], "endereco": b["endereco"],
                "obs": b["obs"],
                "motivo": "endereço não encontrado pelo Nominatim",
            })
            continue
        # O horário pode estar no sufixo do NOME (FÓRMULA) e/ou nas observações.
        # Concat com espaço entre os dois pra não colar palavras.
        ini, fim = extrair_janela(f"{b['nome']} {b['obs']}")
        # Bairro extraído do endereço bruto — usado pra preferências por
        # bairro do entregador. None vira string vazia.
        bairro, _ = _extrair_bairro_cidade(b["endereco"])
        ok.append({
            "id":             cod,
            "nome":           b["nome"],
            "endereco":       b["endereco"],
            "lat":            coord[0],
            "lng":            coord[1],
            "bairro":         bairro or "",
            "obs":            b["obs"],
            "janela_inicio":  ini,
            "janela_fim":     fim,
        })
    return (_csv_motor(ok), ok, falhas)


def _parse_entregas(reader: csv.DictReader) -> list[Entrega]:
    """Converte um DictReader (colunas id, lat, lng [, nome, obs, janela_*]) em
    lista de Entrega. Linhas com lat/lng inválido são ignoradas (com log)."""
    entregas: list[Entrega] = []
    for i, row in enumerate(reader, start=2):  # linha 2 = primeira de dados
        try:
            lat = float(str(row["lat"]).replace(",", "."))
            lng = float(str(row["lng"]).replace(",", "."))
        except (KeyError, ValueError, TypeError):
            log.warning("Linha %d ignorada: lat/lng inválido (%s)", i, row)
            continue
        ji = row.get("janela_inicio")
        jf = row.get("janela_fim")
        entregas.append(Entrega(
            id=str(row.get("id") or f"L{i}").strip(),
            lat=lat,
            lng=lng,
            nome=(row.get("nome") or "").strip(),
            obs=(row.get("obs") or "").strip(),
            bairro=(row.get("bairro") or "").strip(),
            janela_inicio=int(ji) if ji not in (None, "", "None") else None,
            janela_fim=int(jf) if jf not in (None, "", "None") else None,
        ))
    return entregas


def carregar_entregas(caminho_csv: str) -> list[Entrega]:
    """Lê o CSV de entregas de um arquivo. Colunas obrigatórias: id, lat, lng.
    Opcionais: obs, janela_inicio, janela_fim (minutos desde o início)."""
    with open(caminho_csv, encoding="utf-8-sig") as f:
        return _parse_entregas(csv.DictReader(f))


def carregar_entregas_texto(texto: str) -> list[Entrega]:
    """Mesma coisa que carregar_entregas, mas a partir do conteúdo do CSV
    em memória (usado pela UI web, onde o CSV vem do upload)."""
    texto = texto.lstrip("﻿")  # tira BOM se vier
    return _parse_entregas(csv.DictReader(io.StringIO(texto)))


def carregar_config(caminho_json: str) -> tuple[CD, list[Entregador]]:
    """Lê o JSON de configuração: CD + lista de entregadores disponíveis."""
    with open(caminho_json, encoding="utf-8") as f:
        cfg = json.load(f)
    c = cfg["cd"]
    cd = CD(lat=float(c["lat"]), lng=float(c["lng"]), nome=c.get("nome", "CD"))
    entregadores = [
        Entregador(
            id=str(e["id"]),
            nome=e["nome"],
            lat=float(e["lat"]),
            lng=float(e["lng"]),
            preferencias=[p.strip() for p in (e.get("preferencias") or []) if p and p.strip()],
        )
        for e in cfg["entregadores"]
        if e.get("disponivel", True)
    ]
    return cd, entregadores


def _hms(segundos: int) -> str:
    h, resto = divmod(int(segundos), 3600)
    m, s = divmod(resto, 60)
    return f"{h}h{m:02d}" if h else f"{m}min"


def imprimir_rotas(rotas: list[Rota]) -> None:
    """Imprime as rotas de forma legível no terminal."""
    if not rotas:
        print("Nenhuma rota gerada.")
        return
    print(f"\n{'='*60}")
    print(f"  {len(rotas)} ROTAS GERADAS")
    print(f"{'='*60}")
    total_km = sum(r.distancia_m for r in rotas) / 1000
    total_paradas = sum(r.n_paradas for r in rotas)
    for r in sorted(rotas, key=lambda x: x.entregador.nome if x.entregador else "~"):
        quem = r.entregador.nome if r.entregador else "LALAMOVE (candidata)"
        print(f"\n▶ {quem}  —  {r.n_paradas} entregas  ·  "
              f"{r.distancia_m/1000:.1f} km  ·  {_hms(r.duracao_s)}")
        for p in r.paradas:
            obs = f"  ({p.entrega.obs})" if p.entrega.obs else ""
            print(f"   {p.ordem:2d}. {p.entrega.id:<12} "
                  f"[{p.entrega.lat:.5f}, {p.entrega.lng:.5f}]"
                  f"  chega ~{_hms(p.chegada_estimada_s)}{obs}")
    print(f"\n{'-'*60}")
    print(f"  TOTAL: {total_paradas} entregas · {total_km:.1f} km · "
          f"{len(rotas)} rotas")
    abaixo_min = [r for r in rotas if r.n_paradas < 10]
    if abaixo_min:
        nomes = ", ".join(r.entregador.nome if r.entregador else "?" for r in abaixo_min)
        print(f"  ⚠ {len(abaixo_min)} rota(s) abaixo de 10 paradas: {nomes}")
    print(f"{'='*60}\n")


def rotas_para_dict(rotas: list[Rota], cd: CD) -> dict:
    """Serializa as rotas + CD num dict JSON-friendly pra a API/UI web.
    Inclui coordenadas + geometria real (polyline OSRM) pra o front desenhar
    rotas seguindo as ruas reais. Pra Lalamove (entregador virtual), geometria
    é linha reta (Lalamove não passa pelo OSRM).
    """
    rotas_json = []
    for r in rotas:
        ent = r.entregador

        # Geometria da polyline via OSRM (ruas reais). Pra Lalamove, a
        # sequência é CD → paradas (sem casa final — Lalamove não tem casa,
        # o app cobra pela ida apenas). Pra entregador real, CD → paradas → casa.
        if r.candidata_lalamove or ent is None:
            seq = [(cd.lat, cd.lng)] + [(p.entrega.lat, p.entrega.lng) for p in r.paradas]
        else:
            seq = ([(cd.lat, cd.lng)]
                   + [(p.entrega.lat, p.entrega.lng) for p in r.paradas]
                   + [(ent.lat, ent.lng)])
        geom = rota_geometria(seq)

        rotas_json.append({
            "entregador": None if ent is None else {
                "id": ent.id, "nome": ent.nome, "lat": ent.lat, "lng": ent.lng,
            },
            "candidata_lalamove": bool(r.candidata_lalamove),
            "n_paradas":     r.n_paradas,
            "distancia_km":  round(r.distancia_m / 1000, 2),
            "duracao_s":     int(r.duracao_s),
            "geometry":      [[lat, lng] for lat, lng in geom],
            "paradas": [
                {
                    "ordem":              p.ordem,
                    "id":                 p.entrega.id,
                    "nome":               p.entrega.nome,
                    "lat":                p.entrega.lat,
                    "lng":                p.entrega.lng,
                    "obs":                p.entrega.obs,
                    "janela_inicio":      p.entrega.janela_inicio,
                    "janela_fim":         p.entrega.janela_fim,
                    "chegada_estimada_s": int(p.chegada_estimada_s),
                }
                for p in r.paradas
            ],
        })
    rotas_normais  = [r for r in rotas if not r.candidata_lalamove]
    rotas_lalamove = [r for r in rotas if r.candidata_lalamove]
    return {
        "cd": {"lat": cd.lat, "lng": cd.lng, "nome": cd.nome},
        "rotas": rotas_json,
        "resumo": {
            "n_rotas":              len(rotas),
            "n_rotas_entregadores": len(rotas_normais),
            "n_rotas_lalamove":     len(rotas_lalamove),
            "total_entregas":       sum(r.n_paradas for r in rotas),
            "entregas_lalamove":    sum(r.n_paradas for r in rotas_lalamove),
            "total_km":             round(sum(r.distancia_m for r in rotas) / 1000, 1),
            "km_lalamove":          round(sum(r.distancia_m for r in rotas_lalamove) / 1000, 1),
        },
    }


def exportar_csv(rotas: list[Rota], caminho_csv: str) -> None:
    """Exporta as rotas pra um CSV plano (uma linha por parada)."""
    with open(caminho_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "entregador", "ordem", "entrega_id", "lat", "lng",
            "chegada_estimada_s", "obs", "rota_km", "rota_duracao_s",
            "candidata_lalamove",
        ])
        for r in rotas:
            quem = r.entregador.nome if r.entregador else "LALAMOVE"
            for p in r.paradas:
                w.writerow([
                    quem, p.ordem, p.entrega.id, p.entrega.lat, p.entrega.lng,
                    p.chegada_estimada_s, p.entrega.obs,
                    round(r.distancia_m / 1000, 2), r.duracao_s,
                    int(r.candidata_lalamove),
                ])
    log.info("Rotas exportadas para %s", caminho_csv)
