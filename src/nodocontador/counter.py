from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from .gpio import PHYSICAL_PINS, GpioService
from .models import CounterSettings
from .settings import SettingsStore
from .store import CounterStore, minute_key, now

log = logging.getLogger(__name__)

RATE_WINDOW_SECONDS = 600


class ProductionCounter:
    """Cuenta las piezas que cortan el haz del sensor fotoeléctrico.

    Los flancos llegan en el hilo de gpiozero; ahí solo se filtran y se suman
    en memoria. El bucle asíncrono vuelca lo acumulado a SQLite cada pocos
    segundos, detecta paradas y cambia de turno. Así un pulso nunca espera a
    la tarjeta SD, y un corte de luz pierde a lo sumo el último volcado.

    Filtros, en este orden:

    - **Pulso mínimo** (``PulsoMinimoMs``): si es mayor que cero, la pieza se
      cuenta al *liberar* el haz y solo si estuvo cortado al menos ese tiempo.
      Descarta destellos, polvo o vibración del sensor.
    - **Antirrebote** (``AntirreboteMs``): tiempo mínimo entre dos piezas
      contadas, medido desde el flanco de detección. Una caja con asas o
      huecos que corta el haz dos veces seguidas cuenta una sola vez.
    """

    def __init__(
        self,
        gpio: GpioService,
        settings: SettingsStore,
        store: CounterStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = now,
        flush_seconds: float = 5.0,
    ) -> None:
        self.gpio = gpio
        self.settings_store = settings
        self.store = store
        self._clock = clock
        self._wall = wall
        self.flush_seconds = flush_seconds
        self._lock = threading.RLock()
        self._settings: CounterSettings = settings.current
        self._pending: dict[str, list[int]] = {}
        shift = store.current_shift()
        self._shift_id: int = shift["id"]
        self._shift_start: datetime = datetime.fromisoformat(shift["inicio"])
        self._shift_units: int = shift["unidades"]
        self._shift_pulses: int = shift["pulsos"]
        self._total_units: int = store.total_units
        self._last_accept: float | None = None
        self._last_piece_at: datetime | None = None
        self._active_since: float | None = None
        self._recent: deque[tuple[float, int]] = deque()
        self.filtered = 0
        self._started = clock()
        self._started_at = wall()
        self._stopped = False
        self._stopped_since: datetime | None = None
        self._last_flush = clock()
        self._sensor_pin: int | None = None
        self._output_pins: list[int] = []
        self._task: asyncio.Task | None = None
        self.error: str | None = None

    # -- configuración ----------------------------------------------------
    def apply_configuration(self) -> None:
        """Lleva los GPIO a lo que dice la configuración guardada."""
        settings = self.settings_store.current
        with self._lock:
            self._settings = settings
            self._active_since = None
        wanted_outputs = settings.output_pins if settings.enabled else []
        wanted_sensor = settings.sensor_pin if settings.enabled else None
        # Primero se liberan los pines que dejaron de usarse: un pin que pasa de
        # salida a sensor debe apagarse antes de reconfigurarse como entrada.
        for pin in self._output_pins:
            if pin not in wanted_outputs:
                self._release(pin)
        if self._sensor_pin is not None and self._sensor_pin != wanted_sensor:
            self._release(self._sensor_pin)
        self._output_pins, self._sensor_pin = [], None
        self.error = None
        try:
            if wanted_sensor is not None:
                if self.gpio.mode(wanted_sensor) != settings.input_mode:
                    self.gpio.configure(wanted_sensor, settings.input_mode)
                self._sensor_pin = wanted_sensor
            for pin in wanted_outputs:
                if self.gpio.mode(pin) != "Output":
                    self.gpio.configure(pin, "Output")
                self._output_pins.append(pin)
        except Exception as exc:
            self.error = str(exc)
            raise
        finally:
            self._update_outputs()

    def _release(self, pin: int) -> None:
        try:
            if self.gpio.mode(pin) == "Output":
                self.gpio.set(pin, False)
            self.gpio.configure(pin, "Disabled")
        except Exception:
            log.exception("No se pudo liberar BCM%s", pin)

    # -- flancos ------------------------------------------------------------
    def on_edge(self, bcm: int, high: bool) -> None:
        settings = self._settings
        if not settings.enabled or bcm != settings.sensor_pin:
            return
        moment = self._clock()
        detecting = high == settings.active_high
        with self._lock:
            if settings.min_pulse_ms <= 0:
                if detecting:
                    self._accept(moment, settings)
                return
            if detecting:
                self._active_since = moment
            elif self._active_since is not None:
                start, self._active_since = self._active_since, None
                if (moment - start) * 1000 >= settings.min_pulse_ms:
                    self._accept(start, settings)
                else:
                    self.filtered += 1

    def _accept(self, moment: float, settings: CounterSettings) -> None:
        if self._last_accept is not None and (moment - self._last_accept) * 1000 < settings.debounce_ms:
            self.filtered += 1
            return
        self._last_accept = moment
        self._add(settings.units_per_pulse, 1, moment)

    def _add(self, units: int, pulses: int, moment: float) -> None:
        wall = self._wall()
        bucket = self._pending.setdefault(minute_key(wall), [0, 0])
        bucket[0] += units
        bucket[1] += pulses
        self._shift_units += units
        self._shift_pulses += pulses
        self._total_units += units
        self._last_piece_at = wall
        self._recent.append((moment, units))

    def add_manual(self, pulses: int) -> None:
        """Suma pulsos sin pasar por los filtros (simulación y pruebas)."""
        if pulses <= 0:
            raise ValueError("La cantidad debe ser positiva.")
        with self._lock:
            moment = self._clock()
            self._last_accept = moment
            self._add(pulses * self._settings.units_per_pulse, pulses, moment)

    # -- persistencia y turnos ---------------------------------------------
    def flush(self) -> None:
        with self._lock:
            pending = {key: (value[0], value[1]) for key, value in self._pending.items()}
            self._pending.clear()
            self._last_flush = self._clock()
        if not pending:
            return
        try:
            self.store.add_counts(pending)
        except Exception:
            log.exception("No se pudo guardar el conteo; se reintenta en el próximo volcado")
            with self._lock:
                for key, (units, pulses) in pending.items():
                    bucket = self._pending.setdefault(key, [0, 0])
                    bucket[0] += units
                    bucket[1] += pulses

    def new_shift(self, reason: str = "manual", at: datetime | None = None) -> dict:
        """Cierra el turno en curso (con todo lo contado hasta ahora) y abre otro."""
        self.flush()
        with self._lock:
            shift = self.store.close_shift(reason, at)
            self._shift_id = shift["id"]
            self._shift_start = datetime.fromisoformat(shift["inicio"])
            self._shift_units = shift["unidades"]
            self._shift_pulses = shift["pulsos"]
            if self._stopped:
                # La parada sigue, pero ahora se le carga al turno nuevo.
                self._stopped_since = self._shift_start
                self.store.open_stop(self._shift_start)
        self._update_outputs()
        log.info("Turno %s abierto (%s)", shift["id"], reason)
        return shift

    def last_shift_boundary(self, moment: datetime) -> datetime | None:
        """Último inicio de turno programado que ya pasó."""
        candidates = []
        for start in self._settings.shift_starts:
            hour, minute = (int(part) for part in start.split(":"))
            for days in (0, 1):
                day = moment - timedelta(days=days)
                candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= moment:
                    candidates.append(candidate)
        return max(candidates) if candidates else None

    # -- bucle -------------------------------------------------------------
    def tick(self) -> None:
        wall = self._wall()
        boundary = self.last_shift_boundary(wall)
        if boundary is not None and self._shift_start < boundary:
            self.new_shift("automatico", boundary)
        self._check_stop()
        if self._clock() - self._last_flush >= self.flush_seconds:
            self.flush()
        self._update_outputs()

    def _check_stop(self) -> None:
        settings = self._settings
        with self._lock:
            if not settings.enabled:
                if self._stopped:
                    self.store.close_stop(self._wall())
                self._stopped, self._stopped_since = False, None
                return
            reference = self._last_accept if self._last_accept is not None else self._started
            idle = self._clock() - reference
            if idle >= settings.stop_seconds and not self._stopped:
                self._stopped = True
                self._stopped_since = self._last_piece_at or self._started_at
                if self._stopped_since < self._shift_start:
                    self._stopped_since = self._shift_start
                self.store.open_stop(self._stopped_since)
            elif idle < settings.stop_seconds and self._stopped:
                self._stopped, self._stopped_since = False, None
                self.store.close_stop(self._last_piece_at or self._wall())

    def _update_outputs(self) -> None:
        settings = self._settings
        states = {}
        if settings.target_output_pin in self._output_pins:
            states[settings.target_output_pin] = (
                settings.target_per_shift > 0 and self._shift_units >= settings.target_per_shift
            )
        if settings.stop_output_pin in self._output_pins:
            states[settings.stop_output_pin] = self._stopped
        for pin, value in states.items():
            try:
                self.gpio.set(pin, value)
            except Exception:
                log.exception("No se pudo escribir la salida BCM%s", pin)

    async def start(self) -> None:
        self.gpio.add_callback(self.on_edge)
        try:
            self.apply_configuration()
        except Exception:
            log.exception("No se pudo configurar el sensor al arrancar")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("Fallo en el ciclo del contador")
            await asyncio.sleep(1)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.gpio.remove_callback(self.on_edge)
        self.flush()

    # -- estado para el panel ------------------------------------------------
    def rates(self) -> tuple[int, float]:
        """Unidades en el último minuto y promedio por minuto de los últimos diez."""
        moment = self._clock()
        with self._lock:
            while self._recent and moment - self._recent[0][0] > RATE_WINDOW_SECONDS:
                self._recent.popleft()
            last_minute = sum(units for at, units in self._recent if moment - at <= 60)
            window = min(RATE_WINDOW_SECONDS, max(60.0, moment - self._started))
            ten_minutes = sum(units for _, units in self._recent)
        return last_minute, round(ten_minutes * 60 / window, 1)

    def snapshot(self) -> dict:
        settings = self._settings
        per_minute, average = self.rates()
        moment = self._clock()
        with self._lock:
            if not settings.enabled:
                state = "DESHABILITADO"
            elif self._stopped:
                state = "PARADA"
            elif self._last_accept is None:
                state = "ESPERANDO"
            else:
                state = "PRODUCIENDO"
            idle = None if self._last_accept is None else round(moment - self._last_accept, 1)
            shift = {
                "id": self._shift_id,
                "inicio": self._shift_start.isoformat(),
                "unidades": self._shift_units,
                "pulsos": self._shift_pulses,
                "meta": settings.target_per_shift,
                "avance": round(100 * self._shift_units / settings.target_per_shift, 1)
                if settings.target_per_shift > 0 else None,
            }
            stopped_since = self._stopped_since.isoformat() if self._stopped_since else None
            last_piece = self._last_piece_at.isoformat() if self._last_piece_at else None
            total = self._total_units
            filtered = self.filtered
        level = self.gpio.read(settings.sensor_pin) if settings.enabled else None
        shift["segundosParada"] = self.store.stop_seconds_in_shift(shift["id"])
        return {
            "estado": state,
            "habilitado": settings.enabled,
            "nodo": settings.node_name,
            "linea": settings.line_name,
            "turno": shift,
            "totalAcumulado": total,
            "ritmo": {"ultimoMinuto": per_minute, "promedio10Min": average},
            "ultimaPieza": last_piece,
            "segundosSinPiezas": idle,
            "paradaDesde": stopped_since,
            "filtrados": filtered,
            "sensor": {
                "bcm": settings.sensor_pin,
                "fisico": PHYSICAL_PINS.get(settings.sensor_pin),
                "modo": self.gpio.mode(settings.sensor_pin),
                "alto": level,
                "detectando": None if level is None else level == settings.active_high,
            },
            "error": self.error,
        }
