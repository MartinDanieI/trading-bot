"""Envío de alertas por Telegram (Bot API oficial, gratis, sin ventanas de 24h).

Configuración:
    1. En Telegram habla con @BotFather -> /newbot -> te da el token
       (TELEGRAM_BOT_TOKEN, formato 123456789:ABC...).
    2. Escríbele cualquier cosa a tu bot nuevo (Telegram no deja que un bot
       inicie la conversación).
    3. `python run_standalone.py --telegram-chat-id` muestra tu chat_id
       (TELEGRAM_CHAT_ID).

Los mensajes usan *negrita* y _cursiva_ igual que WhatsApp (Markdown clásico de
Telegram). Si algún carácter rompe el formato, se reenvía como texto plano.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import Settings
from .whatsapp_notifier import WhatsAppError

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
TEXT_LIMIT = 4096


class TelegramNotifier:
    def __init__(self, settings: Settings, timeout: float = 20.0) -> None:
        self.s = settings
        self.timeout = timeout

    def _call(self, method: str, payload: dict | None = None, attempts: int = 4) -> dict:
        url = API.format(token=self.s.telegram_token, method=method)
        for i in range(1, attempts + 1):
            try:
                resp = requests.post(url, json=payload or {}, timeout=self.timeout)
                break
            except requests.ConnectionError as exc:  # cortes de red intermitentes
                if i == attempts:
                    # sin el token en el mensaje de error
                    raise WhatsAppError(f"Telegram sin conexión tras {attempts} intentos: "
                                        f"{type(exc).__name__}") from None
                log.warning("Telegram: conexión cortada (intento %d/%d), reintentando…", i, attempts)
                time.sleep(2 * i)
        try:
            body = resp.json()
        except ValueError:
            body = {"ok": False, "description": resp.text[:300]}
        if not body.get("ok"):
            raise WhatsAppError(f"Telegram HTTP {resp.status_code}: {body.get('description')}",
                                code=body.get("error_code"), payload=body)
        return body

    def _send(self, chat_id: str, text: str) -> int:
        payload = {
            "chat_id": chat_id,
            "text": text[:TEXT_LIMIT],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            body = self._call("sendMessage", payload)
        except WhatsAppError as exc:
            if "parse entities" not in str(exc):
                raise
            log.warning("Formato Markdown rechazado; reenviando como texto plano.")
            payload.pop("parse_mode")
            body = self._call("sendMessage", payload)
        return body["result"]["message_id"]

    def notify(self, text: str, chat_ids: list[str] | None = None) -> dict:
        """Envía a los chats indicados (por defecto, los de TELEGRAM_CHAT_ID).

        Basta con que llegue a uno para darlo por enviado; los fallos se registran.
        """
        sent: dict[str, int] = {}
        failed: dict[str, str] = {}
        for chat_id in (chat_ids if chat_ids is not None else self.s.telegram_chat_ids):
            try:
                sent[chat_id] = self._send(chat_id, text)
            except WhatsAppError as exc:
                # el repo es público: en los logs solo los últimos dígitos del chat
                log.error("Telegram: no se pudo enviar al chat …%s: %s", str(chat_id)[-3:], exc)
                failed[chat_id] = str(exc)
        if not sent:
            raise WhatsAppError(f"Telegram: ningún chat recibió la alerta ({failed})")
        return {"delivered_as": "telegram", "response": {"enviados": len(sent), "fallidos": len(failed)}}

    def recent_chats(self) -> list[dict]:
        """Chats que le escribieron al bot recientemente (para descubrir el chat_id)."""
        chats: dict[int, dict] = {}
        for update in self._call("getUpdates").get("result", []):
            msg = update.get("message") or update.get("edited_message") or {}
            chat = msg.get("chat")
            if chat:
                chats[chat["id"]] = {
                    "chat_id": chat["id"],
                    "nombre": chat.get("first_name") or chat.get("title") or "",
                    "usuario": chat.get("username", ""),
                    "ultimo_mensaje": msg.get("text", ""),
                }
        return list(chats.values())
