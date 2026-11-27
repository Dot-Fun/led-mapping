"""Unlimited Camera Fleet Manager: macOS local bridge + Wi-Fi mDNS + UDP :8089 beacon + USB ADB."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from ledmap.models import CameraIntrinsics, CameraNode


class _AndroidCamListener(ServiceListener):
    def __init__(self, on_found: Any) -> None:
        self._on_found = on_found

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
        self._on_found(addrs[0], info.port)


class _UdpBeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_beacon: Any) -> None:
        self._on_beacon = on_beacon

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
            msg_type = payload.get("type")
            if msg_type in ("ledcam_beacon", "ledcam_macos_host"):
                ip = payload.get("ip") or addr[0]
                port = int(payload.get("port", 8088 if msg_type == "ledcam_beacon" else 8781))
                self._on_beacon(ip, port, payload)
        except Exception:
            pass


class CameraFleetManager:
    """Manages an arbitrary number of local macOS and Android camera nodes."""

    def __init__(
        self,
        macos_cam_port: int = 8781,
        udp_beacon_port: int = 8089,
        profiles_path: Path | None = None,
    ) -> None:
        self.macos_cam_port = macos_cam_port
        self.udp_beacon_port = udp_beacon_port
        self.profiles_path = profiles_path
        self.cameras: dict[str, CameraNode] = {}
        self.calibrated_profiles: dict[str, CameraIntrinsics] = {}

        self._macos_proc: subprocess.Popen[bytes] | None = None
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._adb_ports: dict[str, int] = {}
        self._next_adb_port = 18088
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bg_task: asyncio.Task[None] | None = None
        self._studio_host_url: str = self._detect_studio_host_url()
        self._client = httpx.AsyncClient(
            timeout=8.0,
            headers={"X-Studio-Host": self._studio_host_url},
        )

        self._load_profiles()

    @staticmethod
    def _detect_studio_host_url(port: int = 8780) -> str:
        try:
            out = subprocess.check_output(["ifconfig"], text=True, timeout=2)
            ips = [
                ln.strip().split()[1]
                for ln in out.splitlines()
                if ln.strip().startswith("inet ")
                and not ln.strip().split()[1].startswith(("127.", "169.254."))
            ]
            lan = next((i for i in ips if i.startswith("192.168.0.")), None) or next(
                (i for i in ips if i.startswith(("192.168.", "10."))), None
            ) or (ips[0] if ips else "127.0.0.1")
            return f"http://{lan}:{port}"
        except Exception:
            return f"http://127.0.0.1:{port}"

    def _load_profiles(self) -> None:
        if self.profiles_path and self.profiles_path.exists():
            try:
                raw = json.loads(self.profiles_path.read_text())
                for k, v in raw.items():
                    self.calibrated_profiles[k] = CameraIntrinsics(**v)
            except Exception:
                pass

    def save_calibrated_profile(self, camera_id: str, intrinsics: CameraIntrinsics) -> None:
        self.calibrated_profiles[camera_id] = intrinsics
        if camera_id in self.cameras:
            self.cameras[camera_id].intrinsics = intrinsics
        if self.profiles_path:
            self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.model_dump() for k, v in self.calibrated_profiles.items()}
            self.profiles_path.write_text(json.dumps(data, indent=2))

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._start_macos_bridge()
        # Wait up to 2.0s for local macos-cam bridge to bind its port
        for _ in range(20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", self.macos_cam_port)) == 0:
                    break
            await asyncio.sleep(0.1)
        self._start_mdns()
        await self._start_udp_listener()
        await self.refresh_all()
        self._bg_task = asyncio.create_task(self._periodic_refresh())

    async def stop(self) -> None:
        if self._bg_task:
            self._bg_task.cancel()
        if self._udp_transport:
            self._udp_transport.close()
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass
        if self._macos_proc and self._macos_proc.poll() is None:
            self._macos_proc.terminate()
        await self._client.aclose()

    def _start_macos_bridge(self) -> None:
        repo_root = Path(__file__).resolve().parents[4]
        candidates = [
            repo_root / "macos-cam" / ".build" / "release" / "macos-cam",
            repo_root / "macos-cam" / ".build" / "debug" / "macos-cam",
        ]
        binary = next((p for p in candidates if p.exists() and os.access(p, os.X_OK)), None)
        if not binary:
            return
        # Check if already listening
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", self.macos_cam_port)) == 0:
                return
        try:
            self._macos_proc = subprocess.Popen(
                [str(binary), "--port", str(self.macos_cam_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _start_mdns(self) -> None:
        try:
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(
                self._zc,
                "_ledcam._tcp.local.",
                _AndroidCamListener(self._on_mdns_found),
            )
        except Exception:
            pass

    async def _start_udp_listener(self) -> None:
        if not self._loop:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self.udp_beacon_port))
            self._udp_transport, _ = await self._loop.create_datagram_endpoint(
                lambda: _UdpBeaconProtocol(self._on_udp_beacon),
                sock=sock,
            )
        except Exception:
            pass

    def _on_mdns_found(self, ip: str, port: int) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.register_http_camera(f"http://{ip}:{port}", transport="wifi"),
                self._loop,
            )

    def _on_udp_beacon(self, ip: str, port: int, payload: dict[str, Any]) -> None:
        if not self._loop:
            return
        if payload.get("type") == "ledcam_macos_host":
            host_name = str(payload.get("host_name", ip))
            asyncio.run_coroutine_threadsafe(
                self.poll_macos_cameras(base=f"http://{ip}:{port}", host_label=host_name, transport="wifi"),
                self._loop,
            )
        else:
            asyncio.run_coroutine_threadsafe(
                self.register_http_camera(f"http://{ip}:{port}", transport="wifi"),
                self._loop,
            )

    async def _periodic_refresh(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            try:
                await self.poll_macos_cameras()
                await self.poll_adb_cameras()
                now = time.time()
                for cam in self.cameras.values():
                    if cam.transport == "wifi" and (now - cam.last_seen) > 20.0:
                        cam.online = False
            except Exception:
                pass

    async def refresh_all(self) -> list[CameraNode]:
        await asyncio.gather(
            self.poll_macos_cameras(),
            self.poll_adb_cameras(),
            return_exceptions=True,
        )
        return self.list_cameras()

    async def poll_macos_cameras(
        self,
        base: str | None = None,
        host_label: str | None = None,
        transport: str = "local",
    ) -> None:
        base = (base or f"http://127.0.0.1:{self.macos_cam_port}").rstrip("/")
        # Avoid duplicating local macOS bridge when our own LAN broadcast echoes back
        if transport == "wifi":
            try:
                local_ips = {
                    info[4][0]
                    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                }
                host_ip = base.split("://", 1)[-1].split(":", 1)[0]
                if host_ip in local_ips or host_ip == "127.0.0.1":
                    return
            except Exception:
                pass
        try:
            r = await self._client.get(f"{base}/devices", timeout=2.0)
            if r.status_code != 200:
                return
            for d in r.json().get("devices", []):
                raw_uid = str(d.get("unique_id", d["id"]))
                cid = str(d["id"]) if transport == "local" else f"macos:{host_label or base}:{raw_uid}"
                display_name = str(d.get("name", cid))
                if host_label and transport == "wifi":
                    display_name = f"{host_label} — {display_name}"
                intr = self.calibrated_profiles.get(cid)
                if intr is None and "intrinsics" in d:
                    intr = CameraIntrinsics(**d["intrinsics"])
                prev = self.cameras.get(cid)
                self.cameras[cid] = CameraNode(
                    id=cid,
                    name=display_name,
                    model=str(d.get("model", "")),
                    platform="macos",
                    transport=transport,  # type: ignore[arg-type]
                    base_url=base,
                    device_param=raw_uid,
                    width=int(d.get("width", 1920)),
                    height=int(d.get("height", 1080)),
                    online=True,
                    enabled=prev.enabled if prev is not None else True,
                    supports_manual_exposure=bool(d.get("supports_manual_exposure", False)),
                    config_locked_by_other_app=bool(d.get("config_locked_by_other_app", False)),
                    intrinsics=intr,
                    last_seen=time.time(),
                )
        except Exception:
            pass

    async def poll_adb_cameras(self) -> None:
        if not shutil.which("adb"):
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "devices", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            lines = out.decode("utf-8", errors="ignore").splitlines()[1:]
            serials = [ln.split()[0] for ln in lines if len(ln.split()) >= 2 and ln.split()[1] == "device"]
            for serial in serials:
                local_port = self._adb_ports.get(serial)
                if local_port is None:
                    local_port = self._next_adb_port
                    self._next_adb_port += 1
                    self._adb_ports[serial] = local_port
                fwd = await asyncio.create_subprocess_exec(
                    "adb", "-s", serial, "forward", f"tcp:{local_port}", "tcp:8788",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await fwd.wait()
                await self.register_http_camera(f"http://127.0.0.1:{local_port}", transport="usb_adb")
        except Exception:
            pass

    async def register_http_camera(self, base_url: str, transport: str = "wifi") -> CameraNode | None:
        base_url = base_url.rstrip("/")
        try:
            r = await self._client.get(f"{base_url}/info", timeout=2.0)
            if r.status_code != 200:
                return None
            d = r.json()
            cid = str(d.get("id", base_url))
            prev = self.cameras.get(cid)
            # Prefer Wi-Fi over USB if already known, or update URL if transport changed
            if prev and prev.transport == "wifi" and transport == "usb_adb" and (time.time() - prev.last_seen) < 8.0:
                return prev

            intr = self.calibrated_profiles.get(cid)
            if intr is None and "intrinsics" in d:
                intr = CameraIntrinsics(**d["intrinsics"])

            node = CameraNode(
                id=cid,
                name=str(d.get("name", cid)),
                model=str(d.get("model", "")),
                platform="android" if d.get("platform") == "android" else "http",
                transport=transport,  # type: ignore[arg-type]
                base_url=base_url,
                width=int(d.get("width", 1920)),
                height=int(d.get("height", 1080)),
                hardware_level=str(d.get("hardware_level", "")),
                battery=int(d.get("battery", -1)),
                online=True,
                enabled=prev.enabled if prev is not None else True,
                supports_manual_exposure=bool(d.get("supports_manual_exposure", False)),
                supports_manual_focus=bool(d.get("supports_manual_focus", False)),
                ev_min=int(d.get("ev_min", -12)),
                ev_max=int(d.get("ev_max", 12)),
                exposure_min_ns=int(d.get("exposure_min_ns", 100_000)),
                exposure_max_ns=int(d.get("exposure_max_ns", 100_000_000)),
                iso_min=int(d.get("iso_min", 50)),
                iso_max=int(d.get("iso_max", 3200)),
                current_config=dict(d.get("current_config", {})),
                intrinsics=intr,
                gravity_cam=d.get("gravity_cam"),
                last_seen=time.time(),
            )
            self.cameras[cid] = node
            return node
        except Exception:
            return None

    def list_cameras(self) -> list[CameraNode]:
        return sorted(self.cameras.values(), key=lambda c: (c.platform != "macos", c.name.lower()))

    def get_enabled_cameras(self) -> list[CameraNode]:
        return [c for c in self.list_cameras() if c.enabled and c.online]

    def set_enabled(self, camera_id: str, enabled: bool) -> CameraNode | None:
        c = self.cameras.get(camera_id)
        if c:
            c.enabled = enabled
        return c

    def _query_suffix(self, cam: CameraNode) -> str:
        return f"?device={cam.device_param}" if cam.device_param else ""

    async def configure_camera(self, camera_id: str, config: dict[str, Any]) -> dict[str, Any]:
        cam = self.cameras.get(camera_id)
        if not cam:
            raise KeyError(f"Unknown camera: {camera_id}")
        url = f"{cam.base_url}/config{self._query_suffix(cam)}"
        r = await self._client.post(url, json=config, timeout=4.0)
        res = r.json()
        if isinstance(res, dict):
            cam.current_config.update({k: v for k, v in res.items() if k != "ok"})
        return res

    async def lock_all_for_leds(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for cam in self.get_enabled_cameras():
            try:
                if cam.platform == "android":
                    results[cam.id] = await self.configure_camera(cam.id, {"led_preset": True})
                else:
                    results[cam.id] = await self.configure_camera(
                        cam.id, {"ae_lock": True, "awb_lock": True, "af_lock": True}
                    )
            except Exception as e:
                results[cam.id] = {"ok": False, "error": str(e)}
        return results

    async def capture_single(self, cam: CameraNode) -> tuple[np.ndarray, int]:
        """Grab one fresh BGR frame + nanosecond capture timestamp."""
        sep = "&" if cam.device_param else "?"
        url = f"{cam.base_url}/capture{self._query_suffix(cam)}{sep}fresh=1"
        r = await self._client.get(url, timeout=6.0)
        r.raise_for_status()
        arr = np.frombuffer(r.content, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"Failed to decode JPEG from {cam.id}")
        ts = int(r.headers.get("X-Capture-Timestamp-Ns", "0"))
        cam.width = img.shape[1]
        cam.height = img.shape[0]
        return img, ts

    async def capture_all_enabled(self) -> dict[str, tuple[np.ndarray, int]]:
        """Trigger fresh JPEG capture concurrently across all enabled cameras."""
        cams = self.get_enabled_cameras()
        if not cams:
            return {}
        coros = [self.capture_single(c) for c in cams]
        results = await asyncio.gather(*coros, return_exceptions=True)
        out: dict[str, tuple[np.ndarray, int]] = {}
        for cam, res in zip(cams, results):
            if isinstance(res, Exception):
                continue
            out[cam.id] = res
        return out
