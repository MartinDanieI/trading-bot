"""Estrategias basadas en la "caja" del día anterior (máximo PDH / mínimo PDL).

Salen de dos videos que el usuario aportó; las reglas se confirmaron con él:

BOX THEORY (velas de 1h)
    - Caja = máximo y mínimo de ayer, con línea al 50 %.
    - Mitad de abajo: solo compras. Mitad de arriba: solo ventas. Cerca del 50 %: nada.
    - Entrada en el extremo (30 % de abajo / de arriba) con vela de rechazo:
      mecha >= 2 x cuerpo y cierre más allá del máximo (o mínimo) de la vela anterior.
    - SL detrás de la mecha. TP1 = 50 % de la caja. TP2 = 90 % hacia el extremo opuesto.

TRAMPA / RUPTURA DEL MÁXIMO (o mínimo) DE AYER (velas de 15m)
    - Trampa: la vela pasa el PDH solo con mecha y cierra otra vez debajo -> VENTA.
      SL arriba de la mecha. TP1 = 50 % de la caja. TP2 = mínimo de ayer.
      (Espejo en el PDL -> COMPRA.)
    - Ruptura real: vela que cierra con cuerpo arriba del PDH y, en las siguientes
      2 horas, una vela vuelve a tocar la línea y cierra arriba -> COMPRA.
      SL debajo de la línea. TP1 = 1.5 R, TP2 = 2.5 R. (Espejo en el PDL -> VENTA.)

"El día": Oro desde las 17:00 de Nueva York (como TradingView/brokers);
BTC desde medianoche UTC. Máximo una señal por tipo de setup y por día.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from .strategy import atr

EXTREME = 0.30  # fracción de la caja considerada "extremo"
WICK_BODY = 2.0  # mecha mínima respecto al cuerpo
MIN_RISK_ATR = 0.25  # el SL nunca queda a menos de 0.25 ATR de la entrada
RETEST_BARS = 8  # velas de 15m para esperar el regreso a la línea (2 horas)


@dataclass
class BoxSignal:
    time: pd.Timestamp  # cierre de la vela que da la señal (UTC)
    asset: str
    strategy: str
    direction: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    pdh: float
    pdl: float
    note: str


def day_key(index: pd.DatetimeIndex, asset: str) -> pd.Index:
    """Fecha de "sesión" de cada vela."""
    if asset.startswith("XAU"):
        # 17:00 NY pasa a ser 00:00 del día siguiente
        return (index.tz_convert("America/New_York") + pd.Timedelta(hours=7)).date
    return index.tz_convert("UTC").date


def previous_day_box(h1: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Para cada vela de 1h: PDH, PDL y 50 % de la sesión anterior disponible."""
    keys = pd.Index(day_key(h1.index, asset))
    daily = h1.groupby(keys).agg(high=("high", "max"), low=("low", "min"))
    prev = daily.shift(1)  # sesión anterior (para el oro, el viernes antes del lunes)
    out = pd.DataFrame({"pdh": prev["high"].reindex(keys).values,
                        "pdl": prev["low"].reindex(keys).values,
                        "day": keys}, index=h1.index)
    out["mid"] = (out["pdh"] + out["pdl"]) / 2
    return out


def _levels(entry: float, stop: float, min_risk: float, direction: str) -> tuple[float, float]:
    """Asegura un riesgo mínimo; devuelve (stop, riesgo)."""
    if direction == "LONG":
        stop = min(stop, entry - min_risk)
        return stop, entry - stop
    stop = max(stop, entry + min_risk)
    return stop, stop - entry


def box_signals(h1: pd.DataFrame, asset: str) -> list[BoxSignal]:
    box = previous_day_box(h1, asset)
    a = atr(h1, 14)
    signals: list[BoxSignal] = []
    used: set = set()
    for i in range(1, len(h1)):
        c, p, b = h1.iloc[i], h1.iloc[i - 1], box.iloc[i]
        if pd.isna(b["pdh"]) or pd.isna(a.iloc[i]) or b["pdh"] <= b["pdl"]:
            continue
        rng = b["pdh"] - b["pdl"]
        body = abs(c["close"] - c["open"])
        lower = min(c["open"], c["close"]) - c["low"]
        upper = c["high"] - max(c["open"], c["close"])
        t = h1.index[i] + timedelta(hours=1)
        min_risk = MIN_RISK_ATR * a.iloc[i]

        long_ok = (c["low"] <= b["pdl"] + EXTREME * rng and c["close"] < b["mid"]
                   and lower >= WICK_BODY * max(body, 1e-9) and c["close"] > p["high"])
        short_ok = (c["high"] >= b["pdh"] - EXTREME * rng and c["close"] > b["mid"]
                    and upper >= WICK_BODY * max(body, 1e-9) and c["close"] < p["low"])
        for direction, ok in (("LONG", long_ok), ("SHORT", short_ok)):
            key = (b["day"], direction)
            if not ok or key in used:
                continue
            used.add(key)
            entry = float(c["close"])
            if direction == "LONG":
                stop, _ = _levels(entry, float(c["low"]), min_risk, "LONG")
                tp1, tp2 = b["mid"], b["pdl"] + 0.9 * rng
            else:
                stop, _ = _levels(entry, float(c["high"]), min_risk, "SHORT")
                tp1, tp2 = b["mid"], b["pdh"] - 0.9 * rng
            signals.append(BoxSignal(t, asset, "Box Theory", direction, entry, stop,
                                     float(tp1), float(tp2), b["pdh"], b["pdl"],
                                     "rechazo con mecha larga en el extremo de la caja"))
    return signals


