"""Evaluación de señales: ¿qué tocó primero, el SL o los TP?

Lo usan el backtest (backtest.py) y el reporte semanal, con las mismas reglas:

    - Se recorren las velas de 15m posteriores a la señal (hasta HORIZON horas).
    - Si en una misma vela se tocan SL y TP, se asume que tocó primero el SL
      (criterio conservador: nunca se inflan los resultados).
    - Gestión simulada: 50 % se cierra en TP1 y el SL del resto pasa a la
      entrada (break-even); el otro 50 % se cierra en TP2.
          SL antes de TP1           -> -1.00 R   (perdida)
          TP1 y luego break-even    -> +0.75 R   (ganada)
          TP1 y luego TP2           -> +2.00 R   (ganada)
          Ni SL ni TP1 en HORIZON h -> se cierra al precio de ese momento (expirada)
    - R = lo que se arriesga en la operación (distancia entrada-SL).

"Acierto" = porcentaje de señales cerradas que llegaron a TP1 antes que al SL.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import pandas as pd

HORIZON = timedelta(hours=48)
R_TP1_THEN_BE = 0.5 * 1.5 + 0.5 * 0.0
R_TP1_THEN_TP2 = 0.5 * 1.5 + 0.5 * 2.5


@dataclass
class TradeResult:
    asset: str
    direction: str
    sent_at: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    outcome: str  # perdida | ganada_tp1 | ganada_tp2 | expirada | abierta
    r: float  # resultado en R (0 si sigue abierta)
    closed_at: str | None = None

    @property
    def won(self) -> bool:
        return self.outcome.startswith("ganada")

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_signal(m15: pd.DataFrame, asset: str, direction: str, sent_at: datetime,
                    entry: float, stop_loss: float, tp1: float, tp2: float,
                    horizon: timedelta = HORIZON) -> TradeResult:
    """m15: velas de 15m (índice UTC = apertura) que cubren desde sent_at en adelante."""
    sent_ts = pd.Timestamp(sent_at)
    sent_ts = sent_ts.tz_localize("UTC") if sent_ts.tz is None else sent_ts.tz_convert("UTC")
    end = sent_ts + horizon
    future = m15[(m15.index >= sent_ts) & (m15.index < end)]
    risk = abs(entry - stop_loss)
    up = direction == "LONG"

    def result(outcome: str, r: float, ts) -> TradeResult:
        return TradeResult(asset, direction, sent_ts.isoformat(), entry, stop_loss, tp1, tp2,
                           outcome, round(r, 2), None if ts is None else pd.Timestamp(ts).isoformat())

    stop = stop_loss
    hit_tp1 = False
    for ts, c in future.iterrows():
        hit_sl = c["low"] <= stop if up else c["high"] >= stop
        if hit_sl:  # conservador: el SL se evalúa antes que los TP en la misma vela
            return result("ganada_tp1" if hit_tp1 else "perdida",
                          R_TP1_THEN_BE if hit_tp1 else -1.0, ts)
        if not hit_tp1 and (c["high"] >= tp1 if up else c["low"] <= tp1):
            hit_tp1 = True
            stop = entry  # break-even para la mitad restante
        if hit_tp1 and (c["high"] >= tp2 if up else c["low"] <= tp2):
            return result("ganada_tp2", R_TP1_THEN_TP2, ts)

    complete = len(future) and future.index[-1] + timedelta(minutes=15) >= end
    if not complete:
        return result("abierta", 0.0, None)
    last = float(future["close"].iloc[-1])
    if hit_tp1:  # mitad cerrada en TP1, la otra al precio final (nunca peor que break-even)
        rest = max((last - entry) / risk if up else (entry - last) / risk, 0.0)
        return result("ganada_tp1", 0.5 * 1.5 + 0.5 * rest, future.index[-1])
    move = (last - entry) / risk if up else (entry - last) / risk
    return result("expirada", move, future.index[-1])


def summarize(trades: list[TradeResult]) -> dict:
    closed = [t for t in trades if t.outcome != "abierta"]
    decided = [t for t in closed if t.outcome != "expirada"]
    wins = [t for t in decided if t.won]
    return {
        "senales": len(trades),
        "ganadas": len(wins),
        "perdidas": sum(t.outcome == "perdida" for t in decided),
        "expiradas": sum(t.outcome == "expirada" for t in closed),
        "abiertas": len(trades) - len(closed),
        "llegaron_tp2": sum(t.outcome == "ganada_tp2" for t in closed),
        "acierto_pct": round(100 * len(wins) / len(decided), 1) if decided else None,
        "total_r": round(sum(t.r for t in closed), 2),
        "r_promedio": round(sum(t.r for t in closed) / len(closed), 2) if closed else None,
    }
