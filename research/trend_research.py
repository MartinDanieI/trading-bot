"""Investigación: seguimiento de tendencia en velas diarias con salida móvil.

Reglas clásicas "Turtle" (publicadas en los 80, NO ajustadas a estos datos):
    Entrada: cierre por encima del máximo de las N velas anteriores (o debajo del mínimo).
    Stop inicial: 2 ATR(20) desde la entrada.
    Salida: cierre por debajo del mínimo de las M velas anteriores (o encima del máximo en cortos).
    Una posición a la vez por activo. Costos de comisión + spread incluidos.

Se reportan los resultados en R por año y en dos mitades: si solo funciona en
una mitad, no es confiable.

Uso: python research/trend_research.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
COSTS = {"BTC": 0.0006, "ORO": 0.0002}
FILES = {"BTC": "BTC-USD_1d.csv", "ORO": "GC_F_1d.csv"}

VARIANTS = [
    # nombre, N entrada, M salida, filtro SMA200, permite cortos
    ("Turtle 20/10 · solo compras", 20, 10, False, False),
    ("Turtle 20/10 · compras y ventas", 20, 10, False, True),
    ("Turtle 55/20 · solo compras", 55, 20, False, False),
    ("Turtle 55/20 · compras y ventas", 55, 20, False, True),
    ("Turtle 20/10 + SMA200 · ambos", 20, 10, True, True),
    ("Turtle 55/20 + SMA200 · ambos", 55, 20, True, True),
]


def load(asset: str) -> pd.DataFrame:
    d = pd.read_csv(ROOT / FILES[asset], index_col=0, parse_dates=True)
    d = d[d["close"] > 0].dropna().iloc[:-1]  # la última vela diaria puede estar incompleta
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 20, adjust=False).mean()
    d["sma200"] = d["close"].rolling(200).mean()
    return d


def simulate(d: pd.DataFrame, asset: str, n: int, m: int, sma: bool, shorts: bool) -> pd.DataFrame:
    hi_n, lo_n = d["high"].rolling(n).max().shift(1), d["low"].rolling(n).min().shift(1)
    hi_m, lo_m = d["high"].rolling(m).max().shift(1), d["low"].rolling(m).min().shift(1)
    o, h, l, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
    trades, pos = [], None
    start = 200 if sma else max(n, 21)
    for i in range(start, len(d)):
        if pos:
            s = pos["sign"]
            exit_px = None
            if s > 0 and o[i] <= pos["stop"]:
                exit_px = o[i]
            elif s < 0 and o[i] >= pos["stop"]:
                exit_px = o[i]
            elif s > 0 and l[i] <= pos["stop"]:
                exit_px = pos["stop"]
            elif s < 0 and h[i] >= pos["stop"]:
                exit_px = pos["stop"]
            elif s > 0 and c[i] < lo_m.iloc[i]:
                exit_px = c[i]
            elif s < 0 and c[i] > hi_m.iloc[i]:
                exit_px = c[i]
            if exit_px is not None:
                r = s * (exit_px - pos["entry"]) / pos["risk"] - COSTS[asset] * pos["entry"] / pos["risk"]
                trades.append({"entrada": d.index[pos["i"]], "salida": d.index[i], "dir": s,
                               "r": r, "dias": i - pos["i"]})
                pos = None
            continue
        long_ok = c[i] > hi_n.iloc[i] and (not sma or c[i] > d["sma200"].iloc[i])
        short_ok = shorts and c[i] < lo_n.iloc[i] and (not sma or c[i] < d["sma200"].iloc[i])
        if long_ok or short_ok:
            s = 1 if long_ok else -1
            risk = 2 * d["atr"].iloc[i]
            pos = {"i": i, "sign": s, "entry": c[i], "risk": risk, "stop": c[i] - s * risk}
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"ops": 0, "acierto": np.nan, "total": 0.0, "prom": np.nan, "dd": 0.0}
    eq = t["r"].cumsum()
    return {"ops": len(t), "acierto": 100 * (t["r"] > 0).mean(), "total": t["r"].sum(),
            "prom": t["r"].mean(), "dd": (eq.cummax() - eq).max()}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    data = {a: load(a) for a in FILES}
    rows = []
    for name, n, m, sma, shorts in VARIANTS:
        for asset, d in data.items():
            t = simulate(d, asset, n, m, sma, shorts)
            mid = d.index[len(d) // 2]
            s, s1, s2 = stats(t), stats(t[t["entrada"] < mid]), stats(t[t["entrada"] >= mid])
            years = t.groupby(t["entrada"].dt.year)["r"].sum() if not t.empty else pd.Series(dtype=float)
            rows.append({"variante": name, "activo": asset, **s,
                         "1.ª mitad": s1["total"], "2.ª mitad": s2["total"],
                         "años +": int((years > 0).sum()), "años": len(years),
                         "dias_prom": t["dias"].mean() if not t.empty else np.nan,
                         "ops/año": len(t) / max(len(years), 1)})
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(out.round(2).to_string(index=False))
    out.to_csv(ROOT / "trend_results.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
