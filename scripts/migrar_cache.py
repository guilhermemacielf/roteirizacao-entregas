"""Migra o cache de geocode pra usar chaves canonicas robustas.

Le o cache atual (chaves brutas tipo 'rua x 123 lourdes mg'), recomputa
cada chave usando _chave_canonica, e regrava. Multiplas entradas que
geram a mesma chave nova colapsam — fica a que tem coordenadas validas
(prefere coord != None).

Modo dry-run por padrao — mostra o que seria feito sem alterar arquivo.
Use --aplicar pra escrever.

Uso:
  python scripts/migrar_cache.py             # dry-run
  python scripts/migrar_cache.py --aplicar   # aplica
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.geocode import _chave_canonica, CACHE_PATH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true",
                    help="Aplica alteracoes (default: dry-run)")
    ap.add_argument("--mostrar-grupos", type=int, default=5,
                    help="Mostra N maiores grupos de colapso")
    args = ap.parse_args()

    with open(CACHE_PATH, encoding="utf-8") as f:
        cache_velho = json.load(f)

    print(f"cache atual: {len(cache_velho)} entradas")

    # Agrupa por nova chave
    grupos: dict[str, list[tuple[str, list | None]]] = defaultdict(list)
    for chave_velha, valor in cache_velho.items():
        nova = _chave_canonica(chave_velha)
        grupos[nova].append((chave_velha, valor))

    print(f"cache novo:  {len(grupos)} entradas unicas")
    print(f"reducao: {len(cache_velho) - len(grupos)} entradas colapsadas "
          f"({(len(cache_velho)-len(grupos))*100/len(cache_velho):.1f}%)")

    # Maiores grupos de colapso
    grupos_grandes = sorted(
        [(k, v) for k, v in grupos.items() if len(v) > 1],
        key=lambda t: -len(t[1])
    )
    print(f"\n{len(grupos_grandes)} chaves novas tem multiplas entradas velhas.")
    print(f"top {args.mostrar_grupos}:")
    for chave_nova, entradas in grupos_grandes[:args.mostrar_grupos]:
        print(f"\n  CHAVE: {chave_nova!r}  ({len(entradas)} entradas velhas)")
        for chave_velha, valor in entradas[:6]:
            tem_coord = "OK" if valor else "NULL"
            print(f"    [{tem_coord}] {chave_velha!r}")
        if len(entradas) > 6:
            print(f"    ... mais {len(entradas) - 6}")

    # Constroi novo cache: prefere entradas com coord valida
    cache_novo = {}
    for chave_nova, entradas in grupos.items():
        # Pega a primeira com coord valida; se nenhuma valida, pega None
        valor_escolhido = None
        for _, v in entradas:
            if v:
                valor_escolhido = v
                break
        cache_novo[chave_nova] = valor_escolhido

    n_ok = sum(1 for v in cache_novo.values() if v)
    n_null = sum(1 for v in cache_novo.values() if not v)
    print(f"\ncache novo: {n_ok} com coord, {n_null} None")

    if not args.aplicar:
        print(f"\n[DRY-RUN] nada foi escrito. Use --aplicar pra gravar.")
        return

    # Backup do cache antigo
    bkp = CACHE_PATH + ".bkp"
    print(f"\nfazendo backup em {bkp}")
    with open(CACHE_PATH, encoding="utf-8") as f:
        with open(bkp, "w", encoding="utf-8") as g:
            g.write(f.read())

    # Escreve novo
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache_novo, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)
    print(f"cache atualizado: {CACHE_PATH}")


if __name__ == "__main__":
    main()
