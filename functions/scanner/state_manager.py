"""Control anti-spam: recuerda la última señal enviada por activo.

Backends:
    firestore -> colección `scanner_state`, un documento por activo (BTC_USD, XAU_USD).
    json      -> archivo local (por defecto state/last_signals.json). En GitHub
                 Actions este archivo se conserva entre ejecuciones con actions/cache.
    auto      -> firestore si corre en Cloud Functions, json en cualquier otro caso.

Regla: no se repite la MISMA dirección para un activo si la anterior se envió
hace menos de COOLDOWN_HOURS (4h por defecto). Un cambio de dirección
(LONG -> SHORT) se notifica siempre.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .config import Settings, running_on_gcp

log = logging.getLogger(__name__)

COLLECTION = "scanner_state"


def _doc_id(asset: str) -> str:
    return asset.replace("/", "_")


class _Backend(Protocol):
    def get(self, asset: str) -> dict | None: ...
    def put(self, asset: str, record: dict) -> None: ...


class FirestoreBackend:
    def __init__(self) -> None:
        import firebase_admin
        from firebase_admin import firestore

        if not firebase_admin._apps:  # noqa: SLF001 - API oficial no expone otra forma
            firebase_admin.initialize_app()
        self._col = firestore.client().collection(COLLECTION)

    def get(self, asset: str) -> dict | None:
        snap = self._col.document(_doc_id(asset)).get()
        return snap.to_dict() if snap.exists else None

    def put(self, asset: str, record: dict) -> None:
        self._col.document(_doc_id(asset)).set(record)


class JsonBackend:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Estado JSON ilegible (%s); se reinicia.", exc)
            return {}

    def get(self, asset: str) -> dict | None:
        return self._load().get(_doc_id(asset))

    def put(self, asset: str, record: dict) -> None:
        data = self._load()
        data[_doc_id(asset)] = record
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)


class StateManager:
    def __init__(self, settings: Settings) -> None:
        self.cooldown = timedelta(hours=settings.cooldown_hours)
        backend = settings.state_backend
        if backend == "auto":
            backend = "firestore" if running_on_gcp() else "json"
        if backend == "firestore":
            try:
                self._backend: _Backend = FirestoreBackend()
                self.name = "firestore"
                return
            except Exception as exc:  # noqa: BLE001
                log.error("Firestore no disponible (%s); usando JSON local.", exc)
        self._backend = JsonBackend(settings.state_file)
        self.name = f"json:{settings.state_file}"

    def last_signal(self, asset: str) -> dict | None:
        try:
            return self._backend.get(asset)
        except Exception as exc:  # noqa: BLE001 - un fallo de estado no debe frenar el escaneo
            log.error("No se pudo leer el estado de %s: %s", asset, exc)
            return None

    def is_duplicate(self, asset: str, direction: str, now: datetime | None = None) -> bool:
        """True si ya se envió esta misma dirección dentro de la ventana de cooldown."""
        last = self.last_signal(asset)
        if not last or last.get("direction") != direction:
            return False
        try:
            sent_at = datetime.fromisoformat(last["sent_at"])
        except (KeyError, ValueError):
            return False
        now = now or datetime.now(timezone.utc)
        return now - sent_at < self.cooldown

    def record_signal(self, asset: str, direction: str, score: float, price: float,
                      now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        record = {
            "asset": asset,
            "direction": direction,
            "score": score,
            "price": price,
            "sent_at": now.isoformat(),
        }
        try:
            self._backend.put(asset, record)
        except Exception as exc:  # noqa: BLE001
            log.error("No se pudo guardar el estado de %s: %s", asset, exc)
