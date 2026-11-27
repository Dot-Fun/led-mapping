"""Built-in vectorized NumPy 3D spatial effects (<1 ms per frame for 2,000+ LEDs)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

import numpy as np

EffectFn = Callable[[np.ndarray, float, dict[str, Any]], np.ndarray]


def _hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Vectorized HSV [0,1] -> RGB uint8 (N, 3)."""
    h = np.mod(h, 1.0)
    i = np.floor(h * 6.0).astype(np.int32)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    i_mod = i % 6

    r = np.select([i_mod == 0, i_mod == 1, i_mod == 2, i_mod == 3, i_mod == 4, i_mod == 5], [v, q, p, p, t, v])
    g = np.select([i_mod == 0, i_mod == 1, i_mod == 2, i_mod == 3, i_mod == 4, i_mod == 5], [t, v, v, q, p, p])
    b = np.select([i_mod == 0, i_mod == 1, i_mod == 2, i_mod == 3, i_mod == 4, i_mod == 5], [p, p, t, v, v, q])
    rgb = np.stack([r, g, b], axis=1)
    return np.clip(rgb * 255.0, 0, 255).astype(np.uint8)


def _normalize_bbox(xyz: np.ndarray) -> np.ndarray:
    """Normalize arbitrary 3D coordinates into [-1, 1] isotropic cube for scale-invariant effects."""
    if len(xyz) == 0:
        return xyz
    lo = np.min(xyz, axis=0)
    hi = np.max(xyz, axis=0)
    center = 0.5 * (lo + hi)
    span = float(np.max(hi - lo))
    if span < 1e-6:
        return np.zeros_like(xyz)
    return (xyz - center) / (0.5 * span)


def effect_sweep_plane(xyz: np.ndarray, t: float, params: dict[str, Any]) -> np.ndarray:
    """Glowing 3D plane sweeping back and forth along X, Y, Z, or diagonal axis."""
    p = _normalize_bbox(xyz)
    speed = float(params.get("speed", 0.7))
    thickness = float(params.get("thickness", 0.25))
    axis = str(params.get("axis", "y")).lower()
    hue_shift = float(params.get("hue", 0.55))

    if axis == "x":
        coord = p[:, 0]
    elif axis == "z":
        coord = p[:, 2]
    elif axis == "diag":
        coord = (p[:, 0] + p[:, 1] + p[:, 2]) / 1.732
    else:
        coord = p[:, 1]

    pos = np.sin(t * speed * np.pi)
    dist = np.abs(coord - pos)
    val = np.exp(-((dist / max(thickness, 0.03)) ** 2))
    hue = (hue_shift + 0.25 * (coord + 1.0) * 0.5 + t * 0.05) % 1.0
    sat = np.full(len(p), 0.85, dtype=np.float32)
    return _hsv_to_rgb(hue, sat, val)


def effect_expanding_sphere(xyz: np.ndarray, t: float, params: dict[str, Any]) -> np.ndarray:
    """3D spherical shockwaves radiating from a movable 3D center."""
    p = _normalize_bbox(xyz)
    speed = float(params.get("speed", 1.1))
    freq = float(params.get("scale", 2.5))
    ox = float(params.get("origin_x", 0.0))
    oy = float(params.get("origin_y", 0.0))
    oz = float(params.get("origin_z", 0.0))

    r = np.linalg.norm(p - np.array([ox, oy, oz], dtype=np.float32), axis=1)
    wave = 0.5 + 0.5 * np.sin(r * freq * np.pi - t * speed * 2.0 * np.pi)
    val = wave ** 2.2
    hue = (r * 0.35 - t * speed * 0.2) % 1.0
    sat = np.full(len(p), 0.92, dtype=np.float32)
    return _hsv_to_rgb(hue, sat, val)


def effect_plasma_3d(xyz: np.ndarray, t: float, params: dict[str, Any]) -> np.ndarray:
    """Smooth volumetric 3D plasma field across all LEDs."""
    p = _normalize_bbox(xyz)
    speed = float(params.get("speed", 0.8))
    scale = float(params.get("scale", 2.4))
    tt = t * speed

    v1 = np.sin(p[:, 0] * scale + tt)
    v2 = np.sin(p[:, 1] * scale * 1.1 - tt * 1.3)
    v3 = np.sin(p[:, 2] * scale * 0.9 + tt * 0.7)
    v4 = np.sin(np.linalg.norm(p, axis=1) * scale * 1.2 - tt * 1.1)
    field = (v1 + v2 + v3 + v4) * 0.25  # [-1, 1]

    hue = (0.5 + 0.5 * field + tt * 0.06) % 1.0
    sat = np.full(len(p), 0.9, dtype=np.float32)
    val = np.clip(0.35 + 0.65 * (0.5 + 0.5 * np.cos(field * np.pi)), 0.0, 1.0)
    return _hsv_to_rgb(hue, sat, val)


