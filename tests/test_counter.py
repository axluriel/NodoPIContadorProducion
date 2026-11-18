from datetime import datetime, timedelta

import pytest

from nodocontador.counter import ProductionCounter
from nodocontador.gpio import GpioService
from nodocontador.models import CounterSettings
from nodocontador.settings import SettingsStore
from nodocontador.store import CounterStore


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.wall = datetime(2026, 9, 23, 10, 0).astimezone()

    def advance(self, seconds: float) -> None:
        self.value += seconds
        self.wall += timedelta(seconds=seconds)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTADOR_GPIO_MOCK", "1")
    clock = Clock()
    settings = SettingsStore(tmp_path / "contador-settings.json")

    def make(**changes):
        value = CounterSettings(enabled=True, sensor_pin=17, input_mode="InputPullUp",
                                active_high=False, debounce_ms=100, min_pulse_ms=0,
                                shift_starts=[], stop_seconds=30)
        for key, item in changes.items():
            setattr(value, key, item)
        settings.save(value)
        gpio = GpioService(tmp_path / "gpio.json")
        store = CounterStore(tmp_path / "contador.db")
        counter = ProductionCounter(gpio, settings, store, clock=lambda: clock.value, wall=lambda: clock.wall)
        gpio.add_callback(counter.on_edge)
        counter.apply_configuration()
        return counter, gpio, store

    make.clock = clock
    return make


def piece(gpio, clock, width=0.05, gap=0.5, pin=17):
    """Una pieza con sensor activo en bajo: el haz cortado lleva el pin a 0 V."""
    gpio.emit(pin, False)
    clock.advance(width)
    gpio.emit(pin, True)
    clock.advance(gap)


def test_counts_each_piece_on_the_detection_edge(rig):
    counter, gpio, _ = rig()
    for _ in range(5):
        piece(gpio, rig.clock)

    snapshot = counter.snapshot()
    assert snapshot["turno"]["unidades"] == 5
    assert snapshot["estado"] == "PRODUCIENDO"
    assert snapshot["filtrados"] == 0


def test_the_sensor_pin_is_configured_as_input(rig):
    _, gpio, _ = rig(input_mode="InputPullDown")
    assert gpio.mode(17) == "InputPullDown"


def test_debounce_drops_a_second_cut_of_the_same_box(rig):
    counter, gpio, _ = rig(debounce_ms=300)
    # Una caja con asa corta el haz dos veces separadas por 100 ms.
    piece(gpio, rig.clock, gap=0.05)
    piece(gpio, rig.clock, gap=1.0)
    piece(gpio, rig.clock)

    assert counter.snapshot()["turno"]["unidades"] == 2
    assert counter.filtered == 1


def test_min_pulse_width_rejects_flashes(rig):
    counter, gpio, _ = rig(min_pulse_ms=20)
    piece(gpio, rig.clock, width=0.005)  # destello
    piece(gpio, rig.clock, width=0.05)

    assert counter.snapshot()["turno"]["unidades"] == 1
    assert counter.filtered == 1


def test_active_high_sensor_ignores_the_opposite_edge(rig):
    counter, gpio, _ = rig(active_high=True, input_mode="InputPullDown")
    gpio.emit(17, True)
    rig.clock.advance(1)
    gpio.emit(17, False)
    rig.clock.advance(1)

    assert counter.snapshot()["turno"]["unidades"] == 1


def test_units_per_pulse_multiplies_the_count(rig):
    counter, gpio, store = rig(units_per_pulse=12)
    piece(gpio, rig.clock)
    piece(gpio, rig.clock)
    counter.flush()

    assert counter.snapshot()["turno"]["unidades"] == 24
    assert store.current_shift()["pulsos"] == 2
    assert store.total_units == 24


def test_other_pins_and_disabled_counter_do_not_count(rig):
    counter, gpio, _ = rig(enabled=False)
    piece(gpio, rig.clock)
    assert counter.snapshot()["turno"]["unidades"] == 0
    assert counter.snapshot()["estado"] == "DESHABILITADO"


