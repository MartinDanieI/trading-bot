"""Backtest de la estrategia actual con datos históricos reales.

Repite hora por hora lo que habría hecho el bot en vivo (mismo análisis, mismo
umbral, mismo anti-spam de 4h, oro solo con mercado abierto) y mide qué tocó
primero cada señal: SL, TP1 o TP2 (reglas en functions/scanner/performance.py).

Uso:
    python backtest.py                 # 58 días, BTC y Oro
    python backtest.py --days 30 --asset BTC/USD
    python backtest.py --threshold 80  # probar otro umbral

Guarda el detalle de cada operación en out/backtest_<fecha>.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "functions"))

import pandas as pd  # noqa: E402

from scanner.data_fetcher import MarketData, fetch_history, is_gold_market_open  # noqa: E402
from scanner.performance import evaluate_signal, summarize  # noqa: E402
from scanner.strategy import analyze  # noqa: E402

WINDOW = 100  # velas que ve el bot en vivo
COOLDOWN = timedelta(hours=4)


def backtest_asset(asset: str, days: int, threshold: float) -> list:
    print(f"Descargando {asset} ({days} días)…", flush=True)
    h1 = fetch_history(asset, "1h", days + 6)  # +6 días de calentamiento para los indicadores
    m15 = fetch_history(asset, "15m", days)
    start = m15.index[0] + timedelta(hours=26)  # el 15m necesita su propia ventana de 100 velas
    m15_close = m15.index + timedelta(minutes=15)

    trades, last_sent = [], {}
    for i in range(WINDOW, len(h1)):
        close_time = h1.index[i] + timedelta(hours=1)  # momento en que el bot "corre"
        if close_time < start:
            continue
        if asset == "XAU/USD" and not is_gold_market_open(close_time.to_pydatetime()):
            continue
        m15_slice = m15[m15_close <= close_time].tail(WINDOW)
        if len(m15_slice) < 60:
            continue
        try:
            a = analyze(MarketData(asset, "historial", h1.iloc[i - WINDOW + 1:i + 1], m15_slice),
                        threshold=threshold)
        except ValueError:
            continue
        if a.direction == "NEUTRAL":
            continue
        prev = last_sent.get(a.direction)
        if prev is not None and close_time - prev < COOLDOWN:
            continue
        last_sent[a.direction] = close_time
        lv = a.levels
        t = evaluate_signal(m15, asset, a.direction, close_time.to_pydatetime(),
                            lv.entry, lv.stop_loss, lv.tp1, lv.tp2)
        t.setup = a.setup  # type: ignore[attr-defined]
        t.score = a.score  # type: ignore[attr-defined]
        trades.append(t)
    return trades


def print_summary(title: str, trades: list) -> None:
    s = summarize(trades)
    acierto = "—" if s["acierto_pct"] is None else f"{s['acierto_pct']:.0f}%"
    print(f"\n{title}")
    print(f"  Señales: {s['senales']}  ·  ganadas {s['ganadas']}  ·  perdidas {s['perdidas']}"
          f"  ·  expiradas {s['expiradas']}  ·  abiertas {s['abiertas']}")
    print(f"  Acierto (TP1 antes que SL): {acierto}  ·  llegaron a TP2: {s['llegaron_tp2']}")
    avg = "—" if s["r_promedio"] is None else f"{s['r_promedio']:+.2f} R"
    print(f"  Resultado total: {s['total_r']:+.1f} R  ·  promedio por señal: {avg}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Backtest de la estrategia del escáner")
    p.add_argument("--days", type=int, default=58)
    p.add_argument("--threshold", type=float, default=70)
    p.add_argument("--asset", choices=["BTC/USD", "XAU/USD"], action="append")
    args = p.parse_args()

    all_trades = []
    for asset in args.asset or ["BTC/USD", "XAU/USD"]:
        trades = backtest_asset(asset, args.days, args.threshold)
        print_summary(f"=== {asset} ===", trades)
        for setup in sorted({t.setup for t in trades}):
            print_summary(f"  · {setup}", [t for t in trades if t.setup == setup])
        all_trades += trades

    print_summary(f"=== TOTAL ({args.days} días, umbral {args.threshold:.0f}) ===", all_trades)

    if all_trades:
        df = pd.DataFrame([{**t.to_dict(), "setup": t.setup, "score": t.score} for t in all_trades])
        df["semana"] = pd.to_datetime(df["sent_at"]).dt.strftime("%G-S%V")
        print("\nPor semana:")
        for week, g in df.groupby("semana"):
            dec = g[~g["outcome"].isin(["expirada", "abierta"])]
            pct = f"{100 * dec['outcome'].str.startswith('ganada').mean():.0f}%" if len(dec) else "—"
            print(f"  {week}: {len(g):3d} señales · acierto {pct:>4} · {g['r'].sum():+6.1f} R")
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        path = out / f"backtest_{pd.Timestamp.now():%Y%m%d_%H%M}.csv"
        df.to_csv(path, index=False)
        print(f"\nDetalle guardado en {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
