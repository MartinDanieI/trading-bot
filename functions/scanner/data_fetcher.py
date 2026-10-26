"""Descarga de velas OHLCV con fallbacks y filtro de horario del Oro.

Orden de fuentes (Binance se evita a propósito: devuelve HTTP 451 a las IPs
de Google Cloud y GitHub Actions):

    BTC/USD : Kraken (ccxt, público)  ->  Yahoo Finance "BTC-USD"
    XAU/USD : Yahoo "GC=F"  ->  Yahoo "XAUUSD=X"  ->  Kraken "PAXG/USD" (proxy 1:1)

Todas las series se devuelven como DataFrame con índice UTC y columnas
open, high, low, close, volume, SOLO con velas ya cerradas.
"""

from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)
# yfinance aún usa APIs de pandas marcadas como obsoletas; sus avisos solo ensucian el log
warnings.filterwarnings("ignore", module=r"yfinance(\.|$)")

T = TypeVar("T")

CANDLES = 100
MIN_CANDLES = 60  # EMA50 + margen; menos que esto se considera fallo
TIMEFRAMES = {"1h": timedelta(hours=1), "15m": timedelta(minutes=15)}
# periodo que se pide a Yahoo para obtener >= 100 velas de cada temporalidad
YF_PERIOD = {"1h": "10d", "15m": "5d"}
COLUMNS = ["open", "high", "low", "close", "volume"]
NY_TZ = ZoneInfo("America/New_York")


class DataUnavailableError(RuntimeError):
    """Ninguna fuente pudo entregar datos válidos para el activo."""


@dataclass
class MarketData:
    asset: str  # "BTC/USD" | "XAU/USD"
    source: str  # descripción de la fuente usada
    h1: pd.DataFrame
    m15: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def with_retries(fn: Callable[[], T], label: str, attempts: int = 3, base_delay: float = 2.0) -> T:
    """Ejecuta fn con reintentos y backoff exponencial (2s, 4s...)."""
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de red/API cuenta
            last_exc = exc
            log.warning("%s: intento %d/%d falló: %s", label, i, attempts, exc)
            if i < attempts:
                time.sleep(base_delay * (2 ** (i - 1)))
    raise DataUnavailableError(f"{label}: sin datos tras {attempts} intentos ({last_exc})")


def _drop_unclosed(df: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    """Elimina la vela en formación (su apertura + duración aún no llega)."""
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    tf = TIMEFRAMES[timeframe]
    closed_mask = (df.index + tf) <= pd.Timestamp(now)
    return df[closed_mask]


def _validate(df: pd.DataFrame, label: str) -> pd.DataFrame:
    df = df[COLUMNS].astype(float).dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if len(df) < MIN_CANDLES:
        raise DataUnavailableError(f"{label}: solo {len(df)} velas (mínimo {MIN_CANDLES})")
    return df.tail(CANDLES)


# --------------------------------------------------------------------------- #
# Fuentes
# --------------------------------------------------------------------------- #
def _fetch_kraken(symbol: str, timeframe: str) -> pd.DataFrame:
    import ccxt  # import diferido: acelera el arranque si no se usa

    def _call() -> pd.DataFrame:
        exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 20000})
        # +1 por la vela en formación que luego se descarta
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=CANDLES + 1)
        if not rows:
            raise ValueError("respuesta vacía")
        df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
        df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        return _validate(_drop_unclosed(df, timeframe), f"kraken {symbol} {timeframe}")

    return with_retries(_call, f"kraken {symbol} {timeframe}")


def _fetch_yahoo(ticker: str, timeframe: str) -> pd.DataFrame:
    import yfinance as yf

    # yfinance reinicia los filtros al importarse: se vuelve a silenciar aquí
    warnings.filterwarnings("ignore", module=r"yfinance(\.|$)")

    def _call() -> pd.DataFrame:
        raw = yf.Ticker(ticker).history(
            period=YF_PERIOD[timeframe], interval=timeframe, auto_adjust=False, prepost=False
        )
        if raw is None or raw.empty:
            raise ValueError("respuesta vacía")
        df = raw.rename(columns=str.lower)
        idx = pd.DatetimeIndex(df.index)
        df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        return _validate(_drop_unclosed(df, timeframe), f"yahoo {ticker} {timeframe}")

    return with_retries(_call, f"yahoo {ticker} {timeframe}", attempts=2)


def _try_sources(asset: str, sources: list[tuple[str, Callable[[str], pd.DataFrame]]]) -> MarketData:
    """Prueba cada fuente en orden; ambas temporalidades deben salir de la misma."""
    warnings: list[str] = []
    for name, fetch in sources:
        try:
            h1 = fetch("1h")
            m15 = fetch("15m")
            log.info("%s: datos de %s (%d velas 1h, %d velas 15m)", asset, name, len(h1), len(m15))
            return MarketData(asset=asset, source=name, h1=h1, m15=m15, warnings=warnings)
        except DataUnavailableError as exc:
            warnings.append(str(exc))
            log.warning("%s: fuente %s descartada -> %s", asset, name, exc)
    raise DataUnavailableError(f"{asset}: todas las fuentes fallaron: {' | '.join(warnings)}")


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def fetch_btc() -> MarketData:
    return _try_sources(
        "BTC/USD",
        [
            ("Kraken BTC/USD", lambda tf: _fetch_kraken("BTC/USD", tf)),
            ("Yahoo BTC-USD", lambda tf: _fetch_yahoo("BTC-USD", tf)),
        ],
    )


def fetch_gold() -> MarketData:
    data = _try_sources(
        "XAU/USD",
        [
            ("Yahoo GC=F (futuros oro)", lambda tf: _fetch_yahoo("GC=F", tf)),
            ("Yahoo XAUUSD=X", lambda tf: _fetch_yahoo("XAUUSD=X", tf)),
            ("Kraken PAXG/USD (proxy oro)", lambda tf: _fetch_kraken("PAXG/USD", tf)),
        ],
    )
    if "PAXG" in data.source:
        data.warnings.append("Precio de PAXG usado como proxy 1:1 del oro spot.")
    return data


def is_gold_market_open(now: datetime | None = None) -> bool:
    """Horario Forex/Commodities en hora de Nueva York (EST/EDT, con DST automático).

    Cerrado: sábado todo el día, viernes desde las 17:00 y domingo antes de las 18:00.
    """
    now = now or datetime.now(timezone.utc)
    ny = now.astimezone(NY_TZ)
    wd = ny.weekday()  # lunes=0 ... domingo=6
    if wd == 5:
        return False
    if wd == 4 and ny.hour >= 17:
        return False
    if wd == 6 and ny.hour < 18:
        return False
    return True
