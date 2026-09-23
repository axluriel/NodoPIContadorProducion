from __future__ import annotations

from dataclasses import dataclass, field


def _bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "si", "sí", "on", "yes"}
    return bool(value)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get(data: dict, *keys, default=None):
    for key in keys:
        if key in data:
            return data[key]
    return default


@dataclass(slots=True)
class CounterSettings:
    """Configuración del nodo contador (``contador-settings.json``)."""

    enabled: bool = False
    node_name: str = "Contador"
    line_name: str = ""
    # Sensor fotoeléctrico.
    sensor_pin: int = 17
    input_mode: str = "InputPullUp"
    # Con un sensor NPN (o un optoacoplador que lleva el pin a GND) la pieza
    # se detecta cuando la entrada cae a BAJO: activo en bajo.
    active_high: bool = False
    debounce_ms: int = 50
    min_pulse_ms: int = 0
    units_per_pulse: int = 1
    # Estado de la línea.
    stop_seconds: int = 60
    target_per_shift: int = 0
    target_output_pin: int = 0
    stop_output_pin: int = 0
    shift_starts: list[str] = field(default_factory=lambda: ["06:00", "14:00", "22:00"])
    # Central (opcional).
    central_url: str = ""
    node_id: int = 0
    central_api_key: str = ""
    query_key: str = ""
    sync_seconds: int = 30

    @property
    def central_configured(self) -> bool:
        return bool(self.central_url.strip()) and self.node_id > 0

    @property
    def output_pins(self) -> list[int]:
        return [pin for pin in (self.target_output_pin, self.stop_output_pin) if pin > 0]

    @classmethod
    def from_dict(cls, data: dict | None) -> "CounterSettings":
        data = data or {}
        default = cls()
        shifts = _get(data, "InicioTurnos", "inicioTurnos", default=default.shift_starts)
        if isinstance(shifts, str):
            shifts = [item.strip() for item in shifts.replace(";", ",").split(",")]
        return cls(
            enabled=_bool(_get(data, "Habilitado", "habilitado"), default.enabled),
            node_name=str(_get(data, "NombreNodo", "nombreNodo", default=default.node_name) or ""),
            line_name=str(_get(data, "NombreLinea", "nombreLinea", default="") or ""),
            sensor_pin=_int(_get(data, "PinSensor", "pinSensor"), default.sensor_pin),
            input_mode=str(_get(data, "ModoEntrada", "modoEntrada", default=default.input_mode)),
            active_high=_bool(_get(data, "ActivoEnAlto", "activoEnAlto"), default.active_high),
            debounce_ms=_int(_get(data, "AntirreboteMs", "antirreboteMs"), default.debounce_ms),
            min_pulse_ms=_int(_get(data, "PulsoMinimoMs", "pulsoMinimoMs"), default.min_pulse_ms),
            units_per_pulse=_int(_get(data, "UnidadesPorPulso", "unidadesPorPulso"), default.units_per_pulse),
            stop_seconds=_int(_get(data, "SegundosParada", "segundosParada"), default.stop_seconds),
            target_per_shift=_int(_get(data, "MetaTurno", "metaTurno"), default.target_per_shift),
            target_output_pin=_int(_get(data, "PinSalidaMeta", "pinSalidaMeta"), 0),
            stop_output_pin=_int(_get(data, "PinSalidaParada", "pinSalidaParada"), 0),
            shift_starts=[str(item).strip() for item in (shifts or []) if str(item).strip()],
            central_url=str(_get(data, "UrlCentral", "urlCentral", default="") or ""),
            node_id=_int(_get(data, "NodoId", "nodoId"), 0),
            central_api_key=str(_get(data, "ClaveNodo", "claveNodo", default="") or ""),
            query_key=str(_get(data, "ClaveConsulta", "claveConsulta", default="") or ""),
            sync_seconds=_int(_get(data, "SegundosSincronizacion", "segundosSincronizacion"), default.sync_seconds),
        )

    def to_dict(self) -> dict:
        return {
            "Habilitado": self.enabled,
            "NombreNodo": self.node_name,
            "NombreLinea": self.line_name,
            "PinSensor": self.sensor_pin,
            "ModoEntrada": self.input_mode,
            "ActivoEnAlto": self.active_high,
            "AntirreboteMs": self.debounce_ms,
            "PulsoMinimoMs": self.min_pulse_ms,
            "UnidadesPorPulso": self.units_per_pulse,
            "SegundosParada": self.stop_seconds,
            "MetaTurno": self.target_per_shift,
            "PinSalidaMeta": self.target_output_pin,
            "PinSalidaParada": self.stop_output_pin,
            "InicioTurnos": list(self.shift_starts),
            "UrlCentral": self.central_url,
            "NodoId": self.node_id,
            "ClaveNodo": self.central_api_key,
            "ClaveConsulta": self.query_key,
            "SegundosSincronizacion": self.sync_seconds,
        }

    def public_dict(self) -> dict:
        """Lo que ve el panel: las claves no salen del nodo."""
        value = self.to_dict()
        value["ClaveNodo"] = "" if not self.central_api_key else "********"
        value["ClaveConsulta"] = "" if not self.query_key else "********"
        return value
