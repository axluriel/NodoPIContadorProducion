import json

import pytest

from nodocontador.models import CounterSettings
from nodocontador.settings import SettingsStore


def test_defaults_are_safe(tmp_path):
    settings = SettingsStore(tmp_path / "s.json").current
    assert settings.enabled is False
    assert settings.sensor_pin == 17
    assert settings.input_mode == "InputPullUp"


def test_round_trip_and_normalization(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    value = CounterSettings(enabled=True, central_url=" http://c:8080/ ", shift_starts=["22:00", "06:00"])
    store.save(value)
    saved = json.loads((tmp_path / "s.json").read_text("utf-8"))
    assert saved["UrlCentral"] == "http://c:8080"
    assert saved["InicioTurnos"] == ["06:00", "22:00"]
    assert SettingsStore(tmp_path / "s.json").current.central_url == "http://c:8080"


def test_shift_list_accepts_a_comma_separated_string():
    assert CounterSettings.from_dict({"InicioTurnos": "06:00, 18:00"}).shift_starts == ["06:00", "18:00"]


@pytest.mark.parametrize("changes, message", [
    ({"sensor_pin": 2}, "sensor"),
    ({"input_mode": "Output"}, "modo"),
    ({"stop_output_pin": 17}, "sensor y salida"),
    ({"target_output_pin": 23, "stop_output_pin": 23}, "mismo GPIO"),
    ({"units_per_pulse": 0}, "unidades"),
    ({"stop_seconds": 1}, "parada"),
    ({"shift_starts": ["6:00"]}, "turno"),
    ({"central_url": "ftp://x"}, "http"),
])
def test_validation(tmp_path, changes, message):
    value = CounterSettings(**changes)
    with pytest.raises(ValueError, match=message):
        SettingsStore(tmp_path / "s.json").save(value)
