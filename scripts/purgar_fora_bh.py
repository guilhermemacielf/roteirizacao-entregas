"""Purga do cache de geocode os endereços fora de BH que estavam errados
e re-geocodifica com a logica corrigida (cidade no fim).

Pra cada endereço da planilha:
  1. Usa _gerar_variacoes (logica NOVA) pra extrair cidade.
  2. Se cidade != Belo Horizonte E a chave canonica do endereco
     existe no cache (provavelmente com coord errada apontando pra BH):
     - Remove do cache
     - Re-geocodifica (cai numa coord certa em Nova Lima/Contagem/etc)
  3. Se cidade == Belo Horizonte ou chave nao esta no cache: NAO MEXE.

Aceita 1+ URLs de Sheets E/OU arquivos CSV/XLSX locais (path).

Uso:
  python scripts/purgar_fora_bh.py <url_ou_caminho> [<url_ou_caminho> ...]
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.geocode import (
    _gerar_variacoes, _chave_canonica, carregar_cache, salvar_cache,
    geocodificar, _extrair_bairro_cidade,
)
from motor.io import baixar_sheet_csv, carregar_entregas_planilha


def _ler_enderecos(origem: str) -> list[str]:
    """Aceita URL Sheets, CSV ou XLSX. Retorna lista de enderecos brutos."""
    if origem.startswith("http"):
        txt = baixar_sheet_csv(origem)
        brutos = carregar_entregas_planilha(txt)
        return [b["endereco"] for b in brutos if b.get("endereco")]
    ext = os.path.splitext(origem)[1].lower()
    if ext in (".csv", ".tsv", ".txt"):
        with open(origem, encoding="utf-8-sig") as f:
            leitor = csv.reader(f)
            linhas = list(leitor)
        if not linhas:
            return []
        cab = [c.lower().strip() for c in linhas[0]]
        idx_end = None
        for nome in ("endereco", "endereço", "address"):
            if nome in cab:
                idx_end = cab.index(nome)
                break
        if idx_end is None:
            idx_end = 0
        return [l[idx_end].strip() for l in linhas[1:]
                if idx_end < len(l) and l[idx_end].strip()]
    if ext in (".xlsx", ".xls"):
        import pandas as pd
        df = pd.read_excel(origem)
        col = next((c for c in df.columns
                    if str(c).lower().strip() in ("endereco", "endereço", "address")),
                   df.columns[0])
        return [str(v).strip() for v in df[col]
                if str(v).strip() and str(v).strip().lower() != "nan"]
    raise ValueError(f"origem nao suportada: {origem}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("origens", nargs="+", help="URL Sheets ou caminho CSV/XLSX")
    ap.add_argument("--dry-run", action="store_true",
                    help="So mostra o que faria, nao mexe no cache")
    args = ap.parse_args()

    cache = carregar_cache()
    print(f"cache atual: {len(cache)} entradas\n")

    todos_enderecos: list[str] = []
    for origem in args.origens:
        print(f"Lendo {origem}...")
        try:
            ends = _ler_enderecos(origem)
            print(f"  {len(ends)} enderecos")
            todos_enderecos.extend(ends)
        except Exception as e:
            print(f"  ERRO: {e}")

    # Dedup
    todos_unicos = list(dict.fromkeys(todos_enderecos))
    print(f"\nTotal unicos: {len(todos_unicos)}\n")

    # Classifica: fora-BH no cache, fora-BH sem cache, BH
    fora_bh_no_cache = []
    fora_bh_novo = []
    bh = 0
    for end in todos_unicos:
        _, cidade = _extrair_bairro_cidade(end)
        chave = _chave_canonica(end)
        no_cache = chave in cache
        if cidade != "Belo Horizonte":
            if no_cache:
                fora_bh_no_cache.append((end, cidade, chave))
            else:
                fora_bh_novo.append((end, cidade))
        else:
            bh += 1

    print(f"=== Resumo ===")
    print(f"  BH (mantem):                  {bh}")
    print(f"  Fora-BH ja no cache (PURGA):  {len(fora_bh_no_cache)}")
    print(f"  Fora-BH novo (sera geocodif): {len(fora_bh_novo)}")
    print()

    if fora_bh_no_cache:
        print(f"=== Amostra das 10 primeiras a purgar+regeocodificar ===")
        for end, cid, chv in fora_bh_no_cache[:10]:
            coord_atual = cache.get(chv)
            print(f"  cidade={cid:18}  coord_atual={coord_atual}")
            print(f"    end: {end[:80]}")
        print()

    if args.dry_run:
        print("[DRY-RUN] nada foi alterado. Use sem --dry-run pra aplicar.")
        return

    if not fora_bh_no_cache:
        print("Nada a fazer.")
        return

    # Purga + re-geocodifica
    print(f"\n=== Purgando + re-geocodificando {len(fora_bh_no_cache)} ===")
    print(f"  Tempo estimado: {len(fora_bh_no_cache) * 2.5 / 60:.0f} min "
          f"(Nominatim 1.5s/req)")
    t0 = time.time()
    n_ok = 0
    n_fail = 0
    for i, (end, cid, chv) in enumerate(fora_bh_no_cache, start=1):
        # Remove a entrada errada
        cache.pop(chv, None)
        # Re-geocodifica (cache hit nao vai acontecer porque acabou de
        # ser removido; geocodificar() salva de novo no cache).
        try:
            coord = geocodificar(end, cache=cache)
            if coord:
                n_ok += 1
            else:
                n_fail += 1
        except Exception as e:
            print(f"  erro em {end[:60]}: {e}")
            n_fail += 1
        if i % 10 == 0:
            salvar_cache(cache)
            elapsed = time.time() - t0
            taxa = i / elapsed if elapsed > 0 else 0
            restante = (len(fora_bh_no_cache) - i) / taxa if taxa > 0 else 0
            print(f"  [{i}/{len(fora_bh_no_cache)}]  "
                  f"OK={n_ok}  FAIL={n_fail}  ETA={restante/60:.0f}min",
                  flush=True)
    salvar_cache(cache)
    print(f"\nConcluido em {(time.time()-t0)/60:.1f} min")
    print(f"  {n_ok} re-geocodificados")
    print(f"  {n_fail} falhas (provavelmente endereco invalido)")


if __name__ == "__main__":
    main()
