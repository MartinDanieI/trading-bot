"""Configuración centralizada leída de variables de entorno.

Se lee en cada escaneo (no al importar) para que los cambios de entorno
en pruebas locales o en Firebase tengan efecto sin reiniciar el proceso.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

VALID_MODES = ("SIGNALS_ONLY", "HOURLY_SUMMARY")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Canal de alertas: telegram | callmebot | meta (auto -> el que tenga credenciales)
    provider: str
    callmebot_apikey: str
    telegram_token: str
    telegram_chat_id: str

    # WhatsApp (Meta Cloud API)
    whatsapp_token: str
    phone_number_id: str
    whatsapp_to: str
    graph_api_version: str
    template_name: str
    template_lang: str
    template_has_body_param: bool
    always_template: bool

    # Estrategia / notificaciones
    alert_mode: str
    score_threshold: float
    cooldown_hours: float
    heartbeat_hour: int  # hora local del "sigo vivo" diario; -1 lo desactiva
    timezone: str

    # Estado
    state_backend: str  # auto | firestore | json
    state_file: str

    # Ejecución
    dry_run: bool

    @property
    def telegram_chat_ids(self) -> list[str]:
        """TELEGRAM_CHAT_ID admite varios destinatarios separados por comas."""
        return [c.strip() for c in self.telegram_chat_id.split(",") if c.strip()]

    @property
    def notifier_configured(self) -> bool:
        if self.provider == "telegram":
            return bool(self.telegram_token and self.telegram_chat_ids)
        if not self.whatsapp_to:
            return False
        if self.provider == "callmebot":
            return bool(self.callmebot_apikey)
        return bool(self.whatsapp_token and self.phone_number_id)


def load_settings() -> Settings:
    mode = os.getenv("ALERT_MODE", "SIGNALS_ONLY").strip().upper()
    if mode not in VALID_MODES:
        mode = "SIGNALS_ONLY"

    callmebot_apikey = os.getenv("CALLMEBOT_APIKEY", "").strip()
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    # ALERT_PROVIDER; WHATSAPP_PROVIDER se acepta por compatibilidad
    provider = (os.getenv("ALERT_PROVIDER") or os.getenv("WHATSAPP_PROVIDER") or "auto").strip().lower()
    if provider not in ("telegram", "callmebot", "meta"):
        provider = "telegram" if telegram_token else "callmebot" if callmebot_apikey else "meta"

    return Settings(
        provider=provider,
        callmebot_apikey=callmebot_apikey,
        telegram_token=telegram_token,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        whatsapp_token=os.getenv("WHATSAPP_TOKEN", "").strip(),
        phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        # Número destino en formato internacional sin "+" ni espacios, ej. 573001234567
        whatsapp_to=os.getenv("WHATSAPP_TO", "").strip().lstrip("+").replace(" ", ""),
        graph_api_version=os.getenv("GRAPH_API_VERSION", "v25.0").strip(),
        template_name=os.getenv("WHATSAPP_TEMPLATE_NAME", "hello_world").strip(),
        template_lang=os.getenv("WHATSAPP_TEMPLATE_LANG", "en_US").strip(),
        template_has_body_param=_bool("WHATSAPP_TEMPLATE_HAS_BODY_PARAM", False),
        always_template=_bool("WHATSAPP_ALWAYS_TEMPLATE", False),
        alert_mode=mode,
        score_threshold=_float("SCORE_THRESHOLD", 70.0),
        cooldown_hours=_float("COOLDOWN_HOURS", 4.0),
        heartbeat_hour=int(_float("HEARTBEAT_HOUR", 8)),
        timezone=os.getenv("REPORT_TIMEZONE", "America/Bogota").strip(),
        state_backend=os.getenv("STATE_BACKEND", "auto").strip().lower(),
        state_file=os.getenv("STATE_FILE", "state/last_signals.json").strip(),
        dry_run=_bool("DRY_RUN", False),
    )


def running_on_gcp() -> bool:
    """True cuando el código corre dentro de Cloud Functions / Cloud Run."""
    return bool(os.getenv("K_SERVICE") or os.getenv("FUNCTION_TARGET"))
