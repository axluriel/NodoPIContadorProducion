from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class NetworkError(RuntimeError):
    pass


@dataclass(slots=True)
class PendingNetworkChange:
    change_id: str
    interface: str
    previous_connection: str | None
    new_connection: str
    rollback_task: asyncio.Task


def _split_nmcli(line: str) -> list[str]:
    values: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line.rstrip("\n"):
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            values.append("".join(current))
            current = []
        else:
            current.append(character)
    current.append("\\") if escaped else None
    values.append("".join(current))
    return values


class NetworkService:
    def __init__(self, runner: Callable | None = None, rollback_seconds: int = 60) -> None:
        self._runner = runner or subprocess.run
        self.rollback_seconds = rollback_seconds
        self.admin_token = self._load_admin_token()
        self.nmcli_available = shutil.which("nmcli") is not None
        self.pending: PendingNetworkChange | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _load_admin_token() -> str:
        direct = os.getenv("CONTADOR_NETWORK_ADMIN_TOKEN", "").strip()
        if direct:
            return direct
        credentials = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
        if not credentials:
            return ""
        path = Path(credentials) / "network-admin-token"
        try:
            return path.read_text("utf-8").strip()
        except OSError:
            return ""

    @property
    def enabled(self) -> bool:
        return bool(self.admin_token) and self.nmcli_available

    def authorized(self, token: str) -> bool:
        return bool(self.admin_token) and hmac.compare_digest(self.admin_token, token.strip())

    async def _run(self, arguments: list[str], *, timeout: int = 15) -> str:
        def invoke():
            return self._runner(
                arguments,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )

        try:
            result = await asyncio.to_thread(invoke)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NetworkError(f"No se pudo ejecutar NetworkManager: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "NetworkManager rechazó la operación.").strip()
            raise NetworkError(message)
        return result.stdout

    def _require_enabled(self) -> None:
        if not self.admin_token:
            raise NetworkError("La administración de red no tiene token configurado.")
        if not self.nmcli_available:
            raise NetworkError("NetworkManager (nmcli) no está instalado.")

    async def status(self) -> dict:
        self._require_enabled()
        output = await self._run([
            "nmcli", "-t", "--escape", "yes", "-f",
            "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION,GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY",
            "device", "show",
        ])
        devices: list[dict] = []
        current: dict | None = None
        for line in output.splitlines():
            if not line:
                continue
            key, _, value = line.partition(":")
            value = _split_nmcli("x:" + value)[1]
            if key == "GENERAL.DEVICE":
                if current:
                    devices.append(current)
                current = {"name": value, "addresses": []}
            elif not current:
                continue
            elif key == "GENERAL.TYPE":
                current["type"] = value
            elif key == "GENERAL.STATE":
                current["state"] = value
            elif key == "GENERAL.CONNECTION":
                current["connection"] = "" if value == "--" else value
            elif key == "GENERAL.HWADDR":
                current["mac"] = value
            elif key.startswith("IP4.ADDRESS"):
                current["addresses"].append(value)
            elif key == "IP4.GATEWAY":
                current["gateway"] = value
        if current:
            devices.append(current)
        allowed = [item for item in devices if item.get("name") in {"wlan0", "eth0"}]
        return {
            "enabled": True,
            "devices": allowed,
            "pending": {
                "id": self.pending.change_id,
                "interface": self.pending.interface,
                "seconds": self.rollback_seconds,
            } if self.pending else None,
        }

    async def scan_wifi(self) -> list[dict]:
        self._require_enabled()
        output = await self._run([
            "nmcli", "-t", "--escape", "yes", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
            "device", "wifi", "list", "--rescan", "yes", "ifname", "wlan0",
        ], timeout=25)
        networks: dict[str, dict] = {}
        for line in output.splitlines():
            fields = _split_nmcli(line)
            if len(fields) < 4 or not fields[1]:
                continue
            network = {
                "active": fields[0] == "*", "ssid": fields[1],
                "signal": int(fields[2] or 0), "security": fields[3] or "Abierta",
            }
            previous = networks.get(network["ssid"])
            if previous is None or network["signal"] > previous["signal"]:
                networks[network["ssid"]] = network
        return sorted(networks.values(), key=lambda item: (not item["active"], -item["signal"], item["ssid"].lower()))

    async def _active_connection(self, interface: str) -> str | None:
        output = await self._run([
            "nmcli", "-g", "GENERAL.CONNECTION", "device", "show", interface,
        ])
        value = output.strip()
        return value if value and value != "--" else None

    @staticmethod
    def _validate_ssid(ssid: str) -> str:
        value = ssid.strip()
        if not value or len(value.encode("utf-8")) > 32 or any(ord(character) < 32 for character in value):
            raise NetworkError("El nombre Wi-Fi debe contener entre 1 y 32 bytes.")
        return value

    async def connect_wifi(self, ssid: str, password: str, hidden: bool = False) -> dict:
        self._require_enabled()
        ssid = self._validate_ssid(ssid)
        if password and not 8 <= len(password) <= 63:
            raise NetworkError("La contraseña Wi-Fi debe contener entre 8 y 63 caracteres.")
        async with self._lock:
            self._require_no_pending()
            previous = await self._active_connection("wlan0")
            suffix = hashlib.sha256(f"{ssid}-{time.time_ns()}".encode()).hexdigest()[:10]
            profile = f"NodoContador-wifi-{suffix}"
            await self._run(["nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0",
                             "con-name", profile, "ssid", ssid])
            try:
                await self._run(["nmcli", "connection", "modify", profile,
                                 "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
                                 "802-11-wireless.hidden", "yes" if hidden else "no"])
                if password:
                    await self._run(["nmcli", "connection", "modify", profile,
                                     "802-11-wireless-security.key-mgmt", "wpa-psk"])
                    await self._activate_with_password(profile, "wlan0", password)
                else:
                    await self._run(["nmcli", "--wait", "30", "connection", "up", "id", profile,
                                     "ifname", "wlan0"], timeout=35)
            except Exception:
                await self._delete_profile(profile)
                raise
            return self._begin_pending("wlan0", previous, profile)

    async def _activate_with_password(self, profile: str, interface: str, password: str) -> None:
        descriptor, filename = tempfile.mkstemp(prefix="nodocontador-nm-", text=True)
        try:
            os.chmod(filename, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"802-11-wireless-security.psk:{password}\n")
            await self._run(["nmcli", "--wait", "30", "connection", "up", "id", profile,
                             "ifname", interface, "passwd-file", filename], timeout=35)
        finally:
            try:
                os.unlink(filename)
            except FileNotFoundError:
                pass

    @staticmethod
    def _validate_addresses(values: list[str]) -> list[str]:
        addresses: list[str] = []
        for raw in values:
            value = str(raw).strip()
            if not value:
                continue
            try:
                interface = ipaddress.ip_interface(value)
            except ValueError as exc:
                raise NetworkError(f"Dirección IPv4 no válida: {value}") from exc
            if interface.version != 4:
                raise NetworkError("Por ahora solo se admiten direcciones IPv4 en eth0.")
            addresses.append(str(interface))
        if not addresses:
            raise NetworkError("Agrega por lo menos una dirección IPv4 con prefijo, por ejemplo 192.168.1.50/24.")
        if len(addresses) > 8:
            raise NetworkError("Se admiten como máximo ocho direcciones IPv4 en eth0.")
        return addresses

    async def configure_ethernet(self, method: str, addresses: list[str], gateway: str, dns: list[str]) -> dict:
        self._require_enabled()
        method = method.lower().strip()
        if method not in {"auto", "manual"}:
            raise NetworkError("El método de eth0 debe ser DHCP o manual.")
        addresses = self._validate_addresses(addresses) if method == "manual" else []
        gateway = gateway.strip()
        if gateway:
            try:
                if ipaddress.ip_address(gateway).version != 4:
                    raise ValueError
            except ValueError as exc:
                raise NetworkError("La puerta de enlace IPv4 no es válida.") from exc
        clean_dns: list[str] = []
        for value in dns:
            value = str(value).strip()
            if not value:
                continue
            try:
                if ipaddress.ip_address(value).version != 4:
                    raise ValueError
            except ValueError as exc:
                raise NetworkError(f"Servidor DNS no válido: {value}") from exc
            clean_dns.append(value)
        async with self._lock:
            self._require_no_pending()
            status = await self.status()
            if not any(item["name"] == "eth0" for item in status["devices"]):
                raise NetworkError("No se detectó eth0. Conecta primero un adaptador Ethernet USB o HAT.")
            previous = await self._active_connection("eth0")
            profile = f"NodoContador-eth0-{int(time.time())}"
            await self._run(["nmcli", "connection", "add", "type", "ethernet", "ifname", "eth0",
                             "con-name", profile])
            try:
                arguments = ["nmcli", "connection", "modify", profile,
                             "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
                             "ipv4.method", method]
                if method == "manual":
                    arguments.extend(["ipv4.addresses", ",".join(addresses),
                                      "ipv4.gateway", gateway, "ipv4.dns", ",".join(clean_dns)])
                else:
                    arguments.extend(["ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", "",
                                      "ipv4.ignore-auto-dns", "no"])
                await self._run(arguments)
                await self._run(["nmcli", "--wait", "20", "connection", "up", "id", profile,
                                 "ifname", "eth0"], timeout=25)
            except Exception:
                await self._delete_profile(profile)
                raise
            return self._begin_pending("eth0", previous, profile)

    def _require_no_pending(self) -> None:
        if self.pending:
            raise NetworkError("Primero confirma o espera la reversión del cambio de red pendiente.")

    def _begin_pending(self, interface: str, previous: str | None, new: str) -> dict:
        change_id = secrets.token_urlsafe(18)
        task = asyncio.create_task(self._rollback_after(change_id))
        self.pending = PendingNetworkChange(change_id, interface, previous, new, task)
        return {
            "success": True, "changeId": change_id, "confirmationSeconds": self.rollback_seconds,
            "message": "Cambio aplicado temporalmente. Confírmalo para conservarlo.",
        }

    async def _rollback_after(self, change_id: str) -> None:
        try:
            await asyncio.sleep(self.rollback_seconds)
            async with self._lock:
                if self.pending and self.pending.change_id == change_id:
                    await self._rollback(self.pending)
                    self.pending = None
        except asyncio.CancelledError:
            return

    async def _rollback(self, change: PendingNetworkChange) -> None:
        if change.previous_connection:
            try:
                await self._run(["nmcli", "--wait", "25", "connection", "up", "id",
                                 change.previous_connection, "ifname", change.interface], timeout=30)
            except NetworkError:
                pass
        else:
            try:
                await self._run(["nmcli", "connection", "down", "id", change.new_connection])
            except NetworkError:
                pass
        await self._delete_profile(change.new_connection)

    async def confirm(self, change_id: str) -> dict:
        async with self._lock:
            if not self.pending or not hmac.compare_digest(self.pending.change_id, change_id):
                raise NetworkError("El cambio de red ya no está pendiente o el identificador no coincide.")
            change = self.pending
            change.rollback_task.cancel()
            self.pending = None
            if change.previous_connection and change.previous_connection.startswith("NodoContador-"):
                await self._delete_profile(change.previous_connection)
            return {"success": True, "message": "Configuración de red confirmada y guardada."}

    async def _delete_profile(self, profile: str) -> None:
        try:
            await self._run(["nmcli", "connection", "delete", "id", profile])
        except NetworkError:
            pass

    async def close(self) -> None:
        if self.pending:
            self.pending.rollback_task.cancel()
            self.pending = None

