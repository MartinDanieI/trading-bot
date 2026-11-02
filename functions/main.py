"""Firebase Cloud Functions (2nd Gen, Python).

- hourly_scanner : se ejecuta cada hora en punto (America/Bogota) vía Cloud Scheduler.
- manual_scan    : endpoint HTTP para pruebas manuales, protegido con MANUAL_TRIGGER_KEY.
                   GET/POST https://<region>-<proyecto>.cloudfunctions.net/manual_scan
                   Cabecera "X-Trigger-Key: <clave>" (o ?key=<clave>)
                   Parámetro opcional: ?dry_run=1
"""

from __future__ import annotations

import hmac
import json
import logging
import os

from firebase_admin import initialize_app
from firebase_functions import https_fn, options, scheduler_fn
from firebase_functions.params import SecretParam

from scanner.engine import run_scan

initialize_app()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# Secretos en Google Secret Manager (firebase functions:secrets:set NOMBRE).
# En tiempo de ejecución quedan disponibles como variables de entorno.
WHATSAPP_TOKEN = SecretParam("WHATSAPP_TOKEN")
TELEGRAM_BOT_TOKEN = SecretParam("TELEGRAM_BOT_TOKEN")
MANUAL_TRIGGER_KEY = SecretParam("MANUAL_TRIGGER_KEY")

options.set_global_options(
    region="us-central1",
    memory=options.MemoryOption.MB_512,
    timeout_sec=300,
    max_instances=1,  # nunca más de una instancia: evita costos y envíos dobles
)


@scheduler_fn.on_schedule(
    schedule="0 * * * *",
    timezone=scheduler_fn.Timezone("America/Bogota"),
    secrets=[WHATSAPP_TOKEN, TELEGRAM_BOT_TOKEN],
)
def hourly_scanner(event: scheduler_fn.ScheduledEvent) -> None:
    result = run_scan()
    logging.info("Resultado: %s", json.dumps(
        {k: result[k] for k in ("signals", "daily", "errors")}, ensure_ascii=False, default=str))


@https_fn.on_request(secrets=[WHATSAPP_TOKEN, TELEGRAM_BOT_TOKEN, MANUAL_TRIGGER_KEY])
def manual_scan(req: https_fn.Request) -> https_fn.Response:
    expected = os.getenv("MANUAL_TRIGGER_KEY", "")
    given = req.headers.get("X-Trigger-Key") or req.args.get("key", "")
    if not expected or not hmac.compare_digest(given, expected):
        return https_fn.Response("No autorizado", status=401)

    dry_run = req.args.get("dry_run", "").lower() in ("1", "true", "yes")
    result = run_scan(dry_run=dry_run)
    return https_fn.Response(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        status=200,
        content_type="application/json; charset=utf-8",
    )
