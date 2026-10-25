"""Punto de entrada sin Firebase: pruebas locales y GitHub Actions.

Ejemplos:
    python run_standalone.py --dry-run                 # analiza y muestra el mensaje, no envía
    python run_standalone.py --dry-run --mode HOURLY_SUMMARY
    python run_standalone.py --test-whatsapp           # envía un mensaje de prueba
    python run_standalone.py --telegram-chat-id        # muestra tu TELEGRAM_CHAT_ID
    python run_standalone.py                           # escaneo real (usa .env)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "functions"))


def load_dotenv(path: Path) -> None:
    """Lector mínimo de .env (KEY=VALUE). No pisa variables ya definidas."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description="Escáner BTC / XAU con alertas por WhatsApp")
    parser.add_argument("--dry-run", action="store_true", help="no envía WhatsApp ni guarda estado")
    parser.add_argument("--mode", choices=["SIGNALS_ONLY", "HOURLY_SUMMARY"], help="sobrescribe ALERT_MODE")
    parser.add_argument("--json", action="store_true", help="imprime el resultado completo en JSON")
    parser.add_argument("--test-whatsapp", "--test-alert", action="store_true",
                        help="envía un mensaje de prueba por el canal configurado y sale")
    parser.add_argument("--telegram-chat-id", action="store_true",
                        help="muestra el chat_id de quien le escribió a tu bot de Telegram")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    os.environ.setdefault("STATE_FILE", str(ROOT / "state" / "last_signals.json"))
    for stream in (sys.stdout, sys.stderr):  # emojis en la consola de Windows
        if stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from scanner.config import load_settings
    from scanner.runner import run_scan
    from scanner.whatsapp_notifier import WhatsAppError, get_notifier

    settings = load_settings()

    if args.telegram_chat_id:
        from scanner.telegram_notifier import TelegramNotifier

        if not settings.telegram_token:
            print("Falta TELEGRAM_BOT_TOKEN en .env")
            return 2
        try:
            chats = TelegramNotifier(settings).recent_chats()
        except (WhatsAppError, OSError) as exc:
            print(f"Error de Telegram: {exc}")
            return 1
        if not chats:
            print("Ningún chat todavía: escríbele cualquier cosa a tu bot en Telegram y repite.")
            return 1
        for c in chats:
            print(f"TELEGRAM_CHAT_ID={c['chat_id']}   ({c['nombre']} @{c['usuario']}: {c['ultimo_mensaje']!r})")
        return 0

    if args.test_whatsapp:
        if not settings.notifier_configured:
            missing = {
                "telegram": "TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID",
                "callmebot": "CALLMEBOT_APIKEY o WHATSAPP_TO",
                "meta": "WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID o WHATSAPP_TO",
            }[settings.provider]
            print(f"Faltan {missing} en .env")
            return 2
        try:
            print(f"Canal: {settings.provider}")
            res = get_notifier(settings).notify("✅ *Bot escáner BTC/XAU*: conexión de alertas OK.")
        except (WhatsAppError, OSError) as exc:  # OSError incluye fallos de red de requests
            print(f"Error de {settings.provider}: {exc}\n{json.dumps(getattr(exc, 'payload', {}), indent=2)}")
            return 1
        print(f"Enviado como {res['delivered_as']}: {json.dumps(res['response'])}")
        return 0

    result = run_scan(settings, dry_run=args.dry_run or None, mode=args.mode)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print("\n=== Resumen ===")
        for a in result["analyses"]:
            print(f"{a['asset']:8} {a['price']:>12,.2f}  24h {a['change_24h_pct']:+6.2f}%  "
                  f"pos {a['range_position_pct']:5.1f}%  RSI {a['rsi_1h']:5.1f}  "
                  f"LONG {a['long_score']:5.1f}  SHORT {a['short_score']:5.1f}  -> {a['direction']}"
                  f"  [{a['source']}]")
        if not result["gold_market_open"]:
            print("XAU/USD  mercado cerrado (omitido)")
        for asset, err in result["errors"].items():
            print(f"{asset}: ERROR {err}")
        print(f"Señales nuevas: {result['new_signals'] or 'ninguna'}")

    # Falla el job de GitHub solo si TODOS los activos fallaron
    return 1 if result["errors"] and not result["analyses"] else 0


if __name__ == "__main__":
    sys.exit(main())
