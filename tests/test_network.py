import subprocess

import pytest

from nodocontador.network import NetworkError, NetworkService, _split_nmcli


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, arguments, **_):
        self.calls.append(arguments)
        joined = " ".join(arguments)
        if "GENERAL.DEVICE,GENERAL.TYPE" in joined:
            output = (
                "GENERAL.DEVICE:eth0\nGENERAL.TYPE:ethernet\nGENERAL.STATE:100 (connected)\n"
                "GENERAL.CONNECTION:Wired connection 1\nGENERAL.HWADDR:AA\\:BB\\:CC\\:DD\\:EE\\:FF\n"
                "IP4.ADDRESS[1]:192.168.1.20/24\nIP4.GATEWAY:192.168.1.1\n\n"
                "GENERAL.DEVICE:wlan0\nGENERAL.TYPE:wifi\nGENERAL.STATE:100 (connected)\n"
                "GENERAL.CONNECTION:Casa\nGENERAL.HWADDR:11\\:22\\:33\\:44\\:55\\:66\n"
                "IP4.ADDRESS[1]:192.168.1.21/24\nIP4.GATEWAY:192.168.1.1\n"
            )
        elif "-g GENERAL.CONNECTION device show eth0" in joined:
            output = "Wired connection 1\n"
        elif "-g GENERAL.CONNECTION device show wlan0" in joined:
            output = "Casa\n"
        elif "device wifi list" in joined:
            output = "*:Casa:88:WPA2\n:Invitados:52:WPA2\n:Casa:60:WPA2\n"
        else:
            output = "ok\n"
        return subprocess.CompletedProcess(arguments, 0, output, "")


def make_service(monkeypatch):
    monkeypatch.setenv("CONTADOR_NETWORK_ADMIN_TOKEN", "token-seguro")
    monkeypatch.setattr("nodocontador.network.shutil.which", lambda _: "/usr/bin/nmcli")
    runner = FakeRunner()
    return NetworkService(runner=runner, rollback_seconds=3600), runner


def test_nmcli_parser_keeps_escaped_colons():
    assert _split_nmcli(r"*:Red\:Planta:77:WPA2") == ["*", "Red:Planta", "77", "WPA2"]


@pytest.mark.asyncio
async def test_status_and_wifi_scan(monkeypatch):
    service, _ = make_service(monkeypatch)
    status = await service.status()
    assert [item["name"] for item in status["devices"]] == ["eth0", "wlan0"]
    assert status["devices"][0]["addresses"] == ["192.168.1.20/24"]
    networks = await service.scan_wifi()
    assert networks[0] == {"active": True, "ssid": "Casa", "signal": 88, "security": "WPA2"}


@pytest.mark.asyncio
async def test_ethernet_accepts_multiple_ipv4_and_confirms(monkeypatch):
    service, runner = make_service(monkeypatch)
    result = await service.configure_ethernet(
        "manual", ["192.168.1.50/24", "10.20.0.50/16"], "192.168.1.1", ["1.1.1.1", "8.8.8.8"]
    )
    modify = next(call for call in runner.calls if "modify" in call and any("NodoContador-eth0" in value for value in call))
    assert "192.168.1.50/24,10.20.0.50/16" in modify
    confirmation = await service.confirm(result["changeId"])
    assert confirmation["success"] and service.pending is None


@pytest.mark.asyncio
async def test_wifi_password_is_not_passed_as_process_argument(monkeypatch):
    service, runner = make_service(monkeypatch)
    result = await service.connect_wifi("Nueva red", "secreto123")
    assert all("secreto123" not in " ".join(call) for call in runner.calls)
    await service.confirm(result["changeId"])


@pytest.mark.asyncio
async def test_invalid_ethernet_address_is_rejected(monkeypatch):
    service, _ = make_service(monkeypatch)
    with pytest.raises(NetworkError, match="no válida"):
        await service.configure_ethernet("manual", ["999.1.1.1/24"], "", [])

