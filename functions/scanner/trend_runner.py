"""Escaneo de la Tendencia diaria: una vez por vela diaria cerrada y por activo.

Estado guardado (StateManager):
    _DAILY_<activo>  {"day": última vela diaria procesada}
    _POS_<activo>    {"pos": posición abierta o None}
    _TRADES          {"items": operaciones cerradas de los últimos 30 días}

El estado solo avanza si el mensaje de ese día se entregó: si Telegram falla,
la próxima ejecución vuelve a procesar la misma vela y reintenta.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .daily_trend import PARAMS, fetch_daily, indicators, replay, step
from .data_fetcher import DataUnavailableError, fetch_live_price
from .state_manager import StateManager

log = logging.getLogger(__name__)

NAME = "📈 Tendencia diaria"
TRADES_KEEP = timedelta(days=30)


def _key(prefix: str, asset: str) -> str:
    return f"_{prefix}_{asset.replace('/', '_')}"


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _short(asset: str) -> str:
    return "Oro" if asset.startswith("XAU") else asset.split("/")[0]


def _day(iso: str, tz: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%d/%m")


# --------------------------------------------------------------------------- #
# Mensajes
# --------------------------------------------------------------------------- #
def format_entry(ev: dict, live: float | None, zone_r: float, source: str) -> str:
    pos, p = ev["pos"], PARAMS[ev["asset"]]
    up = pos["sign"] > 0
    side = "🟢 *COMPRA (LONG)*" if up else "🔴 *VENTA (SHORT)*"
    low, high = pos["entry"] - zone_r * pos["risk"], pos["entry"] + zone_r * pos["risk"]
    late = live is not None and not low <= live <= high
    why = (f"cerró en {_fmt(ev['price'])}, {'por encima del máximo' if up else 'por debajo del mínimo'} "
           f"de {p.entry_days} días ({_fmt(ev['level'])})")
    if p.sma_filter and ev.get("sma200"):
        why += f" · {'sobre' if up else 'bajo'} la SMA 200"
    lines = [
        "⏰ *SEÑAL TARDE · NO ENTRAR* ⏰" if late else "🚨 *SEÑAL DE ENTRADA* 🚨",
        f"{NAME} · *{ev['asset']}* — {side}",
        f"💡 {why}",
        "",
        f"🎯 Entrada: *{_fmt(pos['entry'])}*",
        f"🛑 Stop inicial: *{_fmt(pos['stop'])}* (2 ATR) — déjalo puesto como orden stop",
        f"🚪 Salida: cuando un día cierre {'debajo del mínimo' if up else 'encima del máximo'} de "
        f"{p.exit_days} días (hoy: {_fmt(ev['exit_line'])}). Yo aviso.",
        "ℹ️ Sin TP fijo: se deja correr la ganancia. Suele durar de 2 a 6 semanas.",
        "",
        f"📍 Zona de entrada: *{_fmt(low)} – {_fmt(high)}*",
    ]
    if live is not None:
        lines.append(f"⚡ Precio al enviar: {_fmt(live)}")
    lines.append("_El precio ya salió de la zona: no entres._" if late
                 else "_Riesgo sugerido: 1 % de la cuenta. Si el precio sale de la zona, no entres._")
    lines.append(f"_{source}_")
    return "\n".join(lines)


def format_update(ev: dict, tz: str) -> str:
    pos = ev["pos"]
    up = pos["sign"] > 0
    days = (datetime.fromisoformat(ev["day"]) - datetime.fromisoformat(pos["opened"])).days
    return "\n".join([
        f"📌 *SEGUIMIENTO* · {NAME}",
        f"*{ev['asset']}* — {'COMPRA' if up else 'VENTA'} desde el {_day(pos['opened'], tz)} ({days} días)",
        f"💲 Cierre de hoy: {_fmt(ev['price'])}  ·  resultado: *{ev['r']:+.1f} R*",
        f"🚪 Salida si un día cierra {'debajo' if up else 'encima'} de *{_fmt(ev['exit_line'])}*",
        f"🛑 Stop de emergencia: {_fmt(pos['stop'])}",
    ])


def format_exit(ev: dict, tz: str) -> str:
    pos = ev["pos"]
    up = pos["sign"] > 0
    days = (datetime.fromisoformat(ev["day"]) - datetime.fromisoformat(pos["opened"])).days
    icon = "✅" if ev["r"] > 0 else "❌"
    return "\n".join([
        f"🔔 *SALIDA* · {NAME}",
        f"*Cierra la {'COMPRA' if up else 'VENTA'} de {ev['asset']}*",
        f"Motivo: {ev['reason']}",
        f"Entrada {_fmt(pos['entry'])} ({_day(pos['opened'], tz)}) → salida {_fmt(ev['price'])} · {days} días",
        f"{icon} Resultado: *{ev['r']:+.1f} R*",
    ])


def format_initial(asset: str, pos: dict, tz: str) -> str:
    up = pos["sign"] > 0
    return "\n".join([
        f"ℹ️ *ESTADO INICIAL* · {NAME}",
        f"La estrategia ya estaba en {'COMPRA' if up else 'VENTA'} de {asset} desde el "
        f"{_day(pos['opened'], tz)} a {_fmt(pos['entry'])}.",
        "No es una señal nueva: si no la tomaste, espera la próxima. Te aviso cuando toque salir.",
    ])


# --------------------------------------------------------------------------- #
# Proceso diario
# --------------------------------------------------------------------------- #
def process_daily(settings: Settings, state: StateManager, deliver: Callable[[str], dict],
                  dry_run: bool) -> dict:
    """Procesa las velas diarias nuevas de cada activo. Devuelve un resumen."""
    summary: dict = {"events": [], "errors": {}}
    for asset in PARAMS:
        try:
            raw, source = fetch_daily(asset)
        except DataUnavailableError as exc:
            log.error("Tendencia diaria %s: %s", asset, exc)
            summary["errors"][asset] = str(exc)
            continue
        d = indicators(raw, PARAMS[asset])
        last_day = state.get(_key("DAILY", asset)).get("day")
        pos = state.get(_key("POS", asset)).get("pos")
        newest = d.index[-1].isoformat()

        if last_day is None:  # primera vez: reconstruir dónde está la estrategia hoy
            _, pos = replay(raw, asset)
            if pos and not dry_run:
                result = deliver(format_initial(asset, pos, settings.timezone))
                if result.get("delivered_as") in ("console", "error"):
                    continue
            if not dry_run:
                state.put(_key("POS", asset), {"pos": pos})
                state.put(_key("DAILY", asset), {"day": newest})
            summary["events"].append({"asset": asset, "type": "init", "open": bool(pos)})
            continue

        for i in range(len(d)):
            day = d.index[i].isoformat()
            if day <= last_day:
                continue
            pos_after, ev = step(d, i, asset, pos)
            text = None
            if ev and ev["type"] == "entry":
                live = fetch_live_price(asset, source)
                text = format_entry(ev, live, settings.entry_zone_r, source)
            elif ev and ev["type"] == "update":
                text = format_update(ev, settings.timezone)
            elif ev and ev["type"] == "exit":
                text = format_exit(ev, settings.timezone)
            if text:
                result = deliver(text)
                if dry_run:
                    summary["events"].append({"asset": asset, **{k: ev[k] for k in ("type", "day")}})
                    pos = pos_after
                    continue
                if result.get("delivered_as") in ("console", "error"):
                    log.error("Tendencia diaria %s: mensaje no entregado; se reintenta luego.", asset)
                    break
            if dry_run:
                pos = pos_after
                continue
            if ev and ev["type"] == "exit":
                _record_trade(state, ev)
            pos = pos_after
            state.put(_key("POS", asset), {"pos": pos})
            state.put(_key("DAILY", asset), {"day": day})
            if ev:
                summary["events"].append({"asset": asset, "type": ev["type"], "day": day})
    return summary


def _record_trade(state: StateManager, ev: dict) -> None:
    now = datetime.fromisoformat(ev["day"])
    items = [t for t in state.get("_TRADES").get("items", [])
             if now - datetime.fromisoformat(t["closed"]) < TRADES_KEEP]
    pos = ev["pos"]
    items.append({"asset": ev["asset"], "direction": pos["direction"], "entry": pos["entry"],
                  "exit": ev["price"], "opened": pos["opened"], "closed": ev["day"], "r": round(ev["r"], 2)})
    state.put("_TRADES", {"items": items})


# --------------------------------------------------------------------------- #
# Para el mensaje diario y el reporte semanal
# --------------------------------------------------------------------------- #
def open_positions(state: StateManager) -> list[dict]:
    return [p for a in PARAMS if (p := state.get(_key("POS", a)).get("pos"))]


def closed_since(state: StateManager, since: datetime) -> list[dict]:
    return [t for t in state.get("_TRADES").get("items", [])
            if datetime.fromisoformat(t["closed"]) >= since]


def heartbeat_lines(state: StateManager, tz: str) -> list[str]:
    pos = open_positions(state)
    if not pos:
        return [f"{NAME}: sin posiciones abiertas (esperando la próxima tendencia)"]
    return [f"{NAME}: {'COMPRA' if p['sign'] > 0 else 'VENTA'} {_short(p['asset'])} desde el "
            f"{_day(p['opened'], tz)}" for p in pos]


def weekly_lines(state: StateManager, now: datetime, tz: str) -> list[str]:
    closed = closed_since(state, now - timedelta(days=7))
    lines = [f"*{NAME}*"]
    if closed:
        wins = sum(t["r"] > 0 for t in closed)
        lines.append(f"Cerradas: {len(closed)} · ✅ {wins} · ❌ {len(closed) - wins} · "
                     f"resultado *{sum(t['r'] for t in closed):+.1f} R*")
        lines += [f"• {_short(t['asset'])} {t['direction']} {t['r']:+.1f} R" for t in closed]
    else:
        lines.append("Sin operaciones cerradas esta semana.")
    for p in open_positions(state):
        lines.append(f"• Abierta: {'COMPRA' if p['sign'] > 0 else 'VENTA'} {_short(p['asset'])} "
                     f"desde el {_day(p['opened'], tz)} a {_fmt(p['entry'])}")
    return lines


def is_active(settings: Settings) -> bool:
    return "tendencia_diaria" in settings.active_strategies

