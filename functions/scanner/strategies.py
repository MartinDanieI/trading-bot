"""Catálogo de estrategias que cada persona puede elegir en Telegram.

Cada estrategia recorre velas cerradas y devuelve señales con hora de cierre de
la vela (UTC), entrada, SL, TP1 y TP2. La misma función sirve para el backtest
(se pasa todo el historial) y para el escaneo en vivo (se pasan las velas
recientes y solo interesa la señal de la última vela).

    confluencia  1h   Confluencia de EMAs, RSI, MACD y rango de 24h (la estrategia original).
    tendencia    4h   Ruptura del canal de 20 velas a favor de EMA 50 > EMA 200.
    reversion    1h   Regreso dentro de las Bandas de Bollinger cuando no hay tendencia (ADX < 20).
    sesion       15m  Ruptura del rango de la primera hora de Londres y de Nueva York.
    scalping     5m   Retroceso a la EMA 20 a favor de la tendencia, confirmado con VWAP y RSI 7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

import numpy as np
import pandas as pd

from .strategy import atr, ema, rsi


@dataclass
class Signal:
    time: pd.Timestamp  # cierre de la vela que da la señal (UTC)
    asset: str
    strategy: str  # id del catálogo
    direction: str  # LONG | SHORT
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    reasons: list[str] = field(default_factory=list)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)


@dataclass(frozen=True)
class StrategyInfo:
    id: str
    name: str
    emoji: str
    timeframe: str  # velas que analiza
    bars: int  # velas que necesita en vivo
    horizon: timedelta  # tiempo máximo para evaluar una señal (backtest y reporte)
    eval_tf: str  # velas con las que se evalúa si tocó SL o TP
    description: str


# --------------------------------------------------------------------------- #
# Indicadores adicionales
# --------------------------------------------------------------------------- #
def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    a = atr(df, period)
    alpha = 1 / period
    pdi = 100 * plus.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / a
    mdi = 100 * minus.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP que se reinicia cada día UTC. Sin volumen, usa el precio típico promedio."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].where(df["volume"] > 0, 1.0)
    day = df.index.tz_convert("UTC").date
    pv = (typical * vol).groupby(day).cumsum()
    return pv / vol.groupby(day).cumsum()


def _signal(t, asset, sid, direction, entry, stop, r1, r2, reasons, min_risk) -> Signal:
    """Arma la señal asegurando un riesgo mínimo; TP en múltiplos de R."""
    sign = 1 if direction == "LONG" else -1
    risk = max(sign * (entry - stop), min_risk)
    stop = entry - sign * risk
    return Signal(t, asset, sid, direction, float(entry), float(stop),
                  float(entry + sign * r1 * risk), float(entry + sign * r2 * risk), reasons)


def _cooldown(signals: list[Signal], bars: int, bar: timedelta) -> list[Signal]:
    """No repite la misma dirección hasta pasadas `bars` velas."""
    out, last = [], {}
    for s in signals:
        prev = last.get(s.direction)
        if prev is None or s.time - prev >= bars * bar:
            out.append(s)
            last[s.direction] = s.time
    return out


# --------------------------------------------------------------------------- #
# Estrategias
# --------------------------------------------------------------------------- #
def tendencia(c4h: pd.DataFrame, asset: str) -> list[Signal]:
    """4h: cierre que rompe el máximo (mínimo) de las 20 velas anteriores con EMA50 > EMA200
    (EMA50 < EMA200) y precio del lado correcto de la EMA 200. SL 2 ATR, TP 1.5 R y 3 R."""
    d = c4h
    e50, e200, a = ema(d["close"], 50), ema(d["close"], 200), atr(d, 14)
    hi20 = d["high"].rolling(20).max().shift(1)
    lo20 = d["low"].rolling(20).min().shift(1)
    long_ = (d["close"] > hi20) & (e50 > e200) & (d["close"] > e200)
    short = (d["close"] < lo20) & (e50 < e200) & (d["close"] < e200)
    bar = timedelta(hours=4)
    sigs = []
    for i in np.flatnonzero((long_ | short).to_numpy()):
        if np.isnan(a.iloc[i]):
            continue
        direction = "LONG" if long_.iloc[i] else "SHORT"
        sign = 1 if direction == "LONG" else -1
        entry = d["close"].iloc[i]
        level = hi20.iloc[i] if direction == "LONG" else lo20.iloc[i]
        sigs.append(_signal(d.index[i] + bar, asset, "tendencia", direction, entry,
                            entry - sign * 2 * a.iloc[i], 1.5, 3.0,
                            [f"rompe el {'máximo' if sign > 0 else 'mínimo'} de 20 velas de 4h ({level:,.2f})",
                             f"EMA50 {'>' if sign > 0 else '<'} EMA200 (tendencia {'alcista' if sign > 0 else 'bajista'})"],
                            a.iloc[i]))
    return _cooldown(sigs, 6, bar)


def reversion(c1h: pd.DataFrame, asset: str) -> list[Signal]:
    """1h, solo en rango (ADX < 20): la vela anterior cerró fuera de la Banda de Bollinger
    (20, 2) y esta vuelve a cerrar dentro, con RSI extremo. TP1 = media, TP2 = banda opuesta."""
    d = c1h
    mid = d["close"].rolling(20).mean()
    sd = d["close"].rolling(20).std(ddof=0)
    up, lo = mid + 2 * sd, mid - 2 * sd
    r, a, x = rsi(d["close"], 14), atr(d, 14), adx(d, 14)
    prev_close = d["close"].shift(1)
    long_ = (prev_close < lo.shift(1)) & (d["close"] > lo) & (r.shift(1) < 35) & (x < 20)
    short = (prev_close > up.shift(1)) & (d["close"] < up) & (r.shift(1) > 65) & (x < 20)
    bar = timedelta(hours=1)
    sigs = []
    for i in np.flatnonzero((long_ | short).to_numpy()):
        direction = "LONG" if long_.iloc[i] else "SHORT"
        entry = d["close"].iloc[i]
        sign = 1 if direction == "LONG" else -1
        swing = d["low"].iloc[i - 2:i + 1].min() if sign > 0 else d["high"].iloc[i - 2:i + 1].max()
        stop = swing - sign * 0.3 * a.iloc[i]
        risk = max(sign * (entry - stop), a.iloc[i])
        tp1, tp2 = mid.iloc[i], (up.iloc[i] if sign > 0 else lo.iloc[i])
        if sign * (tp1 - entry) <= 0.3 * risk:  # la media está demasiado cerca: no vale la pena
            continue
        sigs.append(Signal(d.index[i] + bar, asset, "reversion", direction, float(entry),
                           float(entry - sign * risk), float(tp1), float(tp2),
                           [f"volvió dentro de la Banda de Bollinger {'inferior' if sign > 0 else 'superior'}",
                            f"RSI venía de {r.iloc[i - 1]:.0f}", f"mercado en rango (ADX {x.iloc[i]:.0f})"]))
    return _cooldown(sigs, 4, bar)


SESSIONS = (("Londres", "Europe/London", 8, 0), ("Nueva York", "America/New_York", 9, 30))


def sesion(c15: pd.DataFrame, asset: str) -> list[Signal]:
    """15m: rango de la primera hora de Londres (08:00) y Nueva York (09:30), hora local.
    Primer cierre fuera del rango en las 3 horas siguientes -> entrada a favor.
    SL en la mitad del rango (mínimo 0.5 ATR de 15m), TP 1.5 R y 2.5 R."""
    d = c15
    a = atr(d, 14)
    bar = timedelta(minutes=15)
    sigs = []
    for name, tz, hh, mm in SESSIONS:
        local = d.index.tz_convert(tz)
        mins = local.hour * 60 + local.minute
        start = hh * 60 + mm
        in_range = (mins >= start) & (mins < start + 60)
        after = (mins >= start + 60) & (mins < start + 240)
        days = pd.Index(local.date)
        for day in np.unique(days[in_range]):
            r = d[in_range & (days == day)]
            if len(r) < 4:
                continue
            hi, lo = r["high"].max(), r["low"].min()
            mid = (hi + lo) / 2
            window = np.flatnonzero(after & (days == day))
            for i in window:
                c = d["close"].iloc[i]
                if c > hi or c < lo:
                    direction = "LONG" if c > hi else "SHORT"
                    sigs.append(_signal(d.index[i] + bar, asset, "sesion", direction, c, mid, 1.5, 2.5,
                                        [f"rompió el rango de la 1.ª hora de {name} ({lo:,.2f} – {hi:,.2f})"],
                                        0.5 * a.iloc[i]))
                    break  # solo la primera ruptura de cada sesión
    return sorted(sigs, key=lambda s: s.time)


def scalping(c5: pd.DataFrame, asset: str) -> list[Signal]:
    """5m: tendencia EMA20 > EMA50 (o <), la vela toca la EMA 20 y cierra a favor, del lado
    correcto del VWAP y con RSI(7) girando. SL detrás del mínimo de 3 velas (mín. 1 ATR),
    TP 1 R y 2 R. Señales separadas al menos 30 minutos."""
    d = c5
    e20, e50 = ema(d["close"], 20), ema(d["close"], 50)
    vw, r7, a = session_vwap(d), rsi(d["close"], 7), atr(d, 14)
    long_ = ((e20 > e50) & (d["low"] <= e20) & (d["close"] > e20) & (d["close"] > d["open"])
             & (d["close"] > vw) & (r7 > r7.shift(1)) & (r7 < 70))
    short = ((e20 < e50) & (d["high"] >= e20) & (d["close"] < e20) & (d["close"] < d["open"])
             & (d["close"] < vw) & (r7 < r7.shift(1)) & (r7 > 30))
    bar = timedelta(minutes=5)
    sigs = []
    for i in np.flatnonzero((long_ | short).to_numpy()):
        if i < 3 or np.isnan(a.iloc[i]):
            continue
        direction = "LONG" if long_.iloc[i] else "SHORT"
        sign = 1 if direction == "LONG" else -1
        swing = d["low"].iloc[i - 3:i + 1].min() if sign > 0 else d["high"].iloc[i - 3:i + 1].max()
        sigs.append(_signal(d.index[i] + bar, asset, "scalping", direction, d["close"].iloc[i], swing,
                            1.0, 2.0,
                            [f"retroceso a la EMA 20 con tendencia {'alcista' if sign > 0 else 'bajista'} en 5m",
                             f"precio {'sobre' if sign > 0 else 'bajo'} el VWAP del día", f"RSI(7) {r7.iloc[i]:.0f}"],
                            a.iloc[i]))
    return _cooldown(sigs, 6, bar)


CATALOG: dict[str, StrategyInfo] = {
    s.id: s for s in (
        StrategyInfo("confluencia", "Confluencia", "🧭", "1h", 100, timedelta(hours=48), "15m",
                     "Combina EMAs, RSI, MACD, volumen y el rango de 24 h en velas de 1 hora."),
        StrategyInfo("tendencia", "Tendencia", "📈", "4h", 260, timedelta(days=7), "15m",
                     "Sigue la tendencia grande: entra cuando el precio rompe el máximo o mínimo de "
                     "20 velas de 4 horas a favor de la EMA 200. Pocas señales, operaciones de días."),
        StrategyInfo("reversion", "Reversión a la media", "🔄", "1h", 120, timedelta(hours=48), "15m",
                     "Cuando el mercado está de lado, compra abajo y vende arriba de las Bandas de "
                     "Bollinger esperando que vuelva al promedio."),
        StrategyInfo("sesion", "Ruptura de sesión", "🌍", "15m", 120, timedelta(hours=24), "15m",
                     "Marca el rango de la primera hora de Londres y de Nueva York y entra cuando el "
                     "precio lo rompe. Máximo 2 señales al día por activo."),
        StrategyInfo("scalping", "Scalping", "⚡", "5m", 150, timedelta(hours=4), "5m",
                     "Operaciones rápidas de minutos en velas de 5 minutos. Muchas señales; hay que "
                     "estar pendiente y entrar enseguida."),
    )
}

DETECTORS: dict[str, Callable[[pd.DataFrame, str], list[Signal]]] = {
    "tendencia": tendencia, "reversion": reversion, "sesion": sesion, "scalping": scalping,
}
