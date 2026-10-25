"""Cliente mínimo de WhatsApp Cloud API (Meta Graph API).

Ventana de 24 horas (por qué existe el fallback a plantilla):
    WhatsApp solo permite enviar mensajes de TEXTO LIBRE a un número que te haya
    escrito en las últimas 24 horas ("customer service window"). Fuera de esa
    ventana la API responde con el error 131047 ("Re-engagement message") y el
    mensaje no se entrega. En ese caso solo se pueden enviar PLANTILLAS
    (templates) aprobadas por Meta.

    Estrategia de este módulo:
      1. Se intenta enviar el texto enriquecido.
      2. Si la API devuelve 131047 (o 131026/470, ventana cerrada), se envía la
         plantilla configurada en WHATSAPP_TEMPLATE_NAME:
           - "hello_world" (viene aprobada en toda cuenta nueva, sin variables):
             sirve como "timbre". Cuando respondas cualquier cosa a ese mensaje
             se abre otra ventana de 24h y las siguientes alertas llegan completas.
           - Una plantilla propia con UNA variable en el cuerpo ({{1}}) y
             WHATSAPP_TEMPLATE_HAS_BODY_PARAM=true: el resumen de la señal viaja
             dentro de la plantilla (en una sola línea, máx. 1024 caracteres,
             porque Meta no admite saltos de línea en variables).

    Consejo: responde "ok" al bot una vez al día para mantener la ventana abierta.
"""

from __future__ import annotations

import logging
import re

import requests

from .config import Settings

log = logging.getLogger(__name__)

WINDOW_CLOSED_CODES = {131047, 131026, 470}
TEXT_LIMIT = 4096
TEMPLATE_PARAM_LIMIT = 1024


class WhatsAppError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class WhatsAppNotifier:
    def __init__(self, settings: Settings, timeout: float = 20.0) -> None:
        self.s = settings
        self.timeout = timeout
        self.url = (
            f"https://graph.facebook.com/{settings.graph_api_version}/"
            f"{settings.phone_number_id}/messages"
        )

    # ------------------------------------------------------------------ #
    def _post(self, payload: dict) -> dict:
        resp = requests.post(
            self.url,
            headers={
                "Authorization": f"Bearer {self.s.whatsapp_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if resp.ok and "error" not in body:
            return body
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise WhatsAppError(
            f"HTTP {resp.status_code}: {err.get('message', resp.text)[:300]}",
            code=err.get("code"),
            payload=body,
        )

    def send_text(self, text: str) -> dict:
        return self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.s.whatsapp_to,
            "type": "text",
            "text": {"preview_url": False, "body": text[:TEXT_LIMIT]},
        })

    def send_template(self, summary: str | None = None) -> dict:
        template: dict = {"name": self.s.template_name, "language": {"code": self.s.template_lang}}
        if self.s.template_has_body_param and summary:
            # Meta rechaza variables con saltos de línea, tabs o >4 espacios seguidos
            flat = re.sub(r"\s+", " ", summary.replace("\n", " · ")).strip()
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": flat[:TEMPLATE_PARAM_LIMIT]}],
            }]
        return self._post({
            "messaging_product": "whatsapp",
            "to": self.s.whatsapp_to,
            "type": "template",
            "template": template,
        })

    def notify(self, text: str) -> dict:
        """Envía texto; si la ventana de 24h está cerrada, cae a plantilla.

        Devuelve {"delivered_as": "text"|"template", "response": {...}}.
        """
        if self.s.always_template:
            return {"delivered_as": "template", "response": self.send_template(text)}
        try:
            return {"delivered_as": "text", "response": self.send_text(text)}
        except WhatsAppError as exc:
            if exc.code not in WINDOW_CLOSED_CODES:
                raise
            log.warning("Ventana de 24h cerrada (código %s); enviando plantilla '%s'.",
                        exc.code, self.s.template_name)
        # Ojo: según la cuenta, Meta puede aceptar el texto (HTTP 200) y reportar el
        # 131047 después, de forma asíncrona, por webhook. Para máxima fiabilidad usa
        # una plantilla propia con variable y WHATSAPP_ALWAYS_TEMPLATE=true.
        return {"delivered_as": "template", "response": self.send_template(text)}


def get_notifier(settings: Settings):
    """Devuelve el notificador del canal configurado (ALERT_PROVIDER)."""
    if settings.provider == "telegram":
        from .telegram_notifier import TelegramNotifier

        return TelegramNotifier(settings)
    if settings.provider == "callmebot":
        from .callmebot_notifier import CallMeBotNotifier

        return CallMeBotNotifier(settings)
    return WhatsAppNotifier(settings)
