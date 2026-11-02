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
TIMEFRAMES = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1h": timedelta(hours=1),
              "4h": timedelta(hours=4)}
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


def fetch_live_price(asset: str, source: str) -> float | None:
    """Último precio negociado, de la misma fuente que usó el análisis.

    Se consulta justo antes de enviar una señal para saber si el precio ya se
    alejó de la entrada. Devuelve None si ninguna fuente responde.
    """
    if "Kraken" in source:
        symbol = "PAXG/USD" if "PAXG" in source else "BTC/USD"
        try:
            import ccxt

            def _ticker() -> float:
                return float(ccxt.kraken({"enableRateLimit": True, "timeout": 15000})
                             .fetch_ticker(symbol)["last"])

            return with_retries(_ticker, f"precio en vivo {symbol}", attempts=2, base_delay=1)
        except DataUnavailableError as exc:
            log.warning("%s", exc)
    import yfinance as yf

    warnings.filterwarnings("ignore", module=r"yfinance(\.|$)")
    for ticker in HISTORY_TICKERS[asset]:
        try:
            raw = yf.Ticker(ticker).history(period="1d", interval="1m")
            if raw is not None and not raw.empty:
                return float(raw["Close"].dropna().iloc[-1])
        except Exception as exc:  # noqa: BLE001
            log.warning("precio en vivo %s: %s", ticker, exc)
    return None


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Agrupa velas (ej. 1h -> 4h) alineadas a medianoche UTC, igual que Kraken."""
    out = df.resample(rule, origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna(subset=["close"])


def fetch_candles(asset: str, timeframe: str, bars: int) -> tuple[pd.DataFrame, str]:
    """Velas cerradas recientes para el escaneo en vivo. Devuelve (velas, fuente).

    BTC: Kraken primero (más fresco), luego Yahoo. Oro: Yahoo (GC=F) y luego PAXG en Kraken.
    """
    def kraken(symbol: str) -> pd.DataFrame:
        import ccxt

        def _call() -> pd.DataFrame:
            rows = ccxt.kraken({"enableRateLimit": True, "timeout": 20000}).fetch_ohlcv(
                symbol, timeframe=timeframe, limit=min(bars + 1, 720))
            df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
            df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
            return _drop_unclosed(df.astype(float), timeframe)

        return with_retries(_call, f"kraken {symbol} {timeframe}", attempts=2)

    tf_days = {"5m": 3, "15m": 5, "1h": 15, "4h": 59}
    days = max(tf_days[timeframe], int(bars * TIMEFRAMES[timeframe].total_seconds() / 86400 * 1.6) + 2)
    sources = ([("Kraken BTC/USD", lambda: kraken("BTC/USD")),
                ("Yahoo BTC-USD", lambda: fetch_history(asset, timeframe, days))]
               if asset == "BTC/USD" else
               [("Yahoo GC=F (futuros oro)", lambda: fetch_history(asset, timeframe, days)),
                ("Kraken PAXG/USD (proxy oro)", lambda: kraken("PAXG/USD"))])
    errors = []
    for name, fn in sources:
        try:
            df = fn()
            if len(df) >= min(bars, 60):
                return df.tail(bars), name
            errors.append(f"{name}: solo {len(df)} velas")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    raise DataUnavailableError(f"{asset} {timeframe}: {' | '.join(errors)}")


HISTORY_TICKERS = {"BTC/USD": ["BTC-USD"], "XAU/USD": ["GC=F", "XAUUSD=X"]}
HISTORY_KRAKEN = {"BTC/USD": "BTC/USD", "XAU/USD": "PAXG/USD"}


def fetch_history(asset: str, timeframe: str, days: int) -> pd.DataFrame:
    """Velas cerradas de los últimos `days` días (backtest y reporte semanal).

    Yahoo da hasta 60 días en 15m y ~2 años en 1h; Kraken (respaldo) solo las
    últimas 720 velas (~7,5 días en 15m, 30 días en 1h).
    """
    import yfinance as yf

    warnings.filterwarnings("ignore", module=r"yfinance(\.|$)")
    days = min(days, 59) if timeframe in ("5m", "15m") else days
    yf_interval = "1h" if timeframe == "4h" else timeframe  # Yahoo no tiene 4h: se agrupa desde 1h
    errors = []
    for ticker in HISTORY_TICKERS[asset]:
        try:
            raw = yf.Ticker(ticker).history(period=f"{days}d", interval=yf_interval, auto_adjust=False)
            if raw is None or raw.empty:
                raise ValueError("respuesta vacía")
            df = raw.rename(columns=str.lower)
            idx = pd.DatetimeIndex(df.index)
            df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
            df = df[COLUMNS].astype(float).dropna(subset=["open", "high", "low", "close"])
            df = df[~df.index.duplicated(keep="last")].sort_index()
            if timeframe == "4h":
                df = resample_ohlcv(_drop_unclosed(df, "1h"), "4h")
            return _drop_unclosed(df, timeframe)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"yahoo {ticker}: {exc}")
    try:
        import ccxt

        rows = ccxt.kraken({"enableRateLimit": True, "timeout": 20000}).fetch_ohlcv(
            HISTORY_KRAKEN[asset], timeframe=timeframe, limit=720)
        df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
        df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        return _drop_unclosed(df.astype(float), timeframe)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"kraken: {exc}")
    raise DataUnavailableError(f"{asset} {timeframe}: sin historial ({' | '.join(errors)})")


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
