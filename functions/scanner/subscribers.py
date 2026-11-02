"""Quién recibe qué: personas aprobadas y las estrategias/activos que eligieron.

La lista vive en el bot de Telegram (Cloudflare Worker, carpeta worker/), donde
cada persona elige de 1 a 4 estrategias. El escáner la pide en cada ejecución:

    GET {WORKER_URL}/api/subscribers   (Authorization: Bearer WORKER_API_KEY)

Si el Worker no responde se usa la última lista guardada en el estado, y si
nunca hubo una, los TELEGRAM_CHAT_ID con la estrategia principal.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import requests

from .config import Settings
from .state_manager import StateManager
from .strategies import CATALOG

log = logging.getLogger(__name__)

ASSETS = ("BTC/USD", "XAU/USD")
DEFAULT_STRATEGY = "tendencia_diaria"


@dataclass
class Subscriber:
    id: str
    name: str
    strategies: list[str]
    assets: list[str]


def _clean(raw: dict) -> Subscriber | None:
    strategies = [s for s in raw.get("strategies", []) if s in CATALOG][:4]
    assets = [a for a in raw.get("assets", ASSETS) if a in ASSETS]
    if not raw.get("id") or not strategies or not assets:
        return None
    return Subscriber(str(raw["id"]), raw.get("name", ""), strategies, assets)


def load(settings: Settings, state: StateManager) -> list[Subscriber]:
    if settings.worker_url and settings.worker_api_key:
        try:
            resp = requests.get(f"{settings.worker_url.rstrip('/')}/api/subscribers",
                                headers={"Authorization": f"Bearer {settings.worker_api_key}"}, timeout=15)
            resp.raise_for_status()
            subs = [s for r in resp.json().get("subscribers", []) if (s := _clean(r))]
            state.put("_SUBS", {"items": [asdict(s) for s in subs]})
            return subs
        except (requests.RequestException, ValueError) as exc:
            log.error("No se pudo leer la lista de personas del bot (%s); uso la última guardada.", exc)
    saved = state.get("_SUBS").get("items")
    if saved is not None:
        return [s for r in saved if (s := _clean(r))]
    return [Subscriber(cid, "", [DEFAULT_STRATEGY], list(ASSETS)) for cid in settings.telegram_chat_ids]


def recipients(subs: list[Subscriber], strategy: str, asset: str) -> list[str]:
    return [s.id for s in subs if strategy in s.strategies and asset in s.assets]


def mask(chat_id: str) -> str:
    """Para los logs (el repo es público): solo los 3 últimos dígitos."""
    return f"…{str(chat_id)[-3:]}"
