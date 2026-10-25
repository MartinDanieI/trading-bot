"""Orquestación de un escaneo: datos -> análisis -> anti-spam -> WhatsApp.

Lo usan tanto la Cloud Function (main.py) como run_standalone.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Settings, load_settings
from .data_fetcher import DataUnavailableError, fetch_btc, fetch_gold, is_gold_market_open
from .state_manager import StateManager
from .strategy import Analysis, analyze
from .whatsapp_notifier import WhatsAppError, get_notifier

log = logging.getLogger(__name__)

DISCLAIMER = "_Alerta automática con fines informativos. No es asesoría financiera._"


# --------------------------------------------------------------------------- #
# Formato de mensajes
# --------------------------------------------------------------------------- #
def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _stamp(settings: Settings, now: datetime) -> str:
    local = now.astimezone(ZoneInfo(settings.timezone))
    return local.strftime("%d/%m %H:%M") + f" ({settings.timezone.split('/')[-1]})"


def format_signal(a: Analysis) -> str:
    up = a.direction == "LONG"
    head = "🟢 *SUBIDA (LONG)*" if up else "🔴 *BAJADA (SHORT)*"
    lv = a.levels
    reasons = " · ".join(a.reasons[:5])
    lines = [
        f"*{a.asset}* — {head}",
        f"📊 Confianza: *{a.score:.0f}/100* · {a.setup}",
        f"💡 {reasons}",
        f"📈 Rango 24h: {_fmt(a.low_24h)} – {_fmt(a.high_24h)} (pos. {a.range_position_pct:.0f}%)"
        f" · 24h: {a.change_24h_pct:+.2f}%",
        "",
        f"🎯 Entrada: *{_fmt(lv.entry)}*",
        f"🛑 SL: *{_fmt(lv.stop_loss)}*  (1.5×ATR = {_fmt(lv.risk)})",
        f"✅ TP1: *{_fmt(lv.tp1)}*  (1:1.5)",
        f"✅ TP2: *{_fmt(lv.tp2)}*  (1:2.5)",
    ]
    if a.warnings:
        lines.append(f"⚠️ {' '.join(a.warnings)}")
    return "\n".join(lines)


def format_summary_block(a: Analysis) -> str:
    if a.direction == "NEUTRAL":
        status = "⚪ *NEUTRAL / SIN ENTRADA*"
    else:
        status = "🟢 *ENTRADA LONG*" if a.direction == "LONG" else "🔴 *ENTRADA SHORT*"
    vol = "N/D" if a.volume_ratio is None else f"{a.volume_ratio:.1f}x"
    lines = [
        f"*{a.asset}* — {status}",
        f"💲 {_fmt(a.price)} · 24h: {a.change_24h_pct:+.2f}%",
        f"📈 Rango: {_fmt(a.low_24h)} – {_fmt(a.high_24h)} (pos. {a.range_position_pct:.0f}%)",
        f"📊 RSI 1h {a.rsi_1h:.0f} · RSI 15m {a.rsi_15m:.0f} · Vol {vol}",
        f"🧮 Puntaje LONG {a.long_score:.0f} | SHORT {a.short_score:.0f} (umbral {a.threshold:.0f})",
    ]
    if a.levels:
        lv = a.levels
        lines.append(f"🎯 E {_fmt(lv.entry)} · 🛑 SL {_fmt(lv.stop_loss)} · "
                     f"✅ TP1 {_fmt(lv.tp1)} · TP2 {_fmt(lv.tp2)}")
        lines.append(f"💡 {' · '.join(a.reasons[:4])}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Escaneo
# --------------------------------------------------------------------------- #
def _deliver(settings: Settings, text: str, dry_run: bool) -> dict:
    if dry_run or not settings.notifier_configured:
        reason = "DRY_RUN" if dry_run else "canal de alertas sin configurar"
        print(f"\n----- [{reason}] mensaje que se enviaría -----\n{text}\n" + "-" * 48)
        return {"delivered_as": "console", "reason": reason}
    try:
        result = get_notifier(settings).notify(text)
        log.info("Alerta enviada como %s", result["delivered_as"])
        return result
    except (WhatsAppError, OSError) as exc:
        log.error("Fallo al enviar la alerta: %s", exc)
        return {"delivered_as": "error", "error": str(exc)}


def run_scan(
    settings: Settings | None = None,
    *,
    dry_run: bool | None = None,
    mode: str | None = None,
    now: datetime | None = None,
) -> dict:
    settings = settings or load_settings()
    dry_run = settings.dry_run if dry_run is None else dry_run
    mode = (mode or settings.alert_mode).upper()
    now = now or datetime.now(timezone.utc)
    state = StateManager(settings)
    log.info("Escaneo %s · modo %s · estado %s · dry_run=%s",
             now.isoformat(timespec="minutes"), mode, state.name, dry_run)

    fetchers = [("BTC/USD", fetch_btc)]
    gold_open = is_gold_market_open(now)
    if gold_open:
        fetchers.append(("XAU/USD", fetch_gold))
    else:
        log.info("Mercado del Oro cerrado: se omite XAU/USD.")

    analyses: list[Analysis] = []
    errors: dict[str, str] = {}
    for asset, fetch in fetchers:
        try:
            analyses.append(analyze(fetch(), threshold=settings.score_threshold))
        except (DataUnavailableError, ValueError) as exc:
            log.error("%s: %s", asset, exc)
            errors[asset] = str(exc)

    for a in analyses:
        log.info("%s %s · LONG %.0f / SHORT %.0f · %s", a.asset, _fmt(a.price),
                 a.long_score, a.short_score, a.direction)

    deliveries: list[dict] = []
    signals = [a for a in analyses if a.direction != "NEUTRAL"]
    fresh = [a for a in signals if not state.is_duplicate(a.asset, a.direction, now)]
    for a in signals:
        if a not in fresh:
            log.info("%s %s ya enviado hace < %.0fh: se omite.", a.asset, a.direction,
                     settings.cooldown_hours)

    if mode == "HOURLY_SUMMARY":
        blocks = [f"🕐 *Resumen 24h* · {_stamp(settings, now)}"]
        blocks += [format_summary_block(a) for a in analyses]
        if not gold_open:
            blocks.append("🥇 *XAU/USD* — mercado cerrado.")
        blocks += [f"❗ *{asset}*: sin datos ({msg[:120]})" for asset, msg in errors.items()]
        blocks.append(DISCLAIMER)
        result = _deliver(settings, "\n\n".join(blocks), dry_run)
        deliveries.append(result)
        sent = {a.asset: result for a in fresh}
    else:
        sent = {}
        for a in fresh:
            text = f"{format_signal(a)}\n🕐 {_stamp(settings, now)} · {a.source}\n{DISCLAIMER}"
            sent[a.asset] = _deliver(settings, text, dry_run)
            deliveries.append(sent[a.asset])

    # Solo se registra una señal que realmente se envió (nunca en dry-run)
    for a in fresh:
        if sent[a.asset].get("delivered_as") not in ("console", "error"):
            state.record_signal(a.asset, a.direction, a.score, a.price, now)

    return {
        "timestamp": now.isoformat(),
        "mode": mode,
        "dry_run": dry_run,
        "gold_market_open": gold_open,
        "state_backend": state.name,
        "analyses": [a.to_dict() for a in analyses],
        "new_signals": [f"{a.asset} {a.direction}" for a in fresh],
        "deliveries": deliveries,
        "errors": errors,
    }
