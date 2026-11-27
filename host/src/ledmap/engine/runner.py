"""Async 30-60 FPS 3D effect render loop feeding DDPStreamer + WebSocket UI mirror."""
from __future__ import annotations

import asyncio
import base64
import time
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
        self.fps = 40.0
        self.brightness = 0.75
        self.params: dict[str, Any] = dict(BUILTIN_EFFECTS["sweep_plane"]["defaults"])

        self._task: asyncio.Task[None] | None = None
        self._controllers: list[WLEDController] = []
        self._led_table: list[LEDDescriptor] = []
        self._xyz = np.zeros((0, 3), dtype=np.float32)
        self._mapped_mask = np.zeros(0, dtype=bool)
        self._on_frame: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def list_effects(self) -> list[dict[str, Any]]:
        all_eff = dict(BUILTIN_EFFECTS)
        if self.custom_dir:
            all_eff.update(load_custom_effects(self.custom_dir))
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
        self._controllers = list(controllers)
        self._led_table = list(led_table)
        n = len(led_table)
        xyz = np.zeros((n, 3), dtype=np.float32)
        mask = np.zeros(n, dtype=bool)
        if map_3d:
            for led in map_3d.leds:
                if 0 <= led.global_id < n and led.source in ("measured", "interpolated"):
                    xyz[led.global_id] = (led.x, led.y, led.z)
                    mask[led.global_id] = True
        self._xyz = xyz
        self._mapped_mask = mask

    def state_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "effect_id": self.effect_id,
            "fps": self.fps,
            "brightness": self.brightness,
            "params": self.params,
        }

    async def start(
        self,
        effect_id: str | None = None,
        fps: float | None = None,
        brightness: float | None = None,
        params: dict[str, Any] | None = None,
        on_frame: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        if effect_id is not None:
            self.effect_id = effect_id
            effs = dict(BUILTIN_EFFECTS)
            if self.custom_dir:
                effs.update(load_custom_effects(self.custom_dir))
            if effect_id in effs and params is None:
                self.params = dict(effs[effect_id]["defaults"])
        if fps is not None:
            self.fps = max(5.0, min(60.0, float(fps)))
        if brightness is not None:
            self.brightness = max(0.05, min(1.0, float(brightness)))
        if params is not None:
            self.params.update(params)
        if on_frame is not None:
            self._on_frame = on_frame

        if self.running and self._task and not self._task.done():
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self, blackout: bool = False) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        if blackout and self._controllers:
            self.ddp.blackout(self._controllers)

    def render_once(self, t: float) -> np.ndarray:
        n = len(self._led_table)
        if n == 0:
            return np.zeros((0, 3), dtype=np.uint8)
        effs = dict(BUILTIN_EFFECTS)
        if self.custom_dir:
            effs.update(load_custom_effects(self.custom_dir))
        eff = effs.get(self.effect_id, BUILTIN_EFFECTS["sweep_plane"])
        rgb = np.zeros((n, 3), dtype=np.uint8)
        if np.any(self._mapped_mask):
            mapped_rgb = eff["fn"](self._xyz[self._mapped_mask], t, self.params)
            rgb[self._mapped_mask] = mapped_rgb
        else:
            # Fallback 1D gradient along index if no 3D map yet
            synth = np.zeros((n, 3), dtype=np.float32)
            synth[:, 0] = np.linspace(-1.0, 1.0, n)
            rgb = eff["fn"](synth, t, self.params)
        return rgb

    async def _loop(self) -> None:
        t0 = time.monotonic()
        last_ws = 0.0
        while self.running:
            frame_start = time.monotonic()
            t = frame_start - t0
            rgb = self.render_once(t)
            if len(rgb) > 0:
                self.ddp.send_global_frame(
                    self._controllers, self._led_table, rgb, brightness=self.brightness
                )
                # Mirror RGB to browser Three.js canvas at ~20 FPS
                if self._on_frame and (frame_start - last_ws) >= 0.05:
                    last_ws = frame_start
                    b64 = base64.b64encode(rgb.tobytes()).decode("ascii")
                    await self._on_frame({"type": "live_rgb", "rgb_b64": b64, "count": len(rgb)})

            period = 1.0 / max(self.fps, 5.0)
            elapsed = time.monotonic() - frame_start
            await asyncio.sleep(max(0.002, period - elapsed))
