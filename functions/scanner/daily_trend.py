"""Tendencia diaria (reglas "Turtle") con salida móvil: la estrategia principal del bot.

Validada con 12 años de BTC y 26 de Oro (research/trend_research.py), con
comisiones incluidas y sin ajustar parámetros a los datos:

    Entrada: un día cierra por encima del máximo de los N días anteriores
             (o por debajo del mínimo, si se permiten ventas).
    Stop inicial: 2 ATR(20) desde la entrada. Se deja como orden stop en el broker.
    Salida: un día cierra por debajo del mínimo de los M días anteriores
            (encima del máximo en ventas). Sin TP fijo: la ganancia corre.
    Una posición a la vez por activo.

    BTC: N=20, M=10, compras y ventas, solo a favor de la SMA 200.
    Oro: N=55, M=20, solo compras.

Se procesa una vez por vela diaria cerrada. Esta misma función sirve para
reconstruir el historial (backtest) y para el día a día en vivo.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data_fetcher import COLUMNS, DataUnavailableError, fetch_history, with_retries
from .performance import COSTS


@dataclass(frozen=True)
class TrendParams:
    entry_days: int
    exit_days: int
    sma_filter: bool
    shorts: bool


PARAMS = {
    "BTC/USD": TrendParams(20, 10, True, True),
    "XAU/USD": TrendParams(55, 20, False, False),
}
ATR_DAYS = 20
STOP_ATR = 2.0


def fetch_daily(asset: str) -> tuple[pd.DataFrame, str]:
    """Velas diarias cerradas (>= 260). BTC: Kraken y luego Yahoo. Oro: Yahoo."""
    if asset == "BTC/USD":
        try:
            import ccxt

            def _call() -> pd.DataFrame:
                rows = ccxt.kraken({"enableRateLimit": True, "timeout": 20000}).fetch_ohlcv(
                    "BTC/USD", timeframe="1d", limit=720)
                df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
                df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
                return df.astype(float)

            df = with_retries(_call, "kraken BTC/USD 1d", attempts=2)
            return df[df.index + pd.Timedelta(days=1) <= pd.Timestamp.now(tz="UTC")], "Kraken BTC/USD"
        except DataUnavailableError:
            pass
    df = fetch_history(asset, "1d", 400)
    return df, ("Yahoo BTC-USD" if asset == "BTC/USD" else "Yahoo GC=F (futuros oro)")


def indicators(d: pd.DataFrame, p: TrendParams) -> pd.DataFrame:
    d = d.copy()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / ATR_DAYS, adjust=False).mean()
    d["sma200"] = d["close"].rolling(200).mean()
    d["hi_n"] = d["high"].rolling(p.entry_days).max().shift(1)
    d["lo_n"] = d["low"].rolling(p.entry_days).min().shift(1)
    d["hi_m"] = d["high"].rolling(p.exit_days).max().shift(1)
    d["lo_m"] = d["low"].rolling(p.exit_days).min().shift(1)
    return d


def _next_exit_line(d: pd.DataFrame, i: int, p: TrendParams, sign: int) -> float:
    """Nivel de salida para el próximo cierre: mínimo (máximo) de los últimos M días incluyendo hoy."""
    window = d.iloc[max(0, i - p.exit_days + 1):i + 1]
    return float(window["low"].min() if sign > 0 else window["high"].max())


def step(d: pd.DataFrame, i: int, asset: str, pos: dict | None) -> tuple[dict | None, dict | None]:
    """Procesa la vela diaria i. Devuelve (posición después de la vela, evento).

    Eventos: {"type": "entry"|"exit"|"update", ...}. Mismo orden de reglas que el backtest:
    primero el hueco de apertura contra el stop, luego el stop dentro del día y por último
    el cierre contra el canal de salida.
    """
    p = PARAMS[asset]
    row = d.iloc[i]
    day = d.index[i] + pd.Timedelta(days=1)  # momento en que cierra la vela diaria
    if pos:
        s = pos["sign"]
        exit_px, reason = None, None
        if (s > 0 and row["open"] <= pos["stop"]) or (s < 0 and row["open"] >= pos["stop"]):
            exit_px, reason = row["open"], "abrió más allá del stop"
        elif (s > 0 and row["low"] <= pos["stop"]) or (s < 0 and row["high"] >= pos["stop"]):
            exit_px, reason = pos["stop"], "tocó el stop durante el día (si tenías la orden stop puesta, ya se cerró)"
        elif s > 0 and row["close"] < row["lo_m"]:
            exit_px, reason = row["close"], f"cerró debajo del mínimo de {p.exit_days} días ({row['lo_m']:,.2f})"
        elif s < 0 and row["close"] > row["hi_m"]:
            exit_px, reason = row["close"], f"cerró encima del máximo de {p.exit_days} días ({row['hi_m']:,.2f})"
        if exit_px is not None:
            r = s * (exit_px - pos["entry"]) / pos["risk"] - COSTS[asset] * pos["entry"] / pos["risk"]
            return None, {"type": "exit", "asset": asset, "pos": pos, "price": float(exit_px),
                          "reason": reason, "r": float(r), "day": day.isoformat()}
        return pos, {"type": "update", "asset": asset, "pos": pos, "price": float(row["close"]),
                     "r": float(s * (row["close"] - pos["entry"]) / pos["risk"]),
                     "exit_line": _next_exit_line(d, i, p, s), "day": day.isoformat()}

    if np.isnan(row["hi_n"]) or np.isnan(row["atr"]) or (p.sma_filter and np.isnan(row["sma200"])):
        return None, None
    long_ok = row["close"] > row["hi_n"] and (not p.sma_filter or row["close"] > row["sma200"])
    short_ok = (p.shorts and row["close"] < row["lo_n"]
                and (not p.sma_filter or row["close"] < row["sma200"]))
    if not (long_ok or short_ok):
        return None, None
    s = 1 if long_ok else -1
    risk = STOP_ATR * float(row["atr"])
    entry = float(row["close"])
    level = float(row["hi_n"] if s > 0 else row["lo_n"])
    exit_line = _next_exit_line(d, i, p, s)
    new = {"asset": asset, "sign": s, "direction": "LONG" if s > 0 else "SHORT", "entry": entry,
           "risk": risk, "stop": entry - s * risk, "opened": day.isoformat()}
    return new, {"type": "entry", "asset": asset, "pos": new, "price": entry, "level": level,
                 "sma200": None if np.isnan(row["sma200"]) else float(row["sma200"]),
                 "exit_line": exit_line, "day": day.isoformat()}


def replay(d: pd.DataFrame, asset: str) -> tuple[list[dict], dict | None]:
    """Recorre todo el historial: devuelve (operaciones cerradas, posición abierta al final)."""
    d = indicators(d, PARAMS[asset])
    pos, trades = None, []
    for i in range(len(d)):
        prev_pos = pos
        pos, ev = step(d, i, asset, pos)
        if ev and ev["type"] == "exit":
            trades.append(ev)
        if prev_pos is not None and pos is None:
            continue  # tras una salida no se entra el mismo día (igual que el backtest)
    return trades, pos
