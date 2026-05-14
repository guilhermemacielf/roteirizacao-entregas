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
import json
import logging
from motor.modelos import Entrega, Entregador, CD, Rota

log = logging.getLogger(__name__)


def carregar_entregas(caminho_csv: str) -> list[Entrega]:
    """Lê o CSV de entregas. Colunas obrigatórias: id, lat, lng.
    Opcionais: obs, janela_inicio, janela_fim (minutos desde o início)."""
    entregas: list[Entrega] = []
    with open(caminho_csv, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
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
                obs=(row.get("obs") or "").strip(),
                janela_inicio=int(ji) if ji not in (None, "", "None") else None,
                janela_fim=int(jf) if jf not in (None, "", "None") else None,
            ))
    return entregas


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
