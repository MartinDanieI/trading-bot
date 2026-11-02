"""Genera worker/src/catalog.json (lo que muestra el menú de Telegram) a partir del
catálogo de estrategias y del último backtest (worker/stats.json).

Uso: python backtest.py --strategy catalogo --save-stats worker/stats.json
     python scripts/build_worker_catalog.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "functions"))

from scanner.strategies import CATALOG  # noqa: E402


def stats_line(sid: str, s: dict) -> str:
    pct = "—" if s["acierto_pct"] is None else f"{s['acierto_pct']:.0f} %"
    if sid == "tendencia_diaria":
        freq = "~8 señales al año entre BTC y Oro"
        period = "de 12 años (BTC) y 26 (Oro)"
    else:
        per_week = s["por_dia"] * 7
        freq = (f"~{s['por_dia']:.0f} señales al día" if s["por_dia"] >= 1.5
                else f"~{per_week:.0f} señales a la semana" if per_week >= 1.5
                else "~1 señal a la semana" if per_week >= 0.5 else "muy pocas señales")
        period = f"de {s['periodo_dias']} días"
    verdict = "⭐ ganó" if s["total_r"] > 0 else "⚠️ perdió"
    return f"Backtest {period}: acierto {pct} · {s['total_r']:+.0f} R · {freq} · {verdict}"


sys.stdout.reconfigure(encoding="utf-8")
stats = json.loads((ROOT / "worker" / "stats.json").read_text(encoding="utf-8"))["strategies"]
out = [{"id": i.id, "name": i.name, "emoji": i.emoji, "timeframe": i.timeframe,
        "description": i.description, "stats": stats_line(i.id, stats[i.id]["TOTAL"])}
       for i in CATALOG.values()]
(ROOT / "worker" / "src" / "catalog.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
for o in out:
    print(f"{o['emoji']} {o['name']}: {o['stats']}")