def test_counts_survive_a_restart(rig, tmp_path):
    counter, gpio, store = rig()
    for _ in range(3):
        piece(gpio, rig.clock)
    counter.flush()
    store.close()

    reopened = CounterStore(tmp_path / "contador.db")
    assert reopened.current_shift()["unidades"] == 3
    assert reopened.total_units == 3


def test_manual_shift_reset_starts_from_zero_but_keeps_the_total(rig):
    counter, gpio, store = rig()
    for _ in range(4):
        piece(gpio, rig.clock)
    closed_id = counter.snapshot()["turno"]["id"]

    counter.new_shift("manual")
    piece(gpio, rig.clock)

    snapshot = counter.snapshot()
    assert snapshot["turno"]["unidades"] == 1
    assert snapshot["totalAcumulado"] == 5
    closed = next(item for item in store.recent_shifts() if item["id"] == closed_id)
    assert closed["unidades"] == 4
    assert closed["motivoCierre"] == "manual"


def test_shift_closes_automatically_at_the_configured_time(rig):
    rig.clock.wall = datetime(2026, 9, 23, 13, 59, 50).astimezone()
    counter, gpio, store = rig(shift_starts=["06:00", "14:00", "22:00"])
    # El turno abierto al crear la base empezó «ahora» (13:59:50 reales); se
    # lo lleva al turno de la mañana para que el cruce de las 14:00 se note.
    counter._shift_start = datetime(2026, 9, 23, 6, 0).astimezone()
    counter.tick()
    piece(gpio, rig.clock)
    rig.clock.advance(20)
    counter.tick()

    snapshot = counter.snapshot()
    assert snapshot["turno"]["inicio"].startswith("2026-09-23T14:00")
    assert snapshot["turno"]["unidades"] == 0
    assert store.recent_shifts()[1]["motivoCierre"] == "automatico"
    assert store.recent_shifts()[1]["unidades"] == 1


def test_stop_is_detected_and_logged(rig):
    counter, gpio, store = rig(stop_seconds=30, stop_output_pin=23)
    piece(gpio, rig.clock)
    rig.clock.advance(31)
    counter.tick()

    assert counter.snapshot()["estado"] == "PARADA"
    assert gpio.read(23) is True

    rig.clock.advance(60)
    piece(gpio, rig.clock)
    counter.tick()

    assert counter.snapshot()["estado"] == "PRODUCIENDO"
    assert gpio.read(23) is False
    stop = store.recent_stops()[0]
    assert stop["hasta"] is not None
    assert stop["segundos"] >= 90


def test_target_output_turns_on_when_the_goal_is_reached(rig):
    counter, gpio, _ = rig(target_per_shift=3, target_output_pin=24)
    piece(gpio, rig.clock)
    piece(gpio, rig.clock)
    counter.tick()
    assert gpio.read(24) is False
    piece(gpio, rig.clock)
    counter.tick()
    assert gpio.read(24) is True
    assert counter.snapshot()["turno"]["avance"] == 100.0

    counter.new_shift()
    assert gpio.read(24) is False


def test_rates_report_the_last_minute(rig):
    counter, gpio, _ = rig(debounce_ms=0)
    for _ in range(30):
        piece(gpio, rig.clock, gap=1.0)
    per_minute, _ = counter.rates()
    assert per_minute == 30


def test_pending_minutes_are_absolute_and_resent_after_growth(rig):
    counter, gpio, store = rig()
    piece(gpio, rig.clock)
    counter.flush()
    pending = store.pending_minutes()
    assert [item["unidades"] for item in pending] == [1]

    store.mark_delivered(pending)
    assert store.pending_minutes() == []

    piece(gpio, rig.clock)
    counter.flush()
    assert [item["unidades"] for item in store.pending_minutes()] == [2]


def test_changing_the_sensor_pin_releases_the_old_one(rig, tmp_path):
    counter, gpio, _ = rig()
    value = counter.settings_store.current
    value.sensor_pin = 27
    counter.settings_store.save(value)
    counter.apply_configuration()

    assert gpio.mode(17) == "Disabled"
    assert gpio.mode(27) == "InputPullUp"
