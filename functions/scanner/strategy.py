"""Indicadores técnicos (pandas/numpy puros), motor de confluencia y niveles.

Sin TA-Lib ni pandas_ta: todo se calcula de forma vectorial para que el paquete
sea ligero y no requiera compilar C al desplegar en Firebase.

Puntuación: cada setup suma hasta 100 puntos. El puntaje LONG es el máximo
entre sus setups A y B (igual para SHORT). Hay señal cuando el mejor puntaje
alcanza el umbral y supera al de la dirección contraria.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .data_fetcher import MarketData

WINDOW_24H = 24  # velas de 1h
VOLUME_SPIKE = 1.2
SL_ATR_MULT = 1.5
TP1_RR = 1.5
TP2_RR = 2.5


# --------------------------------------------------------------------------- #
# Indicadores
# --------------------------------------------------------------------------- #
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI con suavizado de Wilder (igual que TradingView)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100 - 100 / (1 + rs)
    # sin pérdidas -> 100; sin movimiento -> 50
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out.where(avg_loss.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema9"] = ema(out["close"], 9)
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["rsi"] = rsi(out["close"], 14)
    out = out.join(macd(out["close"]))
    out["atr"] = atr(out, 14)
    return out


# --------------------------------------------------------------------------- #
# Resultado
# --------------------------------------------------------------------------- #
@dataclass
class Levels:
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    risk: float  # distancia al SL


@dataclass
class Analysis:
    asset: str
    source: str
    timestamp: pd.Timestamp  # apertura de la última vela 1h cerrada (UTC)
    price: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    range_position_pct: float
    avg_volume_24h: float
    volume_ratio: float | None  # None si la fuente no reporta volumen
    rsi_1h: float
    rsi_15m: float
    atr_1h: float
    long_score: float
    short_score: float
    long_setup: str
    short_setup: str
    long_reasons: list[str]
    short_reasons: list[str]
    direction: str  # LONG | SHORT | NEUTRAL
    threshold: float
    levels: Levels | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        if self.direction == "LONG":
            return self.long_score
        if self.direction == "SHORT":
            return self.short_score
        return max(self.long_score, self.short_score)

    @property
    def setup(self) -> str:
        return self.long_setup if self.direction == "LONG" else self.short_setup

    @property
    def reasons(self) -> list[str]:
        if self.direction == "LONG":
            return self.long_reasons
        if self.direction == "SHORT":
            return self.short_reasons
        return self.long_reasons if self.long_score >= self.short_score else self.short_reasons

    def to_dict(self) -> dict:
        d = {
            "asset": self.asset,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "change_24h_pct": round(self.change_24h_pct, 3),
            "high_24h": self.high_24h,
            "low_24h": self.low_24h,
            "range_position_pct": round(self.range_position_pct, 1),
            "avg_volume_24h": self.avg_volume_24h,
            "volume_ratio": None if self.volume_ratio is None else round(self.volume_ratio, 2),
            "rsi_1h": round(self.rsi_1h, 1),
            "rsi_15m": round(self.rsi_15m, 1),
            "atr_1h": self.atr_1h,
            "long_score": self.long_score,
            "short_score": self.short_score,
            "direction": self.direction,
            "setup": self.setup if self.direction != "NEUTRAL" else None,
            "reasons": self.reasons,
            "warnings": self.warnings,
        }
        if self.levels:
            d["levels"] = vars(self.levels)
        return d


# --------------------------------------------------------------------------- #
# Motor de confluencia
# --------------------------------------------------------------------------- #
class _Card:
    """Acumula puntos y motivos de un setup."""

    def __init__(self, name: str):
        self.name = name
        self.score = 0.0
        self.reasons: list[str] = []

    def add(self, ok: bool, points: float, reason: str) -> None:
        if ok:
            self.score += points
            self.reasons.append(reason)


def _rising(s: pd.Series) -> bool:
    return bool(s.iloc[-1] > s.iloc[-2])


def _falling(s: pd.Series) -> bool:
    return bool(s.iloc[-1] < s.iloc[-2])


def _divergence(h1: pd.DataFrame, bullish: bool) -> bool:
    """Divergencia simple en 24h: nuevo extremo de precio en las últimas 6 velas
    sin confirmación del RSI frente al extremo de las 18 velas previas."""
    win = h1.tail(WINDOW_24H)
    recent, prior = win.tail(6), win.head(WINDOW_24H - 6)
    if recent["rsi"].isna().any() or prior["rsi"].isna().any():
        return False
    if bullish:
        r_i, p_i = recent["low"].idxmin(), prior["low"].idxmin()
        return bool(win.at[r_i, "low"] < win.at[p_i, "low"] and win.at[r_i, "rsi"] > win.at[p_i, "rsi"])
    r_i, p_i = recent["high"].idxmax(), prior["high"].idxmax()
    return bool(win.at[r_i, "high"] > win.at[p_i, "high"] and win.at[r_i, "rsi"] < win.at[p_i, "rsi"])


def _volume_points(ratio: float | None, points: float) -> tuple[bool, float, str]:
    """Si la fuente no trae volumen (ej. XAUUSD=X) se otorga la mitad de los
    puntos para no penalizar por completo un dato inexistente."""
    if ratio is None:
        return True, points / 2, "volumen N/D (½ pts)"
    return ratio > VOLUME_SPIKE, points, f"volumen {ratio:.1f}x media 24h"


def _score_long(h1: pd.DataFrame, m15: pd.DataFrame, pos: float, high_24h: float, vol_ratio: float | None):
    c1, p1 = h1.iloc[-1], h1.iloc[-2]
    c15 = m15.iloc[-1]

    # Setup A: rebote en soporte de 24h
    a = _Card("A · Rebote en soporte 24h")
    a.add(pos <= 25, 30, f"precio en zona baja del rango ({pos:.0f}%)")
    rsi_turn = 28 <= c1["rsi"] <= 42 and c1["rsi"] > p1["rsi"]
    div = _divergence(h1, bullish=True)
    a.add(rsi_turn or div, 25,
          f"RSI {c1['rsi']:.0f} girando al alza" if rsi_turn else "divergencia alcista RSI")
    a.add(c1["close"] > c1["ema9"], 15, "cierre 1h sobre EMA9")
    a.add(c15["close"] > c15["ema9"], 15, "cierre 15m sobre EMA9")
    a.add(_rising(h1["macd_hist"]), 10, "histograma MACD mejorando")
    a.add(_rising(m15["rsi"]), 5, "RSI 15m subiendo")

    # Setup B: momentum / breakout alcista
    b = _Card("B · Momentum / Breakout alcista")
    b.add(c1["ema20"] > c1["ema50"], 25, "EMA20 > EMA50")
    near_high = pos >= 75 or c1["close"] >= high_24h - 0.5 * c1["atr"]
    b.add(near_high, 20, f"presionando el máximo 24h ({pos:.0f}%)")
    ok, pts, why = _volume_points(vol_ratio, 20)
    b.add(ok, pts, why)
    b.add(52 <= c1["rsi"] <= 68, 15, f"RSI saludable {c1['rsi']:.0f}")
    b.add(_rising(h1["macd_hist"]), 10, "histograma MACD creciente")
    b.add(c15["close"] > c15["ema20"] and c15["ema9"] > c15["ema20"], 10, "15m alineado al alza")

    return max((a, b), key=lambda card: card.score)


def _score_short(h1: pd.DataFrame, m15: pd.DataFrame, pos: float, low_24h: float, vol_ratio: float | None):
    c1, p1 = h1.iloc[-1], h1.iloc[-2]
    c15 = m15.iloc[-1]

    # Setup A: rechazo en resistencia de 24h
    a = _Card("A · Rechazo en resistencia 24h")
    a.add(pos >= 75, 30, f"precio en zona alta del rango ({pos:.0f}%)")
    rsi_turn = 60 <= c1["rsi"] <= 78 and c1["rsi"] < p1["rsi"]
    div = _divergence(h1, bullish=False)
    a.add(rsi_turn or div, 25,
          f"RSI {c1['rsi']:.0f} girando a la baja" if rsi_turn else "divergencia bajista RSI")
    a.add(c1["close"] < c1["ema9"], 15, "cierre 1h bajo EMA9")
    a.add(c15["close"] < c15["ema9"], 15, "cierre 15m bajo EMA9")
    a.add(_falling(h1["macd_hist"]), 10, "histograma MACD empeorando")
    a.add(_falling(m15["rsi"]), 5, "RSI 15m bajando")

    # Setup B: momentum / breakdown bajista
    b = _Card("B · Momentum / Breakdown bajista")
    b.add(c1["ema20"] < c1["ema50"], 25, "EMA20 < EMA50")
    near_low = pos <= 25 or c1["close"] <= low_24h + 0.5 * c1["atr"]
    bearish_candle = c1["close"] < c1["open"]
    b.add(near_low or bearish_candle, 20,
          f"presión vendedora ({pos:.0f}% del rango)")
    ok, pts, why = _volume_points(vol_ratio, 20)
    b.add(ok, pts, why)
    b.add(32 <= c1["rsi"] <= 48, 15, f"RSI bajista {c1['rsi']:.0f}")
    b.add(_falling(h1["macd_hist"]), 10, "histograma MACD decreciente")
    b.add(c15["close"] < c15["ema20"] and c15["ema9"] < c15["ema20"], 10, "15m alineado a la baja")

    return max((a, b), key=lambda card: card.score)


def compute_levels(entry: float, atr_value: float, direction: str) -> Levels:
    risk = SL_ATR_MULT * atr_value
    sign = 1 if direction == "LONG" else -1
    return Levels(
        entry=entry,
        stop_loss=entry - sign * risk,
        tp1=entry + sign * TP1_RR * risk,
        tp2=entry + sign * TP2_RR * risk,
        risk=risk,
    )


def analyze(data: MarketData, threshold: float = 70.0) -> Analysis:
    h1 = add_indicators(data.h1)
    m15 = add_indicators(data.m15)
    last = h1.iloc[-1]
    needed = ["ema50", "rsi", "macd_hist", "atr"]
    if last[needed].isna().any() or m15.iloc[-1][["ema20", "rsi"]].isna().any():
        raise ValueError(f"{data.asset}: velas insuficientes para calcular indicadores")

    win = h1.tail(WINDOW_24H)
    price = float(last["close"])
    high_24h, low_24h = float(win["high"].max()), float(win["low"].min())
    span = high_24h - low_24h
    pos = 50.0 if span <= 0 else (price - low_24h) / span * 100

    ref = float(h1["close"].iloc[-(WINDOW_24H + 1)]) if len(h1) > WINDOW_24H else float(win["open"].iloc[0])
    change = (price / ref - 1) * 100

    avg_vol = float(win["volume"].mean())
    vol_ratio = float(last["volume"]) / avg_vol if avg_vol > 0 else None

    long_card = _score_long(h1, m15, pos, high_24h, vol_ratio)
    short_card = _score_short(h1, m15, pos, low_24h, vol_ratio)

    direction = "NEUTRAL"
    if long_card.score >= threshold and long_card.score > short_card.score:
        direction = "LONG"
    elif short_card.score >= threshold and short_card.score > long_card.score:
        direction = "SHORT"

    atr_1h = float(last["atr"])
    warnings = list(data.warnings)
    if vol_ratio is None:
        warnings.append("La fuente no reporta volumen.")

    return Analysis(
        asset=data.asset,
        source=data.source,
        timestamp=h1.index[-1],
        price=price,
        change_24h_pct=change,
        high_24h=high_24h,
        low_24h=low_24h,
        range_position_pct=pos,
        avg_volume_24h=avg_vol,
        volume_ratio=vol_ratio,
        rsi_1h=float(last["rsi"]),
        rsi_15m=float(m15["rsi"].iloc[-1]),
        atr_1h=atr_1h,
        long_score=round(long_card.score, 1),
        short_score=round(short_card.score, 1),
        long_setup=long_card.name,
        short_setup=short_card.name,
        long_reasons=long_card.reasons,
        short_reasons=short_card.reasons,
        direction=direction,
        threshold=threshold,
        levels=compute_levels(price, atr_1h, direction) if direction != "NEUTRAL" else None,
        warnings=warnings,
    )
