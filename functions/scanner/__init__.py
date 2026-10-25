"""Escáner de señales para BTC/USD y XAU/USD (Oro).

Módulos:
    config            -> lectura de variables de entorno.
    data_fetcher      -> descarga de velas con fallbacks (Kraken / Yahoo Finance).
    strategy          -> indicadores, motor de confluencia y niveles operativos.
    state_manager     -> anti-spam (Firestore o JSON local).
    whatsapp_notifier -> cliente de Meta WhatsApp Cloud API.
    runner            -> orquestación de un escaneo completo.
"""

__version__ = "1.0.0"
