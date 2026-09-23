import hashlib
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nodocontador.app import APP_KEY, create_app


def test_static_assets_are_cache_busted_by_content():
    """El ?v= debe ser el hash del archivo, para que el navegador no sirva uno viejo.

    Si esta prueba falla, poné en index.html el hash que indica el mensaje.
    """
    static = Path("src/nodocontador/static")
    html = (static / "index.html").read_text("utf-8")
    for name in ("app.css", "app.js"):
        digest = hashlib.sha256((static / name).read_bytes()).hexdigest()[:10]
        assert f"/static/{name}?v={digest}" in html, (
            f"{name} cambió: en index.html poné /static/{name}?v={digest}"
        )


def test_panel_has_the_40_pin_gpio_map():
    """El mapa del conector es la guía para cablear el sensor en la Pi."""
    static = Path("src/nodocontador/static")
    html = (static / "index.html").read_text("utf-8")
    script = (static / "app.js").read_text("utf-8")
    assert 'id="open-gpio-map"' in html and 'id="gpio-map"' in html
    physical = [int(n) for n in re.findall(r"\{physical:(\d+)", script)]
    assert physical == list(range(1, 41))


@pytest.fixture
def mock_gpio(monkeypatch):
    monkeypatch.setenv("CONTADOR_GPIO_MOCK", "1")


SETTINGS = {
    "Habilitado": True, "NombreNodo": "Nodo 1", "NombreLinea": "Envasado",
    "PinSensor": 17, "ModoEntrada": "InputPullUp", "ActivoEnAlto": False,
    "AntirreboteMs": 0, "PulsoMinimoMs": 0, "UnidadesPorPulso": 1,
    "SegundosParada": 60, "MetaTurno": 100, "PinSalidaMeta": 0, "PinSalidaParada": 0,
    "InicioTurnos": ["06:00", "14:00"], "UrlCentral": "", "NodoId": 0,
    "ClaveNodo": "", "ClaveConsulta": "secreta", "SegundosSincronizacion": 30,
}


async def test_dashboard_counts_gpio_pulses(tmp_path, mock_gpio):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        response = await client.put("/api/contador/configuracion", json=SETTINGS)
        assert (await response.json())["success"]

        gpio = client.server.app[APP_KEY]["gpio"]
        for _ in range(3):
            gpio.emit(17, False)
            gpio.emit(17, True)

        data = await (await client.get("/api/contador")).json()
        assert data["turno"]["unidades"] == 3
        assert data["linea"] == "Envasado"
        assert data["sensor"]["modo"] == "InputPullUp"
        # Las claves nunca vuelven al navegador.
        assert data["configuracion"]["ClaveConsulta"] == "********"

        production = await (await client.get("/api/contador/produccion?horas=2")).json()
        assert sum(hour["unidades"] for hour in production["horas"]) == 3

        response = await client.post("/api/contador/turno")
        assert response.status == 200
        data = await (await client.get("/api/contador")).json()
        assert data["turno"]["unidades"] == 0
        assert data["totalAcumulado"] == 3


async def test_masked_keys_are_kept_on_save(tmp_path, mock_gpio):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        await client.put("/api/contador/configuracion", json=SETTINGS)
        await client.put("/api/contador/configuracion", json={**SETTINGS, "ClaveConsulta": "********"})
        assert client.server.app[APP_KEY]["settings"].current.query_key == "secreta"


async def test_invalid_settings_are_rejected(tmp_path, mock_gpio):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        response = await client.put("/api/contador/configuracion", json={**SETTINGS, "PinSalidaParada": 17})
        assert response.status == 400
        response = await client.put("/api/contador/configuracion", json={**SETTINGS, "InicioTurnos": ["25:00"]})
        assert response.status == 400


async def test_query_endpoints_require_the_key(tmp_path, mock_gpio):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        assert (await client.get("/api/consulta/estado")).status == 503
        await client.put("/api/contador/configuracion", json=SETTINGS)
        assert (await client.get("/api/consulta/estado")).status == 401
        response = await client.get("/api/consulta/estado", headers={"X-Api-Key": "secreta"})
        assert response.status == 200
        await client.post("/api/contador/simular", json={"pulsos": 4})
        response = await client.get("/api/consulta/minutos", headers={"X-Api-Key": "secreta"})
        assert sum(item["unidades"] for item in (await response.json())["minutos"]) == 4


async def test_counter_pins_cannot_be_driven_by_hand(tmp_path, mock_gpio):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        await client.put("/api/contador/configuracion", json=SETTINGS)
        response = await client.post("/api/gpio/configure", json={"bcm": 17, "mode": "Output"})
        assert response.status == 400
        response = await client.post("/api/gpio/configure", json={"bcm": 22, "mode": "Output"})
        assert response.status == 200


async def test_sync_delivers_pending_minutes(tmp_path, mock_gpio, monkeypatch):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        svc = client.server.app[APP_KEY]
        await client.put("/api/contador/configuracion", json={
            **SETTINGS, "UrlCentral": "http://central.local", "NodoId": 7, "ClaveNodo": "k"})
        sent = []

        async def fake_send(settings, payload):
            sent.append(payload)
            return True, "Entregado."

        monkeypatch.setattr(svc["central"], "send", fake_send)
        svc["counter"].add_manual(2)
        assert await svc["sync"].sync_once()
        assert sum(item["unidades"] for item in sent[0]["minutos"]) == 2
        assert svc["store"].pending_count == 0
