from __future__ import annotations

import hmac
import json
import logging
import os
import platform
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from aiohttp import ClientSession, web

from .central import CentralClient, Synchronizer
from .counter import ProductionCounter
from .gpio import GpioService
from .metrics import SystemMetrics
from .models import CounterSettings
from .network import NetworkError, NetworkService
from .settings import SettingsStore
from .store import CounterStore, minute_key, now

APP_KEY = web.AppKey("services", dict)
DATA_DIR_KEY = web.AppKey("data_dir", str)
STATIC = Path(__file__).parent / "static"


def default_data_dir() -> Path:
    configured = os.getenv("CONTADOR_DATA_DIR")
    if configured:
        return Path(configured)
    if platform.system() == "Linux":
        return Path("/var/lib/NodoPIContador")
    return Path.cwd() / "data"


def json_response(value, *, status: int = 200) -> web.Response:
    return web.json_response(value, status=status, dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


def services(request: web.Request) -> dict:
    return request.app[APP_KEY]


async def index(_: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html")


# -- estado -------------------------------------------------------------------
async def api_status(request: web.Request) -> web.Response:
    svc = services(request)
    gpio: GpioService = svc["gpio"]
    return json_response({
        "system": svc["metrics"].read(),
        "gpio": {"available": gpio.available, "mock": gpio.mock, "error": gpio.error,
                 "pins": [asdict(pin) for pin in gpio.pins()]},
    })


async def counter_status(request: web.Request) -> web.Response:
    svc = services(request)
    return json_response({
        **svc["counter"].snapshot(),
        "central": svc["sync"].payload(),
        "configuracion": svc["settings"].current.public_dict(),
    })


async def production(request: web.Request) -> web.Response:
    svc = services(request)
    try:
        hours = max(1, min(24 * 31, int(request.query.get("horas", "24"))))
    except ValueError:
        hours = 24
    svc["counter"].flush()
    store: CounterStore = svc["store"]
    return json_response({
        "horas": store.by_hour(hours),
        "turnos": store.recent_shifts(20),
        "paradas": store.recent_stops(30),
    })


# -- acciones -----------------------------------------------------------------
async def new_shift(request: web.Request) -> web.Response:
    shift = services(request)["counter"].new_shift("manual")
    return json_response({"success": True, "message": f"Turno {shift['id']} iniciado.", "turno": shift})


async def simulate(request: web.Request) -> web.Response:
    svc = services(request)
    if not svc["gpio"].mock:
        return json_response({"success": False, "message": "La simulación solo existe con GPIO simulado."}, status=404)
    try:
        payload = await request.json() if request.can_read_body else {}
        pulses = int(payload.get("pulsos", 1))
        svc["counter"].add_manual(pulses)
    except (ValueError, TypeError) as exc:
        return json_response({"success": False, "message": str(exc)}, status=400)
    return json_response({"success": True, "message": f"{pulses} pulso(s) simulados."})


async def save_settings(request: web.Request) -> web.Response:
    svc = services(request)
    try:
        value = CounterSettings.from_dict(await request.json())
        saved = svc["settings"].save(value)
    except (ValueError, TypeError) as exc:
        return json_response({"success": False, "message": str(exc)}, status=400)
    except OSError as exc:
        logging.getLogger(__name__).exception("No se pudo guardar la configuración")
        return json_response({"success": False, "message": f"No se pudo escribir la configuración: {exc}"}, status=500)
    try:
        svc["counter"].apply_configuration()
    except Exception as exc:
        # La configuración ya quedó guardada: se informa el problema de GPIO
        # aparte en vez de convertir un guardado correcto en un error.
        logging.getLogger(__name__).exception("Configuración guardada, pero no se pudieron aplicar los GPIO")
        return json_response({"success": True, "warning": str(exc),
                              "message": f"Configuración guardada; no se pudieron aplicar los GPIO: {exc}",
                              "configuracion": saved.public_dict()})
    return json_response({"success": True, "message": "Configuración guardada y GPIO aplicados.",
                          "configuracion": saved.public_dict()})


async def test_central(request: web.Request) -> web.Response:
    svc = services(request)
    try:
        payload = await request.json() if request.can_read_body else {}
        settings = CounterSettings.from_dict(payload) if payload else svc["settings"].current
        current = svc["settings"].current
        if settings.central_api_key == "********":
            settings.central_api_key = current.central_api_key
        settings.central_url = settings.central_url.strip().rstrip("/")
        return json_response(await svc["central"].diagnose(settings))
    except (ValueError, TypeError) as exc:
        return json_response({"success": False, "message": str(exc), "checks": []}, status=400)


# -- GPIO manual ----------------------------------------------------------------
def _reserved_pins(settings: CounterSettings) -> set[int]:
    return {settings.sensor_pin, *settings.output_pins} if settings.enabled else set()


async def configure_gpio(request: web.Request) -> web.Response:
    svc = services(request)
    try:
        payload = await request.json()
        bcm, mode = int(payload["bcm"]), str(payload["mode"])
        if bcm in _reserved_pins(svc["settings"].current):
            raise ValueError(f"BCM{bcm} lo usa el contador; cámbielo desde la configuración del contador.")
        svc["gpio"].configure(bcm, mode)
        return json_response({"success": True})
    except (KeyError, ValueError, RuntimeError) as exc:
        return json_response({"success": False, "message": str(exc)}, status=400)
    except Exception as exc:
        logging.getLogger(__name__).exception("No se pudo configurar el GPIO")
        return json_response({"success": False, "message": f"El controlador GPIO rechazó la configuración: {exc}"}, status=503)


async def write_gpio(request: web.Request) -> web.Response:
    svc = services(request)
    try:
        payload = await request.json()
        bcm = int(payload["bcm"])
        if bcm in _reserved_pins(svc["settings"].current):
            raise ValueError(f"BCM{bcm} lo maneja el contador.")
        svc["gpio"].set(bcm, bool(payload["high"]))
        return json_response({"success": True})
    except (KeyError, ValueError, RuntimeError) as exc:
        return json_response({"success": False, "message": str(exc)}, status=400)


# -- consulta externa con clave -----------------------------------------------------
def authorize_query(request: web.Request) -> web.Response | None:
    expected = services(request)["settings"].current.query_key
    if not expected:
        return json_response({"codigo": "SIN_CLAVE_CONSULTA", "message": "Este nodo no tiene clave de consulta configurada."}, status=503)
    if not hmac.compare_digest(request.headers.get("X-Api-Key", ""), expected):
        return json_response({"codigo": "CLAVE_INVALIDA", "message": "Clave de consulta inválida o ausente."}, status=401)
    return None


async def query_status(request: web.Request) -> web.Response:
    rejected = authorize_query(request)
    return rejected or json_response(services(request)["counter"].snapshot())


async def query_minutes(request: web.Request) -> web.Response:
    rejected = authorize_query(request)
    if rejected:
        return rejected
    svc = services(request)
    since = request.query.get("desde") or minute_key(now() - timedelta(hours=1))
    svc["counter"].flush()
    return json_response({"desde": since, "minutos": svc["store"].minutes_since(since[:16])})


# -- red --------------------------------------------------------------------------
def authorize_network(request: web.Request) -> web.Response | None:
    network: NetworkService = services(request)["network"]
    if not network.admin_token:
        return json_response({"success": False, "message": "La administración de red no está habilitada en este nodo."}, status=503)
    if not network.authorized(request.headers.get("X-Network-Token", "")):
        return json_response({"success": False, "message": "Token administrativo inválido."}, status=401)
    return None


async def network_status(request: web.Request) -> web.Response:
    rejected = authorize_network(request)
    if rejected:
        return rejected
    try:
        return json_response(await services(request)["network"].status())
    except NetworkError as exc:
        return json_response({"success": False, "message": str(exc)}, status=503)


async def network_scan(request: web.Request) -> web.Response:
    rejected = authorize_network(request)
    if rejected:
        return rejected
    try:
        return json_response({"networks": await services(request)["network"].scan_wifi()})
    except NetworkError as exc:
        return json_response({"success": False, "message": str(exc)}, status=503)


async def network_wifi(request: web.Request) -> web.Response:
    rejected = authorize_network(request)
    if rejected:
        return rejected
    try:
        payload = await request.json()
        result = await services(request)["network"].connect_wifi(
            str(payload.get("ssid", "")), str(payload.get("password", "")), bool(payload.get("hidden", False))
        )
        return json_response(result, status=202)
    except (NetworkError, TypeError, ValueError) as exc:
        return json_response({"success": False, "message": str(exc)}, status=400)


async def network_ethernet(request: web.Request) -> web.Response:
    rejected = authorize_network(request)
    if rejected:
        return rejected
    try:
        payload = await request.json()
        result = await services(request)["network"].configure_ethernet(
            str(payload.get("method", "auto")), list(payload.get("addresses") or []),
            str(payload.get("gateway", "")), list(payload.get("dns") or []),
        )
        return json_response(result, status=202)
    except (NetworkError, TypeError, ValueError) as exc:
        return json_response({"success": False, "message": str(exc)}, status=400)


async def network_confirm(request: web.Request) -> web.Response:
    rejected = authorize_network(request)
    if rejected:
        return rejected
    try:
        payload = await request.json()
        return json_response(await services(request)["network"].confirm(str(payload.get("changeId", ""))))
    except (NetworkError, TypeError, ValueError) as exc:
        return json_response({"success": False, "message": str(exc)}, status=400)


# -- ciclo de vida ------------------------------------------------------------------
async def app_context(app: web.Application):
    data_dir = Path(app[DATA_DIR_KEY])
    session = ClientSession()
    svc = {
        "settings": SettingsStore(data_dir / "contador-settings.json"),
        "store": CounterStore(data_dir / "contador.db"),
        "gpio": GpioService(data_dir / "gpio-config.json"),
        "metrics": SystemMetrics(),
        "network": NetworkService(),
        "http": session,
    }
    svc["counter"] = ProductionCounter(svc["gpio"], svc["settings"], svc["store"])
    svc["central"] = CentralClient(session)
    svc["sync"] = Synchronizer(svc["settings"], svc["store"], svc["counter"], svc["central"])
    app[APP_KEY] = svc
    await svc["counter"].start()
    await svc["sync"].start()
    yield
    await svc["sync"].stop()
    await svc["counter"].stop()
    await svc["network"].close()
    await session.close()
    svc["gpio"].close()
    svc["store"].close()


def create_app(data_dir: Path | None = None) -> web.Application:
    app = web.Application(client_max_size=512 * 1024)
    app[DATA_DIR_KEY] = str(data_dir or default_data_dir())
    app.cleanup_ctx.append(app_context)
    app.add_routes([
        web.get("/", index),
        web.get("/api/status", api_status),
        web.get("/api/contador", counter_status),
        web.get("/api/contador/produccion", production),
        web.post("/api/contador/turno", new_shift),
        web.post("/api/contador/simular", simulate),
        web.put("/api/contador/configuracion", save_settings),
        web.post("/api/contador/central/probar", test_central),
        web.post("/api/gpio/configure", configure_gpio),
        web.post("/api/gpio/write", write_gpio),
        web.get("/api/consulta/estado", query_status),
        web.get("/api/consulta/minutos", query_minutes),
        web.get("/api/network/status", network_status),
        web.post("/api/network/scan", network_scan),
        web.post("/api/network/wifi", network_wifi),
        web.put("/api/network/ethernet", network_ethernet),
        web.post("/api/network/confirm", network_confirm),
    ])
    app.router.add_static("/static/", STATIC, show_index=False)
    return app