def effect_helix_vortex(xyz: np.ndarray, t: float, params: dict[str, Any]) -> np.ndarray:
    """3D cylindrical rainbow spiral rotating around the vertical Y axis."""
    p = _normalize_bbox(xyz)
    speed = float(params.get("speed", 1.0))
    twist = float(params.get("scale", 2.0))
    angle = np.arctan2(p[:, 2], p[:, 0]) / (2.0 * np.pi)
    hue = (angle + p[:, 1] * 0.35 * twist - t * speed * 0.35) % 1.0
    sat = np.full(len(p), 0.95, dtype=np.float32)
    val = np.full(len(p), 0.95, dtype=np.float32)
    return _hsv_to_rgb(hue, sat, val)


def effect_rising_embers(xyz: np.ndarray, t: float, params: dict[str, Any]) -> np.ndarray:
    """Volumetric 3D fire & rising sparks moving upward along +Y."""
    p = _normalize_bbox(xyz)
    speed = float(params.get("speed", 1.2))
    scale = float(params.get("scale", 3.0))

    y_flow = p[:, 1] - t * speed * 0.9
    turbulence = (
        np.sin(p[:, 0] * scale * 1.7 + y_flow * scale * 2.2)
        * np.cos(p[:, 2] * scale * 1.5 - t * speed * 1.4)
        + 0.5 * np.sin(y_flow * scale * 4.0 + p[:, 0] * 3.0)
    )
    height_fade = np.clip(1.0 - 0.45 * (p[:, 1] + 1.0), 0.15, 1.0)
    heat = np.clip((0.55 + 0.45 * turbulence) * height_fade, 0.0, 1.0)

    # Fire palette: black -> deep red -> orange -> bright yellow-white
    r = np.clip(heat * 2.2, 0.0, 1.0)
    g = np.clip((heat - 0.35) * 1.9, 0.0, 1.0)
    b = np.clip((heat - 0.78) * 3.5, 0.0, 1.0)
    return np.clip(np.stack([r, g, b], axis=1) * 255.0, 0, 255).astype(np.uint8)


def effect_axis_test(xyz: np.ndarray, _t: float, _params: dict[str, Any]) -> np.ndarray:
    """Direct spatial XYZ -> RGB mapping for instant visual calibration sanity checks."""
    p = _normalize_bbox(xyz)
    rgb = np.clip((p + 1.0) * 0.5, 0.0, 1.0)
    return (rgb * 255.0).astype(np.uint8)


BUILTIN_EFFECTS: dict[str, dict[str, Any]] = {
    "sweep_plane": {
        "id": "sweep_plane",
        "name": "3D Sweeping Plane",
        "description": "Flat plane of light sweeping through 3D space (great for verifying calibration).",
        "fn": effect_sweep_plane,
        "defaults": {"speed": 0.7, "thickness": 0.22, "axis": "y", "hue": 0.55},
    },
    "expanding_sphere": {
        "id": "expanding_sphere",
        "name": "Expanding 3D Sphere",
        "description": "Spherical shockwaves radiating outward from a 3D origin.",
        "fn": effect_expanding_sphere,
        "defaults": {"speed": 1.0, "scale": 2.4, "origin_x": 0.0, "origin_y": 0.0, "origin_z": 0.0},
    },
    "plasma_3d": {
        "id": "plasma_3d",
        "name": "3D Volumetric Plasma",
        "description": "Smooth 3D interference waves flowing through all mapped controllers.",
        "fn": effect_plasma_3d,
        "defaults": {"speed": 0.8, "scale": 2.4},
    },
    "helix_vortex": {
        "id": "helix_vortex",
        "name": "3D Rainbow Vortex",
        "description": "Cylindrical rainbow helix spinning in 3D space.",
        "fn": effect_helix_vortex,
        "defaults": {"speed": 1.0, "scale": 2.0},
    },
    "rising_embers": {
        "id": "rising_embers",
        "name": "3D Rising Embers",
        "description": "Volumetric fire and rising sparks drifting upward.",
        "fn": effect_rising_embers,
        "defaults": {"speed": 1.1, "scale": 3.0},
    },
    "axis_test": {
        "id": "axis_test",
        "name": "XYZ Axis Test (RGB)",
        "description": "Maps X->Red, Y->Green, Z->Blue directly across the 3D bounding box.",
        "fn": effect_axis_test,
        "defaults": {},
    },
}


def load_custom_effects(custom_dir: Path) -> dict[str, dict[str, Any]]:
    """Hot-load any user .py files in host/effects_custom/ defining render(xyz, t, params)."""
    out: dict[str, dict[str, Any]] = {}
    if not custom_dir.exists():
        return out
    for py_file in sorted(custom_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"custom_eff_{py_file.stem}", py_file)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "render", None)
            if callable(fn):
                eid = f"custom:{py_file.stem}"
                out[eid] = {
                    "id": eid,
                    "name": getattr(mod, "NAME", py_file.stem.replace("_", " ").title()),
                    "description": getattr(mod, "DESCRIPTION", "Custom Python 3D effect"),
                    "fn": fn,
                    "defaults": getattr(mod, "DEFAULTS", {}),
                }
        except Exception:
            continue
    return out
