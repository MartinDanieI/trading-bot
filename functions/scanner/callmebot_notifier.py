"""Envío por WhatsApp con CallMeBot (https://www.callmebot.com).

Alternativa gratuita a la Cloud API de Meta para mandarte mensajes a TU propio
número: no necesita app de Meta, plantillas ni ventana de 24 horas.

Activación (una sola vez):
    1. Busca el número actual de CallMeBot en
       https://www.callmebot.com/blog/free-api-whatsapp-messages/ y agrégalo a tus contactos.
    2. Envíale por WhatsApp:  I allow callmebot to send me messages
    3. Te responde con tu API key -> CALLMEBOT_APIKEY en el .env.

Es un servicio de terceros no oficial y con límites de uso: pensado para pocas
alertas personales, que es justo el caso de este bot.
"""

from __future__ import annotations

import logging
import re
import time

import requests

from .config import Settings
from .whatsapp_notifier import WhatsAppError

log = logging.getLogger(__name__)

API_URL = "https://api.callmebot.com/whatsapp.php"
MIN_INTERVAL = 5.0  # segundos entre mensajes para respetar el límite del servicio
_last_sent = 0.0


class CallMeBotNotifier:
    def __init__(self, settings: Settings, timeout: float = 30.0) -> None:
        self.s = settings
        self.timeout = timeout

    def notify(self, text: str) -> dict:
        global _last_sent
        wait = MIN_INTERVAL - (time.monotonic() - _last_sent)
        if wait > 0:
            time.sleep(wait)

        resp = requests.get(
            API_URL,
            params={"phone": f"+{self.s.whatsapp_to}", "text": text, "apikey": self.s.callmebot_apikey},
            timeout=self.timeout,
        )
        _last_sent = time.monotonic()
        body = resp.text.strip()
        lowered = body.lower()
        if not resp.ok or "error" in lowered or "invalid" in lowered:
            raise WhatsAppError(f"CallMeBot HTTP {resp.status_code}: {_strip_html(body)[:300]}",
                                payload={"raw": body[:1000]})
        log.info("CallMeBot: %s", _strip_html(body)[:120])
        return {"delivered_as": "callmebot", "response": {"raw": _strip_html(body)[:300]}}


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
