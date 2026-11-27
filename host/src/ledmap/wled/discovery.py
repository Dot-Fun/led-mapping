"""mDNS + HTTP /json/info discovery for all WLED controllers on the LAN."""
from __future__ import annotations

import asyncio
import socket
from typing import Callable

import httpx
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from ledmap.models import LEDDescriptor, WLEDController


class _WLEDListener(ServiceListener):
    def __init__(self, on_candidate: Callable[[str, str], None]) -> None:
        self._on_candidate = on_candidate

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._handle(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._handle(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def _handle(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=1500)
        if not info:
            return
        addrs = info.parsed_addresses()
        if not addrs:
            return
        instance = name.replace(f".{type_}", "")
        if "wled" in instance.lower() or "wled" in (info.server or "").lower():
            self._on_candidate(instance, addrs[0])


class WLEDRegistry:
    """Maintains the live list of WLED controllers on the local network."""

    def __init__(self) -> None:
        self.controllers: dict[str, WLEDController] = {}
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_ips: set[str] = set()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        try:
            self._zc = Zeroconf()
            listener = _WLEDListener(self._on_mdns_candidate)
            self._browser = ServiceBrowser(self._zc, "_http._tcp.local.", listener)
        except Exception:
            pass

    def stop(self) -> None:
        try:
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        self._zc = None
        self._browser = None

    def _on_mdns_candidate(self, instance: str, ip: str) -> None:
        if self._loop and ip not in self._pending_ips:
            self._pending_ips.add(ip)
            asyncio.run_coroutine_threadsafe(self.probe_ip(ip, hint_id=instance), self._loop)

    async def probe_ip(self, ip: str, hint_id: str | None = None) -> WLEDController | None:
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                r = await client.get(f"http://{ip}/json/info")
                if r.status_code != 200:
                    return None
                data = r.json()
            if "leds" not in data:
                return None
            mac = str(data.get("mac", ""))
            cid = hint_id or (f"wled-{mac[-6:]}" if len(mac) >= 6 else f"wled-{ip}")
            prev = self.controllers.get(cid)
            ctrl = WLEDController(
                id=cid,
                name=str(data.get("name", cid)),
                ip=ip,
                led_count=int(data.get("leds", {}).get("count", 0)),
                version=str(data.get("ver", "")),
                arch=str(data.get("arch", "esp32")),
                online=True,
                selected=prev.selected if prev is not None else True,
                mac=mac,
            )
            self.controllers[cid] = ctrl
            return ctrl
        except Exception:
            return None
        finally:
            self._pending_ips.discard(ip)

    async def refresh_all(self) -> list[WLEDController]:
        tasks = [self.probe_ip(c.ip, hint_id=c.id) for c in list(self.controllers.values())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.list_controllers()

    def list_controllers(self) -> list[WLEDController]:
        return sorted(self.controllers.values(), key=lambda c: c.name.lower())

    def set_selected(self, controller_id: str, selected: bool) -> WLEDController | None:
        c = self.controllers.get(controller_id)
        if c:
            c.selected = selected
        return c

    def build_led_table(self, selected_only: bool = True) -> list[LEDDescriptor]:
        ctrls = [c for c in self.list_controllers() if (not selected_only or c.selected) and c.online]
        table: list[LEDDescriptor] = []
        gid = 0
        for c in ctrls:
            for idx in range(c.led_count):
                table.append(
                    LEDDescriptor(
                        global_id=gid,
                        controller_id=c.id,
                        controller_name=c.name,
                        led_index=idx,
                    )
                )
                gid += 1
        return table
