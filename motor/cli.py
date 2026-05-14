"""
Entry point do motor de roteirização.

Uso:
    python -m motor.cli dados/exemplo_entregas.csv
    python -m motor.cli dados/exemplo_entregas.csv --config dados/config.json
    python -m motor.cli dados/exemplo_entregas.csv --export saida/rotas.csv
    python -m motor.cli dados/exemplo_entregas.csv --min 10 --max 18

Fluxo:
    CSV de entregas + config (CD + entregadores)
        → matriz OSRM
        → CVRP (OR-Tools)
        → rotas ordenadas (terminal + CSV opcional)
"""

import argparse
import logging
import os
import sys

from motor.io import carregar_entregas, carregar_config, imprimir_rotas, exportar_csv
from motor.roteirizar import roteirizar


def main(argv=None):
    parser = argparse.ArgumentParser(description="Motor de roteirização de entregas")
    parser.add_argument("entregas_csv", help="CSV de entregas (id, lat, lng, ...)")
    parser.add_argument("--config", default="dados/config.json",
                        help="JSON com CD + entregadores (default: dados/config.json)")
    parser.add_argument("--export", help="exporta as rotas pra este CSV")
    parser.add_argument("--min", type=int, default=10, help="mín. de paradas por rota")
    parser.add_argument("--max", type=int, default=18, help="máx. de paradas por rota")
    parser.add_argument("--tempo", type=int, default=30,
                        help="tempo limite do solver em segundos")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not os.path.exists(args.entregas_csv):
        print(f"Erro: arquivo de entregas não encontrado: {args.entregas_csv}")
        return 1
    if not os.path.exists(args.config):
        print(f"Erro: config não encontrada: {args.config}")
        return 1

    entregas = carregar_entregas(args.entregas_csv)
    cd, entregadores = carregar_config(args.config)
    print(f"Carregado: {len(entregas)} entregas, {len(entregadores)} entregadores, CD={cd.nome}")

    if not entregas:
        print("Nenhuma entrega válida no CSV.")
        return 1

    try:
        rotas = roteirizar(
            entregas, entregadores, cd,
            min_paradas=args.min, max_paradas=args.max,
            tempo_limite_s=args.tempo,
        )
    except Exception as e:
        print(f"Erro na roteirização: {e}")
        return 1

    imprimir_rotas(rotas)
    if args.export:
        os.makedirs(os.path.dirname(args.export) or ".", exist_ok=True)
        exportar_csv(rotas, args.export)
        print(f"✓ Exportado para {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
