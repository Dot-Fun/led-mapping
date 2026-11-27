"""FastAPI backend for 3D Multi-Camera WLED Mapping & Spatial Effect Studio."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import qrcode
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from ledmap.cameras.manager import CameraFleetManager
from ledmap.engine.runner import EffectEngine
from ledmap.mapping.calibration import calibrate_camera_from_frames, render_charuco_png
from ledmap.mapping.reconstruct import solve_multi_round_3d
from ledmap.mapping.scanner import run_scan_round
from ledmap.models import LEDDescriptor, ProjectData
from ledmap.storage import ProjectStore
from ledmap.wled.ddp import DDPStreamer
from ledmap.wled.discovery import WLEDRegistry
from ledmap.wled.optimize import (
    build_wled_2d_ledmap,
    inspect_wled_controller,
    optimize_wled_controller,
    upload_wled_2d_ledmap,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
PROJECTS_DIR = REPO_ROOT / "projects"
CUSTOM_EFFECTS_DIR = REPO_ROOT / "host" / "effects_custom"
APK_PATH = REPO_ROOT / "android-cam" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"


class AppState:
    def __init__(self) -> None:
        self.store = ProjectStore(PROJECTS_DIR)
        self.wled = WLEDRegistry()
        self.ddp = DDPStreamer()
        self.fleet = CameraFleetManager(profiles_path=PROJECTS_DIR / "camera_profiles.json")
        self.engine = EffectEngine(self.ddp, custom_dir=CUSTOM_EFFECTS_DIR)
        self.project: ProjectData = self.store.get_or_create_default()
        self.ws_clients: set[WebSocket] = set()
        self.scan_lock = asyncio.Lock()
        self.scanning = False
        self._proxy_client = httpx.AsyncClient(timeout=10.0)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        if not self.ws_clients:
            return
        text = json.dumps(msg)
        dead: list[WebSocket] = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    def sync_engine_scene(self) -> None:
        ctrls = self.project.controllers or self.wled.list_controllers()
        self.engine.set_scene(ctrls, self.project.led_table, self.project.map_3d)


state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    loop = asyncio.get_running_loop()
    state.wled.start(loop)
    await state.fleet.start()
    state.sync_engine_scene()
    yield
    await state.engine.stop(blackout=False)
    await state.fleet.stop()
    state.wled.stop()
    state.ddp.close()
    await state._proxy_client.aclose()


app = FastAPI(title="3D WLED Mapping Studio", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request Bodies ──────────────────────────────────────────────────────────

class AddIpRequest(BaseModel):
    ip: str


class SelectRequest(BaseModel):
    selected: bool


class EnableRequest(BaseModel):
    enabled: bool


class AddCameraRequest(BaseModel):
    base_url: str


class ProjectCreateRequest(BaseModel):
    name: str


class ScanStartRequest(BaseModel):
    mode: str = "ternary_ecc"  # ternary_ecc | delta_ternary | sequential
    label: str = ""
    brightness: int = 140
    settle_ms: int = 110


class SolveRequest(BaseModel):
    interpolate_occluded: bool = True
    max_interp_gap: int = 8
    scale_meters_per_unit: float = 1.0
    rotation_euler_deg: list[float] = [0.0, 0.0, 0.0]


class EffectStartRequest(BaseModel):
    effect_id: str | None = None
    fps: float | None = None
    brightness: float | None = None
    params: dict[str, Any] | None = None


class BlinkLedRequest(BaseModel):
    global_id: int
    duration_ms: int = 1200


class CalibrateRequest(BaseModel):
    num_frames: int = 8
    delay_ms: int = 900


# ── WLED Controllers ────────────────────────────────────────────────────────

@app.get("/api/controllers")
async def get_controllers():
    return {"controllers": [c.model_dump() for c in state.wled.list_controllers()]}


@app.post("/api/controllers/refresh")
async def refresh_controllers():
    ctrls = await state.wled.refresh_all()
    return {"controllers": [c.model_dump() for c in ctrls]}


@app.post("/api/controllers/add")
async def add_controller(req: AddIpRequest):
    ctrl = await state.wled.probe_ip(req.ip.strip())
    if not ctrl:
        raise HTTPException(status_code=404, detail=f"No WLED controller found at {req.ip}")
    return {"controller": ctrl.model_dump()}


@app.post("/api/controllers/{controller_id}/select")
async def select_controller(controller_id: str, req: SelectRequest):
    c = state.wled.set_selected(controller_id, req.selected)
    if not c:
        raise HTTPException(status_code=404, detail="Controller not found")
    return {"controller": c.model_dump()}


@app.post("/api/controllers/select-all")
async def select_all_controllers(req: SelectRequest):
    for c in state.wled.controllers.values():
        c.selected = req.selected
    return {"controllers": [c.model_dump() for c in state.wled.list_controllers()]}


@app.post("/api/controllers/{controller_id}/identify")
async def identify_controller(controller_id: str):
    c = state.wled.controllers.get(controller_id)
    if not c:
        raise HTTPException(status_code=404, detail="Controller not found")
    await state.engine.stop(blackout=False)
    for col in ((255, 255, 255), (0, 220, 255), (255, 255, 255), (0, 0, 0)):
        state.ddp.fill_solid([c], *col)
        await asyncio.sleep(0.18)
    return {"ok": True}


@app.get("/api/controllers/diagnostics")
async def controllers_diagnostics():
    ctrls = state.wled.list_controllers()
    diags = await asyncio.gather(
        *(inspect_wled_controller(c, state._proxy_client) for c in ctrls)
    )
    opt_count = sum(1 for d in diags if d.get("optimized"))
    need_count = sum(1 for d in diags if d.get("reachable") and not d.get("optimized"))
    return {
        "diagnostics": list(diags),
        "optimized_count": opt_count,
        "needs_tune_count": need_count,
    }


@app.post("/api/controllers/optimize-all")
async def optimize_all_controllers():
    ctrls = state.wled.list_controllers()
    diags = await asyncio.gather(
        *(inspect_wled_controller(c, state._proxy_client) for c in ctrls)
    )
    to_tune = [
        c for c, d in zip(ctrls, diags) if d.get("reachable") and not d.get("optimized")
    ]
    tuned = 0
    errors: list[str] = []
    for c in to_tune:
        try:
            await optimize_wled_controller(c, state._proxy_client)
            tuned += 1
        except Exception as exc:
            errors.append(f"{c.name}: {exc}")
    updated = await asyncio.gather(
        *(inspect_wled_controller(c, state._proxy_client) for c in ctrls)
    )
    return {
        "ok": len(errors) == 0,
        "tuned_count": tuned,
        "errors": errors,
        "diagnostics": list(updated),
        "optimized_count": sum(1 for d in updated if d.get("optimized")),
        "needs_tune_count": sum(1 for d in updated if d.get("reachable") and not d.get("optimized")),
    }


@app.post("/api/controllers/{controller_id}/optimize")
async def optimize_one_controller(controller_id: str):
    c = state.wled.controllers.get(controller_id)
    if not c:
        raise HTTPException(status_code=404, detail="Controller not found")
    try:
        return await optimize_wled_controller(c, state._proxy_client)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to optimize {c.name}: {exc}")


@app.get("/api/controllers/{controller_id}/ledmap2d.json")
async def download_controller_ledmap_2d(controller_id: str, max_dim: int = 40):
    try:
        return build_wled_2d_ledmap(state.project, controller_id, max_dim=max_dim)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/controllers/{controller_id}/upload-ledmap2d")
async def upload_controller_ledmap_2d(controller_id: str, max_dim: int = 40):
    c = state.wled.controllers.get(controller_id) or next(
        (pc for pc in state.project.controllers if pc.id == controller_id), None
    )
    if not c:
        raise HTTPException(status_code=404, detail="Controller not found")
    try:
        ledmap = build_wled_2d_ledmap(state.project, controller_id, max_dim=max_dim)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return await upload_wled_2d_ledmap(c, ledmap, state._proxy_client)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to upload ledmap.json to {c.name}: {exc}")


# ── Unlimited Camera Fleet ──────────────────────────────────────────────────

@app.get("/api/cameras")
async def get_cameras():
    return {"cameras": [c.model_dump() for c in state.fleet.list_cameras()]}


@app.post("/api/cameras/refresh")
async def refresh_cameras():
    cams = await state.fleet.refresh_all()
    return {"cameras": [c.model_dump() for c in cams]}


@app.post("/api/cameras/add")
async def add_camera(req: AddCameraRequest):
    cam = await state.fleet.register_http_camera(req.base_url.strip(), transport="manual")
    if not cam:
        raise HTTPException(status_code=404, detail=f"No camera node reachable at {req.base_url}")
    return {"camera": cam.model_dump()}


@app.post("/api/cameras/{camera_id:path}/enable")
async def enable_camera(camera_id: str, req: EnableRequest):
    cam = state.fleet.set_enabled(camera_id, req.enabled)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"camera": cam.model_dump()}


@app.post("/api/cameras/{camera_id:path}/config")
async def config_camera(camera_id: str, body: dict[str, Any]):
    try:
        res = await state.fleet.configure_camera(camera_id, body)
        return res
    except KeyError:
        raise HTTPException(status_code=404, detail="Camera not found")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/cameras/lock-all")
async def lock_all_cameras():
    res = await state.fleet.lock_all_for_leds()
    return {"results": res}


@app.get("/api/cameras/{camera_id:path}/snapshot")
async def proxy_camera_snapshot(camera_id: str):
    cam = state.fleet.cameras.get(camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    suffix = f"?device={cam.device_param}&fresh=0" if cam.device_param else "?fresh=0"
    try:
        r = await state._proxy_client.get(f"{cam.base_url}/capture{suffix}", timeout=5.0)
        return Response(content=r.content, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/cameras/{camera_id:path}/preview")
async def proxy_camera_preview(camera_id: str):
    cam = state.fleet.cameras.get(camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    suffix = f"?device={cam.device_param}" if cam.device_param else ""
    url = f"{cam.base_url}/preview{suffix}"

    async def gen():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=16384):
                    yield chunk

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=ledcamframe")


@app.get("/api/calibration/charuco.png")
async def get_charuco_board():
    return Response(content=render_charuco_png(), media_type="image/png")


@app.post("/api/cameras/{camera_id:path}/calibrate")
async def calibrate_camera(camera_id: str, req: CalibrateRequest):
    cam = state.fleet.cameras.get(camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    frames = []
    for i in range(max(4, min(20, req.num_frames))):
        await state.broadcast(
            {
                "type": "cal_progress",
                "camera_id": camera_id,
                "step": i + 1,
                "total": req.num_frames,
                "message": f"Move ChArUco board ({i + 1}/{req.num_frames})",
            }
        )
        img, _ = await state.fleet.capture_single(cam)
        frames.append(img)
        await asyncio.sleep(req.delay_ms / 1000.0)
    try:
        intr, rms, usable = await asyncio.to_thread(calibrate_camera_from_frames, frames)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    state.fleet.save_calibrated_profile(camera_id, intr)
    return {"intrinsics": intr.model_dump(), "rms_px": round(rms, 3), "usable_frames": usable}


# ── Projects, Multi-Round Scans & 3D Solver ─────────────────────────────────

@app.get("/api/projects")
async def list_projects():
    return {"projects": state.store.list_projects(), "active": state.project.name}


@app.post("/api/projects")
async def create_project(req: ProjectCreateRequest):
    proj = ProjectData(name=req.name.strip() or "project", created_at=time.time())
    state.store.save(proj)
    state.project = proj
    state.sync_engine_scene()
    return {"project": proj.model_dump()}


@app.post("/api/projects/{name}/load")
async def load_project(name: str):
    proj = state.store.load(name)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    state.project = proj
    state.sync_engine_scene()
    return {"project": proj.model_dump()}


@app.get("/api/project")
async def get_active_project():
    return {"project": state.project.model_dump(), "scanning": state.scanning}


@app.post("/api/scan/start")
async def start_scan_round(req: ScanStartRequest):
    if state.scanning:
        raise HTTPException(status_code=409, detail="A scan is already running")

    async with state.scan_lock:
        state.scanning = True
        try:
            await state.engine.stop(blackout=False)

            # Dynamically append any newly selected WLED controllers to the project WITHOUT renumbering
            # existing global_ids — so you can expand to more rooms/ESP32s mid-project seamlessly!
            selected_ctrls = [c for c in state.wled.list_controllers() if c.selected and c.online]
            if not state.project.led_table:
                if not selected_ctrls:
                    raise HTTPException(status_code=400, detail="No online WLED controllers selected")
                state.project.controllers = list(selected_ctrls)
                state.project.led_table = state.wled.build_led_table(selected_only=True)
            else:
                existing_ids = {c.id for c in state.project.controllers}
                # Update live IPs/status for existing project controllers
                for pc in state.project.controllers:
                    live = state.wled.controllers.get(pc.id)
                    if live:
                        pc.ip = live.ip
                        pc.online = live.online
                # Append any newly selected controllers not yet in project.led_table
                next_gid = (max((d.global_id for d in state.project.led_table), default=-1) + 1)
                for sc in selected_ctrls:
                    if sc.id not in existing_ids:
                        state.project.controllers.append(sc)
                        for idx in range(sc.led_count):
                            state.project.led_table.append(
                                LEDDescriptor(
                                    global_id=next_gid,
                                    controller_id=sc.id,
                                    controller_name=sc.name,
                                    led_index=idx,
                                )
                            )
                            next_gid += 1

            active_ids: list[int] | None = None
            if req.mode == "delta_ternary" and state.project.map_3d:
                measured = [l.global_id for l in state.project.map_3d.leds if l.source == "measured"]
                unmapped = [l.global_id for l in state.project.map_3d.leds if l.source != "measured"]
                # Keep ~15% evenly spaced anchor LEDs for PnP lock-on + all unmapped LEDs
                stride = max(1, len(measured) // max(24, len(measured) // 7))
                anchors = measured[::stride]
                active_ids = sorted(set(anchors + unmapped))
                if not active_ids:
                    active_ids = None

            round_num = len(state.project.rounds) + 1
            round_id = f"round_{round_num:03d}"
            rnd = await run_scan_round(
                round_id=round_id,
                label=req.label or f"Round {round_num}",
                mode=req.mode,
                controllers=state.project.controllers,
                led_table=state.project.led_table,
                fleet=state.fleet,
                ddp=state.ddp,
                active_global_ids=active_ids,
                brightness=req.brightness,
                settle_ms=req.settle_ms,
                on_progress=state.broadcast,
            )
            state.project.rounds.append(rnd)

            # Automatically solve global 3D map across all rounds!
            prev_scale = state.project.map_3d.scale_meters_per_unit if state.project.map_3d else 1.0
            prev_rot = state.project.map_3d.rotation_euler_deg if state.project.map_3d else [0.0, 0.0, 0.0]
            state.project.map_3d = await asyncio.to_thread(
                solve_multi_round_3d,
                state.project.rounds,
                state.project.led_table,
                True,
                8,
                prev_scale,
                prev_rot,
            )
            state.store.save(state.project)
            state.sync_engine_scene()
            await state.broadcast({"type": "scan_complete", "round_id": round_id})
            m = state.project.map_3d
            summary_text = (
                f"R{round_num} ({rnd.duration_sec}s): {m.measured_count}+{m.interpolated_count}/{m.total_leds} LEDs · "
                f"{m.median_reproj_px}px · {len(m.components)} zone(s)"
            )
            return {"round": rnd.model_dump(), "map_3d": m.model_dump(), "summary_text": summary_text}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            state.scanning = False


@app.post("/api/rounds/{round_id}/toggle")
async def toggle_round(round_id: str, req: EnableRequest):
    for r in state.project.rounds:
        if r.round_id == round_id:
            r.enabled = req.enabled
            state.store.save(state.project)
            return {"round": r.model_dump()}
    raise HTTPException(status_code=404, detail="Round not found")


@app.post("/api/rounds/undo-last")
async def undo_last_round():
    if not state.project.rounds:
        raise HTTPException(status_code=400, detail="No rounds to undo")
    removed = state.project.rounds.pop()
    state.project.map_3d = await asyncio.to_thread(
        solve_multi_round_3d, state.project.rounds, state.project.led_table
    )
    state.store.save(state.project)
    state.sync_engine_scene()
    m = state.project.map_3d
    return {
        "removed_round_id": removed.round_id,
        "summary_text": f"Undid {removed.label} · {m.measured_count}+{m.interpolated_count}/{m.total_leds} mapped",
        "map_3d": m.model_dump(),
    }


@app.post("/api/effects/toggle")
async def toggle_effect():
    if state.engine.running:
        await state.engine.stop(blackout=False)
        return {"running": False, "summary_text": "3D DDP stopped"}
    state.sync_engine_scene()
    await state.engine.start(on_frame=state.broadcast)
    return {"running": True, "summary_text": f"Playing {state.engine.effect_id} @ {int(state.engine.fps)} FPS"}


@app.delete("/api/rounds/{round_id}")
async def delete_round(round_id: str):
    before = len(state.project.rounds)
    state.project.rounds = [r for r in state.project.rounds if r.round_id != round_id]
    if len(state.project.rounds) == before:
        raise HTTPException(status_code=404, detail="Round not found")
    state.project.map_3d = await asyncio.to_thread(
        solve_multi_round_3d, state.project.rounds, state.project.led_table
    )
    state.store.save(state.project)
    state.sync_engine_scene()
    return {"map_3d": state.project.map_3d.model_dump()}


@app.post("/api/map/solve")
async def solve_map(req: SolveRequest):
    if not state.project.led_table:
        raise HTTPException(status_code=400, detail="No LEDs in active project yet")
    state.project.map_3d = await asyncio.to_thread(
        solve_multi_round_3d,
        state.project.rounds,
        state.project.led_table,
        req.interpolate_occluded,
        req.max_interp_gap,
        req.scale_meters_per_unit,
        req.rotation_euler_deg,
    )
    state.store.save(state.project)
    state.sync_engine_scene()
    return {"map_3d": state.project.map_3d.model_dump()}


@app.post("/api/map/auto-level")
async def auto_level_map():
    """Auto-level the 3D scene so the dominant planar surface stands plumb vertical and faces +Z."""
    if not state.project.map_3d:
        raise HTTPException(status_code=400, detail="No solved 3D map yet")
    import math
    # Re-solve with [0,0,0] first so angles are measured from canonical base frame
    base_map = await asyncio.to_thread(
        solve_multi_round_3d,
        state.project.rounds,
        state.project.led_table,
        True,
        8,
        state.project.map_3d.scale_meters_per_unit,
        [0.0, 0.0, 0.0],
    )
    pts = np.array(
        [[l.x, l.y, l.z] for l in base_map.leds if l.source == "measured" and l.component_id == 0],
        dtype=np.float64,
    )
    if len(pts) < 10:
        raise HTTPException(status_code=400, detail="Need at least 10 measured LEDs to auto-level")

    # Fit dominant plane using RANSAC (so a dominant vertical wall like a wall tree wins cleanly
    # even when perpendicular stairs or branches are also present!)
    rng = np.random.default_rng(42)
    best_inliers = np.zeros(len(pts), dtype=bool)
    span = max(float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))), 1e-4)
    dist_thresh = 0.04 * span
    for _ in range(250):
        idx = rng.choice(len(pts), size=3, replace=False)
        v1 = pts[idx[1]] - pts[idx[0]]
        v2 = pts[idx[2]] - pts[idx[0]]
        n = np.cross(v1, v2)
        nn = float(np.linalg.norm(n))
        if nn < 1e-6:
            continue
        n /= nn
        # Only consider mostly-vertical planes (|ny| < 0.5)
        if abs(n[1]) > 0.5:
            continue
        d = np.abs((pts - pts[idx[0]]) @ n)
        inl = d < dist_thresh
        if int(np.sum(inl)) > int(np.sum(best_inliers)):
            best_inliers = inl

    sub = pts[best_inliers] if int(np.sum(best_inliers)) >= 10 else pts
    _, _, Vt = np.linalg.svd(sub - sub.mean(axis=0))
    normal = Vt[-1]
    if normal[2] < 0:
        normal = -normal
    # Exact solution for Ry(yaw) @ Rx(pitch) @ normal == [0, 0, 1]:
    # Step 1: Rx(pitch) zeroes y -> pitch = atan2(ny, nz), leaving [nx, 0, hypot(ny, nz)]
    # Step 2: Ry(yaw) zeroes x (preserving y=0) -> yaw = -atan2(nx, hypot(ny, nz))
    pitch_deg = math.degrees(math.atan2(normal[1], normal[2]))
    z_after_rx = math.hypot(normal[1], normal[2])
    yaw_deg = -math.degrees(math.atan2(normal[0], z_after_rx))
    rot_deg = [round(pitch_deg, 2), round(yaw_deg, 2), 0.0]

    state.project.map_3d = await asyncio.to_thread(
        solve_multi_round_3d,
        state.project.rounds,
        state.project.led_table,
        True,
        8,
        state.project.map_3d.scale_meters_per_unit,
        rot_deg,
    )
    state.store.save(state.project)
    state.sync_engine_scene()
    return {"rotation_euler_deg": rot_deg, "map_3d": state.project.map_3d.model_dump()}


@app.post("/api/map/paint-coverage")
async def paint_coverage():
    """3D Accuracy Heatmap on physical LEDs:
    - Dim GREEN:   solid measured 3D (< 3.0 px error)
    - YELLOW:      needs another camera angle (interpolated or >= 3.0 px error)
    - AMBER:       seen by only 1 camera view
    - Bright RED:  unmapped (0 views)
    """
    if not state.project.led_table:
        raise HTTPException(status_code=400, detail="No active project LEDs")
    await state.engine.stop(blackout=False)
    n = len(state.project.led_table)
    rgb = np.zeros((n, 3), dtype=np.uint8)
    if state.project.map_3d:
        for led in state.project.map_3d.leds:
            if led.source == "measured" and led.reproj_error_px < 3.0:
                rgb[led.global_id] = (0, 55, 12)      # dim green = rock-solid 3D!
            elif led.source == "measured" or led.source == "interpolated":
                rgb[led.global_id] = (220, 190, 0)    # yellow = needs a better camera angle!
            elif led.views_count == 1:
                rgb[led.global_id] = (255, 95, 0)     # orange = needs 1 more view
            else:
                rgb[led.global_id] = (255, 0, 0)      # bright red = unmapped!
    else:
        rgb[:] = (255, 0, 0)
    state.ddp.send_global_frame(state.project.controllers, state.project.led_table, rgb)
    return {"ok": True}


@app.post("/api/map/blink-led")
async def blink_single_led(req: BlinkLedRequest):
    if not state.project.led_table:
        raise HTTPException(status_code=400, detail="No active project LEDs")
    if not (0 <= req.global_id < len(state.project.led_table)):
        raise HTTPException(status_code=400, detail="Invalid global_id")
    was_running = state.engine.running
    if was_running:
        await state.engine.stop(blackout=False)
    rgb = np.zeros((len(state.project.led_table), 3), dtype=np.uint8)
    rgb[req.global_id] = (255, 255, 255)
    state.ddp.send_global_frame(state.project.controllers, state.project.led_table, rgb)
    await asyncio.sleep(min(3.0, req.duration_ms / 1000.0))
    if was_running:
        await state.engine.start(on_frame=state.broadcast)
    else:
        state.ddp.blackout(state.project.controllers)
    return {"ok": True}


@app.get("/api/map/export")
async def export_map():
    if not state.project.map_3d:
        raise HTTPException(status_code=404, detail="No solved 3D map yet")
    return state.project.map_3d.model_dump()


# ── 3D Spatial Effect Engine ────────────────────────────────────────────────

@app.get("/api/effects")
async def get_effects():
    return {"effects": state.engine.list_effects(), "state": state.engine.state_dict()}


@app.post("/api/effects/start")
async def start_effect(req: EffectStartRequest):
    state.sync_engine_scene()
    await state.engine.start(
        effect_id=req.effect_id,
        fps=req.fps,
        brightness=req.brightness,
        params=req.params,
        on_frame=state.broadcast,
    )
    return {"state": state.engine.state_dict()}


@app.post("/api/effects/update")
async def update_effect(req: EffectStartRequest):
    state.engine.update_live(
        effect_id=req.effect_id,
        fps=req.fps,
        brightness=req.brightness,
        params=req.params,
    )
    return {"state": state.engine.state_dict()}


@app.post("/api/effects/stop")
async def stop_effect(blackout: bool = False):
    await state.engine.stop(blackout=blackout)
    return {"state": state.engine.state_dict()}


# ── Wi-Fi / Tailnet Android APK Distribution & QR Codes ────────────────────

def _discover_host_urls(port: int = 8780) -> list[dict[str, str]]:
    ips: list[str] = []
    try:
        out = subprocess.check_output(["ifconfig"], text=True, timeout=2)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                ip = line.split()[1]
                if not ip.startswith("127.") and not ip.startswith("169.254."):
                    ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            ips.append(socket.gethostbyname(socket.gethostname()))
        except Exception:
            ips.append("127.0.0.1")

    urls: list[dict[str, str]] = []
    # Prioritize primary LAN Wi-Fi (192.168.0.x / 192.168.x / 10.x) and Tailnet (100.64..127.x)
    lan_ip = next((ip for ip in ips if ip.startswith("192.168.0.")), None) or next(
        (ip for ip in ips if ip.startswith("192.168.") or ip.startswith("10.")), None
    )
    ts_ip = next((ip for ip in ips if ip.startswith("100.")), None)
    if lan_ip:
        urls.append({"label": "Local Wi-Fi (Fastest)", "ip": lan_ip, "url": f"http://{lan_ip}:{port}/install"})
    if ts_ip:
        urls.append({"label": "Tailnet (Any Subnet)", "ip": ts_ip, "url": f"http://{ts_ip}:{port}/install"})
    if not urls and ips:
        urls.append({"label": "Host LAN", "ip": ips[0], "url": f"http://{ips[0]}:{port}/install"})
    return urls


@app.get("/api/install-urls")
async def get_install_urls():
    return {
        "apk_available": APK_PATH.exists(),
        "size_mb": round(APK_PATH.stat().st_size / (1024 * 1024), 2) if APK_PATH.exists() else 0,
        "urls": _discover_host_urls(8780),
    }


@app.get("/api/install-qr.png")
async def get_install_qr(url: str):
    qr = qrcode.QRCode(version=1, box_size=8, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/apk-version")
async def get_apk_version():
    if not APK_PATH.exists():
        return {"available": False}
    st = APK_PATH.stat()
    sha = hashlib.sha256(APK_PATH.read_bytes()).hexdigest()[:12]
    return {
        "available": True,
        "build_id": sha,
        "mtime": int(st.st_mtime),
        "size_bytes": st.st_size,
        "download_path": "/download/ledmap-cam.apk",
    }


@app.get("/download/ledmap-cam.apk")
async def download_android_apk():
    if not APK_PATH.exists():
        raise HTTPException(status_code=404, detail="APK not built yet — run ./gradlew assembleDebug in android-cam/")
    return FileResponse(
        path=str(APK_PATH),
        media_type="application/vnd.android.package-archive",
        filename="ledmap-cam.apk",
    )


@app.get("/install", response_class=HTMLResponse)
async def install_landing_page():
    size_mb = round(APK_PATH.stat().st_size / (1024 * 1024), 2) if APK_PATH.exists() else 0
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta name="theme-color" content="#100f0f"/>
  <title>dotfun — Install LEDMap Cam</title>
  <style>
    *{{box-sizing:border-box}}
    body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#100f0f;color:#fffbed;font-family:-apple-system,BlinkMacSystemFont,'Plus Jakarta Sans',system-ui,sans-serif;padding:22px}}
    .card{{max-width:420px;width:100%;background:#181514;border:1px solid rgba(255,251,237,0.14);border-radius:18px;padding:26px;text-align:left;box-shadow:0 24px 60px rgba(0,0,0,0.6)}}
    .brand{{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}}
    .pill{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#e9cf77;background:rgba(233,207,119,0.12);border:1px solid rgba(233,207,119,0.28);padding:4px 9px;border-radius:99px}}
    h1{{margin:0 0 8px;font-size:22px;letter-spacing:-.02em}}
    p{{color:#a69c95;font-size:14px;line-height:1.55;margin:0}}
    a.btn{{display:block;margin:20px 0 14px;padding:15px 18px;border-radius:12px;background:#ff3c00;color:#fffbed;font-weight:700;font-size:16px;text-align:center;text-decoration:none;box-shadow:0 8px 24px rgba(255,60,0,0.35)}}
    .steps{{font-size:13px;color:#dfd6cc;background:#100f0f;border:1px solid rgba(255,251,237,0.1);padding:14px 16px;border-radius:12px;line-height:1.65}}
    .steps b{{color:#fffbed}}
    .foot{{margin-top:16px;font-size:11px;color:#7e746e;display:flex;justify-content:space-between}}
    .foot a{{color:#e9cf77;text-decoration:none;font-weight:600}}
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">
      <svg height="24" viewBox="0 0 106 36" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="dotfun">
        <path d="M87.7432 27.781V9.33851H92.4977V10.8243C93.2035 10.2424 93.9835 9.78425 94.8378 9.44994C95.7046 9.11564 96.658 8.94849 97.698 8.94849C98.9857 8.94849 100.181 9.21469 101.282 9.7471C102.397 10.2795 103.332 11.0038 104.087 11.9201C104.694 12.6506 105.164 13.5111 105.498 14.5017C105.833 15.4922 106 16.7861 106 18.3833V27.781H101.041V18.0119C101.041 17.0956 100.948 16.3775 100.762 15.8574C100.577 15.3374 100.304 14.8979 99.9453 14.5388C99.2643 13.8578 98.3419 13.5173 97.178 13.5173C96.6456 13.5173 96.1441 13.5978 95.6736 13.7588C95.2031 13.9073 94.7759 14.0931 94.3921 14.3159C94.0083 14.5388 93.6678 14.7864 93.3706 15.0588C93.0858 15.3188 92.8568 15.5665 92.6834 15.8017V27.781H87.7432Z" fill="#FF3C00"/>
        <path d="M67.561 19.1075V9.33838H72.5013V19.2932C72.5013 20.1352 72.5818 20.7914 72.7428 21.2619C72.9161 21.7324 73.1452 22.1286 73.4299 22.4505C73.8138 22.8839 74.2471 23.1873 74.73 23.3606C75.2253 23.5216 75.7144 23.602 76.1972 23.602C76.7296 23.602 77.2187 23.5278 77.6645 23.3792C78.1102 23.2182 78.5126 23.0263 78.8717 22.8034C79.2431 22.5682 79.5589 22.3205 79.8189 22.0605C80.0913 21.7881 80.3141 21.5405 80.4875 21.3176V9.33838H85.4463V27.7808H80.6918V26.295C79.9613 26.9513 79.1688 27.428 78.3145 27.7251C77.4602 28.0099 76.5873 28.1523 75.6958 28.1523C74.4081 28.1523 73.2256 27.8799 72.1484 27.3351C71.0712 26.7903 70.1612 26.0784 69.4183 25.1993C68.8363 24.4688 68.3782 23.6144 68.0439 22.6363C67.722 21.6581 67.561 20.4819 67.561 19.1075Z" fill="#FF3C00"/>
        <path d="M55.7651 13.5172V9.33841V8.57694C55.7651 7.07876 55.9261 5.85916 56.248 4.91816C56.5823 3.97715 57.0776 3.19092 57.7338 2.55945C58.39 1.92799 59.1949 1.40796 60.1482 0.999366C61.1016 0.590772 62.216 0.386475 63.4913 0.386475C63.9989 0.386475 64.4818 0.417429 64.9399 0.479337C65.4104 0.528864 65.8995 0.615535 66.4072 0.739352L65.6457 4.9553C65.4476 4.90578 65.2061 4.86244 64.9214 4.8253C64.649 4.78815 64.4013 4.76958 64.1785 4.76958C62.9403 4.76958 62.0117 5.12246 61.3926 5.82821C61.1945 6.11299 61.0273 6.45967 60.8911 6.86827C60.7673 7.26448 60.7054 7.89595 60.7054 8.76266V9.33841H66.4072V13.5172H60.7054V27.7809H55.7651V13.5172Z" fill="#FF3C00"/>
        <path d="M41.9185 13.5172V9.33842V4.19385H46.8773V9.33842H52.9505V13.5172H46.8773V20.5562C46.8773 21.1381 46.9268 21.6086 47.0259 21.9677C47.1249 22.3144 47.2735 22.6177 47.4716 22.8778C47.7193 23.1625 48.0474 23.3854 48.456 23.5464C48.8646 23.7073 49.3784 23.7878 49.9975 23.7878C50.3566 23.7878 50.7342 23.7692 51.1304 23.7321C51.539 23.6949 51.83 23.6516 52.0033 23.6021L52.7648 27.7809C52.4305 27.9047 51.9538 27.9976 51.3347 28.0595C50.7156 28.1214 50.1646 28.1523 49.6818 28.1523C48.1836 28.1523 46.9021 27.9233 45.8372 27.4652C44.7724 26.9947 43.9243 26.3384 43.2928 25.4965C42.8099 24.865 42.4571 24.1221 42.2342 23.2678C42.0237 22.4011 41.9185 21.3053 41.9185 19.9805V13.5172Z" fill="#FFFBED"/>
        <path d="M0 18.569C0 17.1947 0.229061 15.9256 0.687182 14.7617C1.15768 13.5978 1.80772 12.5887 2.63729 11.7344C3.46686 10.8677 4.42644 10.1867 5.51603 9.6914C6.60561 9.19613 7.78187 8.9485 9.0448 8.9485C11.0754 8.9485 12.815 9.51806 14.2637 10.6572V0.776611H19.2039V27.781H14.4494V26.2952C13.6941 26.9514 12.8831 27.4281 12.0164 27.7253C11.1497 28.0101 10.2458 28.1524 9.30481 28.1524C7.93045 28.1524 6.63037 27.8739 5.40459 27.3167C4.17881 26.7595 3.16351 26.0042 2.3587 25.0508C1.62819 24.2089 1.05244 23.2369 0.631464 22.135C0.210488 21.033 0 19.8444 0 18.569ZM4.94028 18.569C4.94028 19.2996 5.0579 19.9744 5.29316 20.5934C5.54079 21.2001 5.88747 21.7264 6.33321 22.1721C6.77895 22.6178 7.29898 22.9707 7.8933 23.2307C8.48762 23.4784 9.12528 23.6022 9.80627 23.6022C10.7473 23.6022 11.6016 23.3917 12.3693 22.9707C13.1493 22.5497 13.7808 21.9988 14.2637 21.3178V15.8017C13.756 15.1208 13.106 14.5698 12.3136 14.1488C11.5335 13.7278 10.6606 13.5173 9.69483 13.5173C8.96431 13.5173 8.3019 13.6597 7.70758 13.9445C7.12564 14.2169 6.63037 14.5883 6.22178 15.0588C5.82557 15.5293 5.50983 16.0679 5.27458 16.6747C5.05171 17.2814 4.94028 17.9128 4.94028 18.569Z" fill="#FFFBED"/>
        <path d="M22.7344 33.6138H38.4736" stroke="#FF3C00" stroke-width="4.372"/>
        <path d="M20.5156 18.3927C20.5156 17.0778 20.7637 15.8312 21.2599 14.6527C21.7561 13.4619 22.4383 12.4385 23.3066 11.5826C24.1874 10.7143 25.2293 10.0259 26.4326 9.51727C27.6358 9.00869 28.9383 8.75439 30.34 8.75439C31.7417 8.75439 33.0442 9.00869 34.2474 9.51727C35.4506 10.0259 36.4926 10.7143 37.3733 11.5826C38.254 12.4385 38.9363 13.4619 39.4201 14.6527C39.9162 15.8312 40.1643 17.0778 40.1643 18.3927C40.1643 19.7448 39.9162 20.9976 39.4201 22.1512C38.9363 23.3049 38.254 24.322 37.3733 25.2027C36.4926 26.0835 35.4506 26.7719 34.2474 27.2681C33.0442 27.7519 31.7417 27.9937 30.34 27.9937C28.9383 27.9937 27.6358 27.7519 26.4326 27.2681C25.2293 26.7719 24.1874 26.0835 23.3066 25.2027C22.4383 24.322 21.7561 23.3049 21.2599 22.1512C20.7637 20.9976 20.5156 19.7448 20.5156 18.3927ZM25.465 18.3927C25.465 19.0749 25.5829 19.7262 25.8185 20.3464C26.0666 20.9542 26.3954 21.4876 26.8047 21.9466C27.2265 22.4055 27.735 22.7715 28.3305 23.0444C28.9383 23.3049 29.6081 23.4351 30.34 23.4351C31.047 23.4351 31.7045 23.3049 32.3123 23.0444C32.9325 22.7715 33.4473 22.4055 33.8566 21.9466C34.2784 21.4876 34.6071 20.9542 34.8428 20.3464C35.0909 19.7386 35.2149 19.0873 35.2149 18.3927C35.2149 17.7104 35.0909 17.0592 34.8428 16.439C34.6071 15.8187 34.2784 15.2792 33.8566 14.8202C33.4473 14.3612 32.9325 14.0015 32.3123 13.741C31.7045 13.4681 31.047 13.3316 30.34 13.3316C29.6081 13.3316 28.9383 13.4681 28.3305 13.741C27.735 14.0015 27.2265 14.3612 26.8047 14.8202C26.3954 15.2792 26.0666 15.8187 25.8185 16.439C25.5829 17.0592 25.465 17.7104 25.465 18.3927Z" fill="#FFFBED"/>
      </svg>
      <span class="pill">Android 7.0+</span>
    </div>
    <h1>LEDMap Cam</h1>
    <p>Turn this phone into a wireless 3D LED camera and field remote ({size_mb} MB).</p>
    <a class="btn" href="/download/ledmap-cam.apk">Download &amp; Install APK</a>
    <div class="steps">
      1. Tap <b>Download &amp; Install APK</b> above.<br/>
      2. Open the download (allow Chrome "Install unknown apps" once).<br/>
      3. Launch <b>LEDMap Cam</b> and allow Camera access—it joins the Studio fleet automatically.
    </div>
    <div class="foot">
      <span>Run Good · Look Good · Feel Good</span>
      <a href="https://www.dotfun.co">dotfun.co</a>
    </div>
  </div>
</body>
</html>"""


# ── Live WebSocket ──────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        state.ws_clients.discard(ws)
    except Exception:
        state.ws_clients.discard(ws)
