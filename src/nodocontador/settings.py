from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from .gpio import INPUT_MODES, PHYSICAL_PINS
from .models import CounterSettings

MASK = "********"
_SHIFT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._current = CounterSettings()
        try:
            if path.exists():
                self._current = CounterSettings.from_dict(json.loads(path.read_text("utf-8")))
        except Exception:
            logging.getLogger(__name__).exception(
                "No se pudo leer %s; se usan valores predeterminados", path
            )

    @property
    def current(self) -> CounterSettings:
        with self._lock:
            return CounterSettings.from_dict(self._current.to_dict())

    @staticmethod
    def validate(settings: CounterSettings) -> None:
        if settings.sensor_pin not in PHYSICAL_PINS:
            raise ValueError("El GPIO del sensor está reservado o no existe en el conector.")
        if settings.input_mode not in INPUT_MODES:
            raise ValueError("El modo de entrada del sensor no es válido.")
        outputs = settings.output_pins
        if any(pin not in PHYSICAL_PINS for pin in outputs):
            raise ValueError("Una salida usa un GPIO reservado o no disponible para E/S digital.")
        if settings.sensor_pin in outputs:
            raise ValueError(f"BCM{settings.sensor_pin} no puede ser sensor y salida al mismo tiempo.")
        if len(outputs) != len(set(outputs)):
            raise ValueError("La salida de meta y la de parada no pueden usar el mismo GPIO.")
        if not 0 <= settings.debounce_ms <= 10_000:
            raise ValueError("El antirrebote debe estar entre 0 y 10000 ms.")
        if not 0 <= settings.min_pulse_ms <= 10_000:
            raise ValueError("El pulso mínimo debe estar entre 0 y 10000 ms.")
        if not 1 <= settings.units_per_pulse <= 10_000:
            raise ValueError("Las unidades por pulso deben estar entre 1 y 10000.")
        if not 5 <= settings.stop_seconds <= 86_400:
            raise ValueError("El tiempo para considerar la línea parada debe estar entre 5 s y 24 h.")
        if settings.target_per_shift < 0:
            raise ValueError("La meta por turno no puede ser negativa.")
        if not 5 <= settings.sync_seconds <= 3600:
            raise ValueError("La sincronización debe estar entre 5 y 3600 segundos.")
        for start in settings.shift_starts:
            if not _SHIFT.match(start):
                raise ValueError(f"Inicio de turno no válido: {start!r}. Use HH:MM.")
        if len(settings.shift_starts) != len(set(settings.shift_starts)):
            raise ValueError("Hay inicios de turno repetidos.")
        if settings.central_url.strip():
            parsed = urlparse(settings.central_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("La URL de la central debe ser http o https.")

    def save(self, settings: CounterSettings) -> CounterSettings:
        with self._lock:
            # El panel recibe las claves enmascaradas; devolver la máscara
            # significa «no la cambié».
            if settings.central_api_key == MASK:
                settings.central_api_key = self._current.central_api_key
            if settings.query_key == MASK:
                settings.query_key = self._current.query_key
            settings.node_name = settings.node_name.strip()
            settings.line_name = settings.line_name.strip()
            settings.central_url = settings.central_url.strip().rstrip("/")
            settings.central_api_key = settings.central_api_key.strip()
            settings.query_key = settings.query_key.strip()
            settings.shift_starts = sorted(item.strip() for item in settings.shift_starts if item.strip())
            self.validate(settings)

            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(settings.to_dict(), indent=2, ensure_ascii=False), "utf-8")
            temporary.replace(self.path)
            self._current = CounterSettings.from_dict(settings.to_dict())
            return self.current