def sweep_signals(m15: pd.DataFrame, h1: pd.DataFrame, asset: str) -> list[BoxSignal]:
    """Trampas y rupturas del máximo/mínimo de ayer en velas de 15m."""
    hbox = previous_day_box(h1, asset)
    # caja de cada vela de 15m = la de su vela de 1h
    box = hbox.reindex(m15.index.floor("h"))
    box.index = m15.index
    a = atr(m15, 14)
    signals: list[BoxSignal] = []
    used: set = set()
    pending: list[dict] = []  # rupturas esperando el regreso a la línea

    for i in range(len(m15)):
        c, b = m15.iloc[i], box.iloc[i]
        if pd.isna(b["pdh"]) or pd.isna(a.iloc[i]) or b["pdh"] <= b["pdl"]:
            continue
        t = m15.index[i] + timedelta(minutes=15)
        min_risk = MIN_RISK_ATR * a.iloc[i]
        pdh, pdl, mid, day = b["pdh"], b["pdl"], b["mid"], b["day"]

        def emit(kind: str, direction: str, entry: float, stop: float, tp1: float | None,
                 tp2: float | None, note: str) -> None:
            key = (day, kind)
            if key in used:
                return
            used.add(key)
            stop, risk = _levels(entry, stop, min_risk, direction)
            sign = 1 if direction == "LONG" else -1
            tp1 = entry + sign * 1.5 * risk if tp1 is None else tp1
            tp2 = entry + sign * 2.5 * risk if tp2 is None else tp2
            if sign * (tp2 - tp1) < 0:  # TP2 siempre más lejos que TP1
                tp1, tp2 = tp2, tp1
            signals.append(BoxSignal(t, asset, kind, direction, entry, stop, float(tp1),
                                     float(tp2), pdh, pdl, note))

        # Rupturas pendientes: ¿el regreso a la línea aguanta?
        still = []
        for pnd in pending:
            if pnd["day"] != day or i - pnd["i"] > RETEST_BARS:
                continue
            if pnd["dir"] == "LONG" and c["low"] <= pnd["line"]:
                if c["close"] > pnd["line"]:
                    emit("Ruptura del máximo", "LONG", float(c["close"]),
                         min(float(c["low"]), pnd["line"]), None, None,
                         "cerró arriba del máximo de ayer y el regreso aguantó")
                continue  # tocó la línea: se resuelve (aguantó o falló)
            if pnd["dir"] == "SHORT" and c["high"] >= pnd["line"]:
                if c["close"] < pnd["line"]:
                    emit("Ruptura del mínimo", "SHORT", float(c["close"]),
                         max(float(c["high"]), pnd["line"]), None, None,
                         "cerró debajo del mínimo de ayer y el regreso aguantó")
                continue
            still.append(pnd)
        pending = still

        # Trampa en el máximo: mecha arriba, cierre otra vez debajo
        if c["high"] > pdh and c["close"] < pdh and c["open"] < pdh:
            emit("Trampa en el máximo", "SHORT", float(c["close"]), float(c["high"]),
                 mid if mid < c["close"] else None, pdl,
                 "pasó el máximo de ayer solo con mecha y cerró debajo")
        # Trampa en el mínimo (espejo)
        if c["low"] < pdl and c["close"] > pdl and c["open"] > pdl:
            emit("Trampa en el mínimo", "LONG", float(c["close"]), float(c["low"]),
                 mid if mid > c["close"] else None, pdh,
                 "pasó el mínimo de ayer solo con mecha y cerró arriba")
        # Ruptura con cuerpo: queda pendiente del regreso
        if c["open"] < pdh < c["close"]:
            pending.append({"dir": "LONG", "line": pdh, "i": i, "day": day})
        if c["open"] > pdl > c["close"]:
            pending.append({"dir": "SHORT", "line": pdl, "i": i, "day": day})
    return signals
