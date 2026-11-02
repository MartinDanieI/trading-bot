"""Escaneo multi-persona: cada quien recibe solo las estrategias y activos que eligió.

Corre cada 5 minutos (cron-job.org -> GitHub Actions). En cada ejecución:
    1. Pide al bot de Telegram la lista de personas (subscribers.py).
    2. Tendencia diaria: una vez por vela diaria (trend_runner.py).
    3. Estrategias intradía: cada una se revisa solo cuando cerró una vela nueva de su
       temporalidad (5m, 15m, 1h, 4h); la señal se manda a quienes la eligieron.
    4. Mensaje diario "Bot activo" y reporte semanal, personalizados.

Todo el estado (qué vela ya se revisó, señales enviadas, posiciones) vive en
StateManager, así que si una ejecución se salta, la siguiente se pone al día.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from . import subscribers as subs_mod
from . import trend_runner
from .config import Settings, load_settings
from .data_fetcher import (TIMEFRAMES, DataUnavailableError, fetch_candles, fetch_history,
                           fetch_live_price, is_gold_market_open)
from .performance import evaluate_signal, summarize
from .runner import DISCLAIMER, _fmt, _stamp
from .state_manager import StateManager
from .strategies import CATALOG, DETECTORS, Signal, StrategyInfo
from .whatsapp_notifier import WhatsAppError, get_notifier

log = logging.getLogger(__name__)

VALIDITY = {"5m": "unos 10 minutos", "15m": "unos 30 minutos", "1h": "alrededor de 1 hora",
            "4h": "unas 4 horas"}
STALE = {"5m": timedelta(minutes=15), "15m": timedelta(minutes=40), "1h": timedelta(hours=2),
         "4h": timedelta(hours=6)}


def _akey(asset: str) -> str:
    return asset.replace("/", "_")


def _short(asset: str) -> str:
    return "Oro" if asset.startswith("XAU") else asset.split("/")[0]


# --------------------------------------------------------------------------- #
# Mensajes
# --------------------------------------------------------------------------- #
def format_signal(s: Signal, info: StrategyInfo, check: dict, source: str, settings: Settings,
                  now: datetime) -> str:
    up = s.direction == "LONG"
    late = check["late"]
    r1 = abs(s.tp1 - s.entry) / s.risk
    r2 = abs(s.tp2 - s.entry) / s.risk
    lines = [
        "⏰ *SEÑAL TARDE · NO ENTRAR* ⏰" if late else "🚨 *SEÑAL DE ENTRADA* 🚨",
        f"{info.emoji} {info.name} · *{s.asset}* — {'🟢 *COMPRA (LONG)*' if up else '🔴 *VENTA (SHORT)*'}",
        f"💡 {' · '.join(s.reasons[:4])}",
        "",
        f"🎯 Entrada: *{_fmt(s.entry)}*",
        f"🛑 SL: *{_fmt(s.stop_loss)}*",
        f"✅ TP1: *{_fmt(s.tp1)}*  ({r1:.1f} R)",
        f"✅ TP2: *{_fmt(s.tp2)}*  ({r2:.1f} R)",
        "",
        f"📍 Zona de entrada: *{_fmt(check['low'])} – {_fmt(check['high'])}*",
    ]
    if check["live"] is not None:
        lines.append(f"⚡ Precio al enviar: {_fmt(check['live'])} ({check['moved_r']:+.2f} R desde la entrada)")
    if late:
        lines.append("_El precio ya salió de la zona: no entres._")
    else:
        lines.append(f"⏱ Vela de {info.timeframe}: la señal vale por {VALIDITY[info.timeframe]}. "
                     "Si el precio sale de la zona, déjala pasar.")
    lines += [f"🕐 {_stamp(settings, now)} · {source}", DISCLAIMER]
    return "\n".join(lines)


def entry_check(s: Signal, live: float | None, zone_r: float) -> dict:
    low, high = s.entry - zone_r * s.risk, s.entry + zone_r * s.risk
    if live is None:
        return {"live": None, "moved_r": None, "low": low, "high": high, "late": False}
    sign = 1 if s.direction == "LONG" else -1
    return {"live": live, "moved_r": sign * (live - s.entry) / s.risk, "low": low, "high": high,
            "late": not low <= live <= high}


def strategy_list(ids: list[str]) -> str:
    return ", ".join(f"{CATALOG[i].emoji} {CATALOG[i].name}" for i in ids)


# --------------------------------------------------------------------------- #
# Envío
# --------------------------------------------------------------------------- #
def _send(settings: Settings, text: str, chat_ids: list[str], dry_run: bool) -> dict:
    if not chat_ids:
        return {"delivered_as": "nobody"}  # nadie eligió esto: cuenta como procesado
    if dry_run or not settings.notifier_configured:
        reason = "DRY_RUN" if dry_run else "canal de alertas sin configurar"
        print(f"\n----- [{reason}] para {len(chat_ids)} persona(s) -----\n{text}\n" + "-" * 48)
        return {"delivered_as": "console", "reason": reason}
    try:
        notifier = get_notifier(settings)
        if settings.provider == "telegram":
            return notifier.notify(text, chat_ids=chat_ids)
        return notifier.notify(text)  # WhatsApp: un único destinatario
    except (WhatsAppError, OSError) as exc:
        log.error("Fallo al enviar: %s", exc)
        return {"delivered_as": "error", "error": str(exc)}


def _ok(result: dict) -> bool:
    return result.get("delivered_as") not in ("console", "error")


# --------------------------------------------------------------------------- #
# Escaneo
# --------------------------------------------------------------------------- #
def run_scan(settings: Settings | None = None, *, dry_run: bool | None = None,
             now: datetime | None = None, **_ignored) -> dict:
    settings = settings or load_settings()
    dry_run = settings.dry_run if dry_run is None else dry_run
    now = now or datetime.now(timezone.utc)
    state = StateManager(settings)
    people = subs_mod.load(settings, state)
    log.info("Escaneo %s · %d persona(s) · estado %s · dry_run=%s",
             now.isoformat(timespec="minutes"), len(people), state.name, dry_run)
    report: dict = {"timestamp": now.isoformat(), "dry_run": dry_run, "people": len(people),
                    "signals": [], "errors": {}, "daily": []}

    # 1. Tendencia diaria
    if any("tendencia_diaria" in p.strategies for p in people):
        daily = trend_runner.process_daily(
            settings, state,
            lambda text, asset: _send(settings, text, subs_mod.recipients(people, "tendencia_diaria", asset),
                                      dry_run),
            dry_run)
        report["daily"] = daily.get("events", [])
        report["errors"].update(daily.get("errors", {}))

    # 2. Estrategias intradía
    needed = sorted({(sid, a) for p in people for sid in p.strategies if sid in DETECTORS for a in p.assets})
    gold_open = is_gold_market_open(now)
    bars_needed: dict[tuple[str, str], int] = {}
    for sid, asset in needed:
        for tf, bars in CATALOG[sid].needs.items():
            bars_needed[(asset, tf)] = max(bars_needed.get((asset, tf), 0), bars)
    candles: dict[tuple[str, str], tuple[pd.DataFrame, str]] = {}

    def get(asset: str, tf: str) -> tuple[pd.DataFrame, str]:
        if (asset, tf) not in candles:
            candles[(asset, tf)] = fetch_candles(asset, tf, bars_needed[(asset, tf)])
        return candles[(asset, tf)]

    for sid, asset in needed:
        if asset == "XAU/USD" and not gold_open:
            continue
        info = CATALOG[sid]
        try:
            data = {tf: get(asset, tf)[0] for tf in info.needs}
        except DataUnavailableError as exc:
            report["errors"][f"{sid} {asset}"] = str(exc)
            log.error("%s %s: %s", sid, asset, exc)
            continue
        source = get(asset, info.timeframe)[1]
        bar_close = data[info.timeframe].index[-1] + TIMEFRAMES[info.timeframe]
        key = f"_EVAL_{sid}_{_akey(asset)}"
        last = state.get(key).get("t")
        if last and bar_close <= pd.Timestamp(last):
            continue  # todavía no cierra una vela nueva de esta temporalidad
        if not dry_run:
            state.put(key, {"t": bar_close.isoformat()})
        if pd.Timestamp(now) - bar_close > STALE[info.timeframe]:
            log.info("%s %s: datos atrasados (última vela %s); se omite.", sid, asset, bar_close)
            continue
        for s in [x for x in DETECTORS[sid](data, asset) if x.time == bar_close]:
            _deliver_signal(settings, state, people, s, info, source, now, dry_run, report)

    # 3 y 4. Mensaje diario y reporte semanal (por persona)
    _personal_messages(settings, state, people, now, dry_run, report)
    return report


def _deliver_signal(settings, state, people, s: Signal, info: StrategyInfo, source: str, now: datetime,
                    dry_run: bool, report: dict) -> None:
    dup_key = f"_LAST_{s.strategy}_{_akey(s.asset)}_{s.direction}"
    last = state.get(dup_key).get("t")
    if last and pd.Timestamp(s.time) - pd.Timestamp(last) < info.cooldown:
        log.info("%s %s %s repetida dentro de %s: se omite.", s.strategy, s.asset, s.direction, info.cooldown)
        return
    to = subs_mod.recipients(people, s.strategy, s.asset)
    check = entry_check(s, fetch_live_price(s.asset, source), settings.entry_zone_r)
    if check["late"] and settings.late_signals == "skip":
        log.info("%s %s %s: el precio ya salió de la zona; no se envía.", s.strategy, s.asset, s.direction)
        return
    result = _send(settings, format_signal(s, info, check, source, settings, now), to, dry_run)
    report["signals"].append(f"{s.strategy} {s.asset} {s.direction} → {len(to)}")
    if _ok(result) and not dry_run:
        state.put(dup_key, {"t": pd.Timestamp(s.time).isoformat()})
        state._append_history({"strategy": s.strategy, "asset": s.asset, "direction": s.direction,
                               "sent_at": pd.Timestamp(s.time).isoformat(),
                               "levels": {"entry": s.entry, "stop_loss": s.stop_loss, "tp1": s.tp1, "tp2": s.tp2}},
                              now)


def _personal_messages(settings, state, people, now: datetime, dry_run: bool, report: dict) -> None:
    tz = ZoneInfo(settings.timezone)
    local = now.astimezone(tz)
    today = local.date().isoformat()
    iso = local.isocalendar()
    week = f"{iso.year}-S{iso.week:02d}"
    weekly_due = settings.report_weekday >= 0 and (
        local.weekday() > settings.report_weekday
        or (local.weekday() == settings.report_weekday and local.hour >= settings.report_hour))
    heartbeat_due = 0 <= settings.heartbeat_hour <= local.hour
    eval_cache: dict = {}

    for p in people:
        mine = [h for h in state.signals_since(now - timedelta(days=7))
                if h.get("strategy") in p.strategies and h["asset"] in p.assets]
        if heartbeat_due and state.get(f"_HB_{p.id}").get("date") != today:
            last24 = [h for h in mine if datetime.fromisoformat(h["sent_at"]) >= now - timedelta(hours=24)]
            lines = [f"💚 *ESTADO DEL BOT* · Activo · {_stamp(settings, now)}",
                     f"🧩 Tus estrategias: {strategy_list(p.strategies)}",
                     f"🪙 Activos: {', '.join(_short(a) for a in p.assets)}",
                     f"📬 Señales para ti en las últimas 24 h: {len(last24)}"]
            if "tendencia_diaria" in p.strategies:
                lines += [f"📊 {line}" for line in trend_runner.heartbeat_lines(state, settings.timezone)]
            lines.append("_Cambia tus estrategias con /estrategias · si mañana no llega este mensaje, avisa._")
            if _ok(_send(settings, "\n".join(lines), [p.id], dry_run)) and not dry_run:
                state.put(f"_HB_{p.id}", {"date": today})

        if weekly_due and state.get(f"_WK_{p.id}").get("week") != week:
            text = _weekly_text(settings, state, p, mine, now, eval_cache)
            if _ok(_send(settings, text, [p.id], dry_run)) and not dry_run:
                state.put(f"_WK_{p.id}", {"week": week})


def _evaluate(h: dict, cache: dict):
    info = CATALOG[h["strategy"]]
    key = (h["asset"], info.eval_tf)
    if key not in cache:
        try:
            cache[key] = fetch_history(h["asset"], info.eval_tf, 9)
        except DataUnavailableError as exc:
            log.error("Reporte semanal: %s", exc)
            cache[key] = None
    if cache[key] is None:
        return None
    lv = h["levels"]
    return evaluate_signal(cache[key], h["asset"], h["direction"], datetime.fromisoformat(h["sent_at"]),
                           lv["entry"], lv["stop_loss"], lv["tp1"], lv["tp2"], horizon=info.horizon)


def _weekly_text(settings, state, p, mine: list[dict], now: datetime, cache: dict) -> str:
    tz = ZoneInfo(settings.timezone)
    lines = ["📊📊 *REPORTE SEMANAL* 📊📊",
             f"🗓 {(now - timedelta(days=7)).astimezone(tz):%d/%m} – {now.astimezone(tz):%d/%m}",
             "━━━━━━━━━━━━━━━"]
    total_r = 0.0
    for sid in p.strategies:
        info = CATALOG[sid]
        if sid == "tendencia_diaria":
            lines += trend_runner.weekly_lines(state, now, settings.timezone)
            total_r += sum(t["r"] for t in trend_runner.closed_since(state, now - timedelta(days=7))
                           if t["asset"] in p.assets)
            continue
        trades = [t for h in mine if h["strategy"] == sid and (t := _evaluate(h, cache))]
        if not trades:
            lines.append(f"{info.emoji} *{info.name}*: sin señales esta semana")
            continue
        s = summarize(trades)
        total_r += s["total_r"]
        pct = "—" if s["acierto_pct"] is None else f"{s['acierto_pct']:.0f}%"
        open_ = f" · ⏳ {s['abiertas']} abiertas" if s["abiertas"] else ""
        lines.append(f"{info.emoji} *{info.name}*: {s['senales']} señales · ✅ {s['ganadas']} · "
                     f"❌ {s['perdidas']}{open_} · acierto {pct} · *{s['total_r']:+.1f} R*")
    lines += ["━━━━━━━━━━━━━━━", f"💰 Total de tus estrategias: *{total_r:+.1f} R*",
              "_R = lo que se arriesga por operación. Simulación: 50 % sale en TP1 y el resto en TP2 o en la "
              "entrada; incluye comisiones._", DISCLAIMER]
    return "\n".join(lines)
