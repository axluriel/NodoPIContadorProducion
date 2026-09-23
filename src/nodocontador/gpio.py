from __future__ import annotations

import json
import logging
import os
import platform
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


PHYSICAL_PINS = {
    4: 7, 17: 11, 18: 12, 27: 13, 22: 15, 23: 16, 24: 18, 25: 22,
    5: 29, 6: 31, 12: 32, 13: 33, 19: 35, 16: 36, 26: 37, 20: 38, 21: 40,
}

INPUT_MODES = {"Input", "InputPullUp", "InputPullDown"}


@dataclass(slots=True, frozen=True)
class PinState:
    bcm: int
    physical: int
    mode: str
    configured: bool
    high: bool


class GpioService:
    """E/S digital de la Zero W.

    Los callbacks reciben siempre el nivel **eléctrico** del pin (alto o bajo),
    no el «activo» de gpiozero: quien decide qué nivel significa «hay pieza»
    es el contador, según cómo esté cableado el sensor.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self._lock = threading.RLock()
        self._modes: dict[int, str] = {}
        self._values: dict[int, bool] = {}
        self.available = False
        self.error: str | None = None
        self.mock = os.getenv("CONTADOR_GPIO_MOCK") == "1" or platform.system() != "Linux"
        self._devices: dict[int, object] = {}
        self._callbacks: list[Callable[[int, bool], None]] = []
        if self.mock:
            self.available = True
        else:
            try:
                from gpiozero import Device

                # Fuerza la creación del backend al arrancar. Importar gpiozero
                # por sí solo no detecta un backend incompatible y el error
                # aparecería después como un HTTP 500 al configurar el pin.
                Device.ensure_pin_factory()
                self.available = True
            except Exception as exc:
                self.error = str(exc)
                logging.getLogger(__name__).exception("No se pudo iniciar el controlador GPIO")
        if self.available:
            saved: dict[int, str] = {}
            try:
                if config_path and config_path.exists():
                    saved = {int(key): str(value) for key, value in json.loads(config_path.read_text("utf-8")).items()}
            except Exception:
                logging.getLogger(__name__).exception("No se pudo leer la configuración GPIO")
            for bcm, mode in saved.items():
                if bcm in PHYSICAL_PINS:
                    try:
                        self.configure(bcm, mode)
                    except Exception:
                        logging.getLogger(__name__).exception("No se pudo restaurar BCM%s", bcm)

    def mode(self, bcm: int) -> str:
        with self._lock:
            return self._modes.get(bcm, "Disabled")

    def configure(self, bcm: int, mode: str) -> None:
        if bcm not in PHYSICAL_PINS:
            raise ValueError("Ese GPIO está reservado o no existe en el conector.")
        if mode not in INPUT_MODES | {"Output", "Disabled"}:
            raise ValueError("Modo GPIO no válido.")
        if not self.available:
            raise RuntimeError(f"Controlador GPIO no disponible: {self.error}")
        with self._lock:
            device = self._devices.pop(bcm, None)
            if device is not None:
                device.close()
            self._modes.pop(bcm, None)
            self._values.pop(bcm, None)
            if mode == "Disabled":
                self._save_configuration()
                return
            if not self.mock:
                from gpiozero import DigitalInputDevice, DigitalOutputDevice

                if mode == "Output":
                    device = DigitalOutputDevice(bcm, initial_value=False)
                else:
                    pull = {"InputPullUp": True, "InputPullDown": False}.get(mode)
                    # Sin resistencia interna gpiozero exige declarar qué nivel
                    # es «activo»; se toma alto para que activo == nivel alto.
                    device = DigitalInputDevice(bcm, pull_up=pull, active_state=True if pull is None else None)
                    # Con pull-up el dispositivo está activo cuando el pin cae a
                    # BAJO; en los demás modos, cuando sube a ALTO.
                    active_high = mode != "InputPullUp"
                    device.when_activated = lambda pin=bcm, level=active_high: self._notify(pin, level)
                    device.when_deactivated = lambda pin=bcm, level=not active_high: self._notify(pin, level)
                self._devices[bcm] = device
            self._modes[bcm] = mode
            # Una entrada pull-up permanece eléctricamente ALTA en reposo.
            self._values[bcm] = mode == "InputPullUp"
            self._save_configuration()

    def add_callback(self, callback: Callable[[int, bool], None]) -> None:
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[int, bool], None]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def emit(self, bcm: int, high: bool) -> None:
        """Inyecta un flanco en modo simulado para pruebas."""
        if not self.mock:
            raise RuntimeError("emit solamente está disponible con GPIO simulado.")
        self._values[bcm] = high
        self._notify(bcm, high)

    def _notify(self, bcm: int, high: bool) -> None:
        self._values[bcm] = high
        for callback in tuple(self._callbacks):
            try:
                callback(bcm, high)
            except Exception:
                logging.getLogger(__name__).exception("Error atendiendo el flanco de BCM%s", bcm)

    def _save_configuration(self) -> None:
        if not self.config_path:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._modes, indent=2), "utf-8")
        temporary.replace(self.config_path)

    def set(self, bcm: int, high: bool) -> None:
        with self._lock:
            if self._modes.get(bcm) != "Output":
                raise RuntimeError(f"BCM{bcm} no está configurado como salida.")
            if not self.mock:
                self._devices[bcm].on() if high else self._devices[bcm].off()
            self._values[bcm] = high

    def read(self, bcm: int) -> bool | None:
        """Nivel eléctrico actual, o None si el pin no está configurado."""
        with self._lock:
            mode = self._modes.get(bcm)
            if mode is None:
                return None
            if not self.mock and bcm in self._devices:
                value = bool(self._devices[bcm].value)
                return not value if mode == "InputPullUp" else value
            return self._values.get(bcm, False)

    def pins(self) -> list[PinState]:
        with self._lock:
            states = []
            for bcm, physical in PHYSICAL_PINS.items():
                mode = self._modes.get(bcm, "Disabled")
                high = self.read(bcm) or False
                states.append(PinState(bcm, physical, mode, mode != "Disabled", high))
            return states

    def close(self) -> None:
        with self._lock:
            for device in self._devices.values():
                device.close()
            self._devices.clear()
