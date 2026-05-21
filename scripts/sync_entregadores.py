"""Sincroniza entregadores a partir de uma URL do Google Sheets.

Uso:
  python scripts/sync_entregadores.py <URL>
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.entregadores_sheet import sincronizar_entregadores

url = sys.argv[1] if len(sys.argv) > 1 else None
if not url:
    print("uso: sync_entregadores.py <url>", file=sys.stderr)
    sys.exit(1)

r = sincronizar_entregadores(url)
print(json.dumps(r, ensure_ascii=False, indent=2))
