from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiohttp import ClientSession, ClientTimeout

from .counter import ProductionCounter
from .errors import reason
from .models import CounterSettings
from .settings import SettingsStore
from .store import CounterStore

log = logging.getLogger(__name__)


class CentralClient:
    """Entrega opcional del conteo a una aplicación central.

    Contrato (ver ``docs/CONTRATO_CENTRAL.md``):
    ``POST {UrlCentral}/api/contador/{NodoId}/conteo`` con ``X-Api-Key``.
    """

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    @staticmethod
    def _url(settings: CounterSettings, suffix: str) -> str:
        return f"{settings.central_url}/api/contador/{settings.node_id}/{suffix}"

    @staticmethod
    def _headers(settings: CounterSettings) -> dict[str, str]:
        return {"X-Api-Key": settings.central_api_key} if settings.central_api_key else {}

    async def send(self, settings: CounterSettings, payload: dict) -> tuple[bool, str]:
        try:
            async with self.session.post(
                self._url(settings, "conteo"), json=payload, headers=self._headers(settings),
                timeout=ClientTimeout(total=15),
            ) as response:
                body = (await response.text()).strip()
                if response.status >= 400:
                    return False, f"HTTP {response.status}: {body[:300]}"
                return True, "Entregado."
        except Exception as exc:
            return False, reason(exc)

    async def diagnose(self, settings: CounterSettings) -> dict:
        checks = [
            {"name": "URL central", "ok": bool(settings.central_url), "detail": settings.central_url or "No configurada."},
            {"name": "NodoId", "ok": settings.node_id > 0,
             "detail": str(settings.node_id) if settings.node_id > 0 else "No configurado."},
            {"name": "Clave del nodo", "ok": bool(settings.central_api_key),
             "detail": "Configurada." if settings.central_api_key else "No configurada."},
        ]
        if not all(item["ok"] for item in checks):
            return {"success": False, "message": "Falta completar la configuración central.", "checks": checks}
        # Un envío vacío es idempotente: sirve de prueba sin inventar conteos.
        ok, message = await self.send(settings, {"nodoId": settings.node_id, "minutos": [], "prueba": True})
        checks.append({"name": "Entrega", "ok": ok, "detail": "La central aceptó el envío." if ok else message})
        return {"success": ok, "message": "La central respondió correctamente." if ok else "La central no aceptó el envío.",
                "checks": checks}


class Synchronizer:
    def __init__(self, settings: SettingsStore, store: CounterStore, counter: ProductionCounter,
                 central: CentralClient) -> None:
        self.settings = settings
        self.store = store
        self.counter = counter
        self.central = central
        self.status = "Central no configurada."
        self.last_delivery: datetime | None = None
        self._task: asyncio.Task | None = None

    async def sync_once(self) -> bool:
        settings = self.settings.current
        if not settings.central_configured:
            self.status = "Central no configurada."
            return False
        # Lo contado en memoria se vuelca antes para no mandar un minuto a medias
        # que en seguida habría que reenviar.
        self.counter.flush()
        minutes = self.store.pending_minutes()
        payload = {
            "nodoId": settings.node_id,
            "nodo": settings.node_name,
            "linea": settings.line_name,
            "estado": self.counter.snapshot(),
            "minutos": minutes,
            "turnos": self.store.recent_shifts(5),
        }
        ok, message = await self.central.send(settings, payload)
        if ok:
            self.store.mark_delivered(minutes)
            self.last_delivery = datetime.now().astimezone()
            self.status = f"Entregados {len(minutes)} minuto(s)."
        else:
            self.status = f"No se pudo entregar: {message}"
            log.warning("Central: %s", message)
        return ok

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.sync_once()
            except Exception as exc:
                self.status = f"Error de sincronización: {reason(exc)}"
                log.exception("Fallo en la sincronización")
            await asyncio.sleep(max(5, self.settings.current.sync_seconds))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def payload(self) -> dict:
        return {
            "configurada": self.settings.current.central_configured,
            "estado": self.status,
            "ultimaEntrega": self.last_delivery.isoformat() if self.last_delivery else None,
            "minutosPendientes": self.store.pending_count,
        }

