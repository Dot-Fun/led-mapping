"""Dedicated real-time 60-120 FPS 3D effect render thread feeding DDPStreamer + decoupled WS mirror."""
from __future__ import annotations

import asyncio
import base64
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from ledmap.engine.effects import BUILTIN_EFFECTS, load_custom_effects
from ledmap.models import LEDDescriptor, Map3DResult, WLEDController
from ledmap.wled.ddp import DDPStreamer


class EffectEngine:
    def __init__(self, ddp: DDPStreamer, custom_dir: Path | None = None) -> None:
        self.ddp = ddp
        self.custom_dir = custom_dir
        self.running = False
        self.effect_id = "sweep_plane"
        self.fps = 60.0
        self.brightness = 0.75
        self.params: dict[str, Any] = dict(BUILTIN_EFFECTS["sweep_plane"]["defaults"])

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_task: asyncio.Task[None] | None = None

        self._controllers: list[WLEDController] = []
        self._led_table: list[LEDDescriptor] = []
        self._xyz = np.zeros((0, 3), dtype=np.float32)
        self._mapped_mask = np.zeros(0, dtype=bool)
        self._on_frame: Callable[[dict[str, Any]], Awaitable[None]] | None = None

        # Cached custom effects (reloaded only when file mtimes change)
        self._custom_cache: dict[str, dict[str, Any]] = {}
        self._custom_sig: tuple[tuple[str, int], ...] = ()
        self._last_custom_check = 0.0

        # Lock-free latest-frame slot for browser WebSocket mirroring
        self._latest_rgb: bytes | None = None
        self._latest_count: int = 0
        self._latest_seq: int = 0

        # Live performance telemetry
        self._actual_fps: float = 0.0
        self._render_ms: float = 0.0
        self._jitter_ms: float = 0.0
        self._frames_sent: int = 0

    def _get_custom_effects(self, force: bool = False) -> dict[str, dict[str, Any]]:
        if not self.custom_dir or not self.custom_dir.exists():
            return {}
        now = time.monotonic()
        if not force and (now - self._last_custom_check) < 0.5:
            return self._custom_cache
        self._last_custom_check = now
        try:
            sig = tuple(
                (p.name, p.stat().st_mtime_ns)
                for p in sorted(self.custom_dir.glob("*.py"))
                if not p.name.startswith("_")
            )
        except OSError:
            return self._custom_cache
        if sig != self._custom_sig:
            self._custom_cache = load_custom_effects(self.custom_dir)
            self._custom_sig = sig
        return self._custom_cache

    def _all_effects(self, force_custom: bool = False) -> dict[str, dict[str, Any]]:
        effs = dict(BUILTIN_EFFECTS)
        effs.update(self._get_custom_effects(force=force_custom))
        return effs

    def list_effects(self) -> list[dict[str, Any]]:
        all_eff = self._all_effects(force_custom=True)
        return [
            {
                "id": e["id"],
                "name": e["name"],
                "description": e["description"],
                "defaults": e["defaults"],
            }
            for e in all_eff.values()
        ]

    def set_scene(
        self,
        controllers: list[WLEDController],
        led_table: list[LEDDescriptor],
        map_3d: Map3DResult | None,
    ) -> None:
        ctrls = list(controllers)
        table = list(led_table)
        n = len(table)
        xyz = np.zeros((n, 3), dtype=np.float32)
        mask = np.zeros(n, dtype=bool)
        if map_3d:
            for led in map_3d.leds:
                if 0 <= led.global_id < n and led.source in ("measured", "interpolated"):
                    xyz[led.global_id] = (led.x, led.y, led.z)
                    mask[led.global_id] = True
        with self._lock:
            self._controllers = ctrls
            self._led_table = table
            self._xyz = xyz
            self._mapped_mask = mask
            if ctrls and table:
                self.ddp.prepare_layout(ctrls, table)

    def state_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "effect_id": self.effect_id,
                "fps": self.fps,
                "brightness": self.brightness,
                "params": dict(self.params),
                "actual_fps": round(self._actual_fps, 1) if self.running else 0.0,
                "render_ms": round(self._render_ms, 2) if self.running else 0.0,
                "jitter_ms": round(self._jitter_ms, 2) if self.running else 0.0,
                "frames_sent": self._frames_sent,
            }

    def update_live(
        self,
        effect_id: str | None = None,
        fps: float | None = None,
        brightness: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if effect_id is not None:
                self.effect_id = effect_id
            if fps is not None:
                self.fps = max(5.0, min(120.0, float(fps)))
            if brightness is not None:
                self.brightness = max(0.05, min(1.0, float(brightness)))
            if params is not None:
                self.params.update(params)

    async def start(
        self,
        effect_id: str | None = None,
        fps: float | None = None,
        brightness: float | None = None,
        params: dict[str, Any] | None = None,
        on_frame: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        effs = self._all_effects(force_custom=True) if effect_id is not None else None
        with self._lock:
            if effect_id is not None:
                self.effect_id = effect_id
                if effs and effect_id in effs and params is None:
                    self.params = dict(effs[effect_id]["defaults"])
            if fps is not None:
                self.fps = max(5.0, min(120.0, float(fps)))
            if brightness is not None:
                self.brightness = max(0.05, min(1.0, float(brightness)))
            if params is not None:
                self.params.update(params)
            if on_frame is not None:
                self._on_frame = on_frame

        if self.running and self._thread and self._thread.is_alive():
            return

        self._stop_evt.clear()
        self.running = True
        self._thread = threading.Thread(target=self._rt_thread_loop, name="ddp-rt", daemon=True)
        self._thread.start()
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._ws_mirror_loop())

    async def stop(self, blackout: bool = False) -> None:
        self.running = False
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 1.5)
        self._thread = None
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None
        with self._lock:
            self._actual_fps = 0.0
            self._render_ms = 0.0
            self._jitter_ms = 0.0
            ctrls = list(self._controllers)
        if blackout and ctrls:
            self.ddp.blackout(ctrls)

    def render_once(self, t: float) -> np.ndarray:
        with self._lock:
            n = len(self._led_table)
            xyz = self._xyz
            mask = self._mapped_mask
            eid = self.effect_id
            params = dict(self.params)
        if n == 0:
            return np.zeros((0, 3), dtype=np.uint8)
        effs = self._all_effects(force_custom=False)
        eff = effs.get(eid, BUILTIN_EFFECTS["sweep_plane"])
        rgb = np.zeros((n, 3), dtype=np.uint8)
        if np.any(mask):
            mapped_rgb = eff["fn"](xyz[mask], t, params)
            rgb[mask] = mapped_rgb
        else:
            synth = np.zeros((n, 3), dtype=np.float32)
            synth[:, 0] = np.linspace(-1.0, 1.0, n)
            rgb = eff["fn"](synth, t, params)
        return rgb

    def _rt_thread_loop(self) -> None:
        """Dedicated OS thread: monotonic deadline pacing + zero-blocking UDP DDP blast."""
        t0 = time.perf_counter()
        next_t = t0
        last_frame_t = t0
        intervals: deque[float] = deque(maxlen=60)
        render_times: deque[float] = deque(maxlen=60)
        last_stat_t = t0

        while not self._stop_evt.is_set():
            frame_start = time.perf_counter()
            t = frame_start - t0

            with self._lock:
                ctrls = self._controllers
                table = self._led_table
                xyz = self._xyz
                mask = self._mapped_mask
                eid = self.effect_id
                params = dict(self.params)
                brightness = self.brightness
                target_fps = self.fps

            n = len(table)
            if n > 0:
                effs = self._all_effects(force_custom=False)
                eff = effs.get(eid, BUILTIN_EFFECTS["sweep_plane"])
                rgb = np.zeros((n, 3), dtype=np.uint8)
                if np.any(mask):
                    rgb[mask] = eff["fn"](xyz[mask], t, params)
                else:
                    synth = np.zeros((n, 3), dtype=np.float32)
                    synth[:, 0] = np.linspace(-1.0, 1.0, n)
                    rgb = eff["fn"](synth, t, params)

                self.ddp.send_global_frame(ctrls, table, rgb, brightness=brightness)

                # Publish latest frame into non-blocking slot for the decoupled 20 FPS WS mirror
                raw_bytes = rgb.tobytes()
                with self._lock:
                    self._latest_rgb = raw_bytes
                    self._latest_count = n
                    self._latest_seq += 1
                    self._frames_sent += 1

            work_ms = (time.perf_counter() - frame_start) * 1000.0
            render_times.append(work_ms)
            dt = frame_start - last_frame_t
            last_frame_t = frame_start
            if 0.0005 < dt < 1.0:
                intervals.append(dt)

            if (frame_start - last_stat_t) >= 0.35 and len(intervals) >= 3:
                last_stat_t = frame_start
                arr_dt = np.fromiter(intervals, dtype=np.float64)
                mean_dt = float(np.mean(arr_dt))
                std_ms = float(np.std(arr_dt) * 1000.0)
                med_work = float(np.median(render_times))
                with self._lock:
                    self._actual_fps = (1.0 / mean_dt) if mean_dt > 0 else 0.0
                    self._jitter_ms = std_ms
                    self._render_ms = med_work

            period = 1.0 / max(target_fps, 5.0)
            next_t += period
            now = time.perf_counter()
            if next_t < now - period:
                next_t = now + period

            remaining = next_t - now
            if remaining > 0.0018:
                # Wake ~1.2 ms early then spin-finish for sub-millisecond frame pacing
                self._stop_evt.wait(remaining - 0.0012)
            while not self._stop_evt.is_set() and time.perf_counter() < next_t:
                pass

    async def _ws_mirror_loop(self) -> None:
        """Mirror the latest RGB frame to browser Three.js clients at ~20 FPS without ever blocking DDP."""
        last_sent_seq = -1
        while self.running:
            await asyncio.sleep(0.05)
            with self._lock:
                seq = self._latest_seq
                raw = self._latest_rgb
                count = self._latest_count
                cb = self._on_frame
            if cb and raw is not None and seq != last_sent_seq:
                last_sent_seq = seq
                b64 = base64.b64encode(raw).decode("ascii")
                try:
                    await asyncio.wait_for(
                        cb({"type": "live_rgb", "rgb_b64": b64, "count": count}),
                        timeout=0.05,
                    )
                except Exception:
                    pass
