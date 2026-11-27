"""WLED ESP32 live diagnostics, safe low-latency DDP config tuning, and native 2D ledmap.json export."""
from __future__ import annotations

import json
import math
from typing import Any

import httpx
import numpy as np

from ledmap.models import ProjectData, WLEDController


def _estimate_hw_max_fps(hw_led: dict[str, Any], led_count: int) -> tuple[int, int]:
    """Compute longest GPIO bus length and physical 800 kHz WS2812B wire FPS ceiling."""
    buses = hw_led.get("ins") or []
    max_pin_leds = 0
    for b in buses:
        try:
            max_pin_leds = max(max_pin_leds, int(b.get("len", 0)))
        except (TypeError, ValueError):
            pass
    if max_pin_leds <= 0:
        max_pin_leds = max(1, led_count)
    # 30 us per WS2812B LED + ~300 us reset/overhead per frame
    frame_us = max_pin_leds * 30 + 300
    hw_max_fps = max(1, min(333, int(1_000_000 / frame_us)))
    return max_pin_leds, hw_max_fps


def evaluate_wled_issues(info: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    """Inspect WLED /json/info + /json/cfg and list anything throttling live DDP smoothness."""
    leds = info.get("leds") or {}
    wifi_info = info.get("wifi") or {}
    wifi_cfg = cfg.get("wifi") or {}
    hw_led = (cfg.get("hw") or {}).get("led") or {}
    live_cfg = (cfg.get("if") or {}).get("live") or {}

    led_count = int(leds.get("count", 0))
    max_pin_leds, hw_max_fps = _estimate_hw_max_fps(hw_led, led_count)
    fps_cap = int(hw_led.get("fps", 0) or 0)
    wifi_sleep = bool(wifi_cfg.get("sleep", False))
    no_gc = bool(live_cfg.get("no-gc", False))
    live_en = bool(live_cfg.get("en", True))
    live_timeout = int(live_cfg.get("timeout", 25) or 25)

    issues: list[str] = []
    if wifi_sleep:
        issues.append("Wi-Fi sleep enabled (causes 50-100ms stutter)")
    if 0 < fps_cap < min(60, hw_max_fps):
        issues.append(f"FPS cap locked at {fps_cap} (hardware can do ~{hw_max_fps})")
    if not live_en:
        issues.append("Live UDP/DDP receive disabled")
    if not no_gc:
        issues.append("Live gamma correction on (adds per-frame CPU overhead)")

    meta = {
        "live": bool(info.get("live", False)),
        "live_mode": str(info.get("lm", "")),
        "live_fps": int(leds.get("fps", 0) or 0),
        "rssi": int(wifi_info.get("rssi", 0) or 0),
        "signal": int(wifi_info.get("signal", 0) or 0),
        "max_pin_leds": max_pin_leds,
        "hw_max_fps": hw_max_fps,
        "fps_cap": fps_cap,
        "wifi_sleep": wifi_sleep,
        "no_gc": no_gc,
        "live_en": live_en,
        "live_timeout": live_timeout,
    }
    return len(issues) == 0, issues, meta


async def inspect_wled_controller(
    ctrl: WLEDController,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Read-only probe of one ESP32 WLED controller's latency & FPS tuning state."""
    base = {
        "id": ctrl.id,
        "name": ctrl.name,
        "ip": ctrl.ip,
        "led_count": ctrl.led_count,
        "version": ctrl.version,
        "reachable": False,
        "optimized": False,
        "issues": ["Unreachable on network"],
    }
    try:
        r_info = await client.get(f"http://{ctrl.ip}/json/info", timeout=2.5)
        r_cfg = await client.get(f"http://{ctrl.ip}/json/cfg", timeout=2.5)
        if r_info.status_code != 200 or r_cfg.status_code != 200:
            return base
        info = r_info.json()
        cfg = r_cfg.json()
        optimized, issues, meta = evaluate_wled_issues(info, cfg)
        return {
            **base,
            "reachable": True,
            "optimized": optimized,
            "issues": issues,
            **meta,
        }
    except Exception as exc:
        base["issues"] = [f"Unreachable ({type(exc).__name__})"]
        return base


async def optimize_wled_controller(
    ctrl: WLEDController,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Safely patch WLED /json/cfg for lowest-latency DDP while preserving GPIO pins & Wi-Fi credentials."""
    r_cfg = await client.get(f"http://{ctrl.ip}/json/cfg", timeout=3.0)
    r_cfg.raise_for_status()
    cfg = r_cfg.json()

    # Preserve existing sub-objects verbatim and mutate only safe DDP performance flags
    wifi_cfg = dict(cfg.get("wifi") or {})
    wifi_cfg["sleep"] = False

    hw_cfg = dict(cfg.get("hw") or {})
    hw_led = dict(hw_cfg.get("led") or {})
    hw_led["fps"] = 0  # unlock software FPS cap (runs at bus/DDP speed)
    hw_cfg["led"] = hw_led

    if_cfg = dict(cfg.get("if") or {})
    live_cfg = dict(if_cfg.get("live") or {})
    live_cfg["en"] = True
    live_cfg["no-gc"] = True
    if int(live_cfg.get("timeout", 25) or 25) < 15:
        live_cfg["timeout"] = 25
    if_cfg["live"] = live_cfg

    patch = {
        "wifi": wifi_cfg,
        "hw": {"led": hw_led},
        "if": {"live": live_cfg},
    }
    r_post = await client.post(f"http://{ctrl.ip}/json/cfg", json=patch, timeout=4.0)
    r_post.raise_for_status()

    diag = await inspect_wled_controller(ctrl, client)
    return {"ok": True, "diagnostics": diag}


def build_wled_2d_ledmap(
    project: ProjectData,
    controller_id: str,
    max_dim: int = 40,
) -> dict[str, Any]:
    """Project a controller's solved 3D map onto an upright 2D WLED ledmap.json matrix.

    Uploading /ledmap.json to WLED NEVER blocks live 3D DDP streaming from Studio—it simply
    gives WLED a spatially accurate 2D map for its built-in onboard effects when Studio stops.
    """
    if not project.map_3d:
        raise ValueError("No solved 3D map yet")

    leds = [
        l
        for l in project.map_3d.leds
        if l.controller_id == controller_id and l.source in ("measured", "interpolated")
    ]
    if len(leds) < 2:
        raise ValueError("Controller needs at least 2 mapped LEDs to export a 2D ledmap")

    xs = np.array([l.x for l in leds], dtype=np.float64)
    ys = np.array([l.y for l in leds], dtype=np.float64)

    x_min, x_max = float(xs.min()), float(xs.max())
    y_min, y_max = float(ys.min()), float(ys.max())
    span_x = max(x_max - x_min, 1e-4)
    span_y = max(y_max - y_min, 1e-4)

    # Size grid cleanly to preserve aspect ratio with enough cells to separate adjacent LEDs
    target_cells = max(len(leds) * 3, 64)
    aspect = span_x / span_y
    h = int(round(math.sqrt(target_cells / max(aspect, 0.2))))
    w = int(round(h * aspect))
    max_d = max(8, min(64, int(max_dim)))
    scale = min(1.0, max_d / max(w, h, 1))
    w = max(4, min(max_d, int(round(w * scale))))
    h = max(4, min(max_d, int(round(h * scale))))

    # Ensure enough grid cells exist to place every LED uniquely
    while w * h < len(leds):
        if w <= h:
            w += 1
        else:
            h += 1

    grid = [-1] * (w * h)
    occupied: set[tuple[int, int]] = set()

    # Place LEDs in index order; WLED 2D (0, 0) is top-left, so flip +Y (up) -> row 0 (top)
    for l in sorted(leds, key=lambda item: item.led_index):
        gx = int(round(((l.x - x_min) / span_x) * (w - 1)))
        gy = int(round(((y_max - l.y) / span_y) * (h - 1)))
        gx = max(0, min(w - 1, gx))
        gy = max(0, min(h - 1, gy))

        if (gx, gy) in occupied:
            # Spiral search for nearest unoccupied cell
            best: tuple[int, int] | None = None
            best_d2 = 10**9
            for r in range(1, max(w, h) + 1):
                for dy in range(-r, r + 1):
                    ny = gy + dy
                    if not (0 <= ny < h):
                        continue
                    for dx in range(-r, r + 1):
                        if max(abs(dx), abs(dy)) != r:
                            continue
                        nx = gx + dx
                        if not (0 <= nx < w) or (nx, ny) in occupied:
                            continue
                        d2 = dx * dx + dy * dy
                        if d2 < best_d2:
                            best_d2 = d2
                            best = (nx, ny)
                if best is not None:
                    break
            if best is not None:
                gx, gy = best

        occupied.add((gx, gy))
        grid[gy * w + gx] = int(l.led_index)

    return {
        "n": f"dotfun_{project.name}_{controller_id}",
        "width": w,
        "height": h,
        "map": grid,
        "mapped_count": len(leds),
    }


async def upload_wled_2d_ledmap(
    ctrl: WLEDController,
    ledmap: dict[str, Any],
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Upload /ledmap.json to WLED LittleFS flash via POST /upload (non-locking 2D fallback)."""
    payload = {
        "n": ledmap.get("n", "dotfun_2d"),
        "width": int(ledmap["width"]),
        "height": int(ledmap["height"]),
        "map": ledmap["map"],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    files = {"data": ("/ledmap.json", raw, "application/json")}
    r = await client.post(f"http://{ctrl.ip}/upload", files=files, timeout=8.0)
    r.raise_for_status()
    return {
        "ok": True,
        "controller_id": ctrl.id,
        "name": ctrl.name,
        "width": payload["width"],
        "height": payload["height"],
        "mapped_count": ledmap.get("mapped_count", 0),
        "bytes": len(raw),
    }
