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
    if req.effect_id is not None:
        state.engine.effect_id = req.effect_id
    if req.fps is not None:
        state.engine.fps = max(5.0, min(60.0, float(req.fps)))
    if req.brightness is not None:
        state.engine.brightness = max(0.05, min(1.0, float(req.brightness)))
    if req.params is not None:
        state.engine.params.update(req.params)
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
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Install LEDMap Cam</title>
  <style>
    body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#06080d;color:#e2e8f0;font-family:-apple-system,system-ui,sans-serif;padding:24px}}
    .card{{max-width:420px;width:100%;background:#0f172a;border:1px solid #1e293b;border-radius:16px;padding:24px;text-align:center}}
    h1{{margin:0 0 8px;font-size:22px}}
    p{{color:#94a3b8;font-size:14px;line-height:1.5}}
    a.btn{{display:block;margin:20px 0 12px;padding:15px 18px;border-radius:12px;background:#0284c7;color:#fff;font-weight:700;font-size:16px;text-decoration:none}}
    .steps{{text-align:left;font-size:13px;color:#cbd5e1;background:#020617;padding:12px 14px;border-radius:10px;line-height:1.6}}
  </style>
</head>
<body>
  <div class="card">
    <h1>LEDMap Cam (Android)</h1>
    <p>Turn this Android phone or tablet into a wireless 3D LED mapping camera node ({size_mb} MB · Android 7.0+).</p>
    <a class="btn" href="/download/ledmap-cam.apk">Download &amp; Install APK</a>
    <div class="steps">
      1. Tap <b>Download &amp; Install APK</b> above.<br/>
      2. Open the downloaded file (allow Chrome "Install unknown apps" if prompted).<br/>
      3. Launch <b>LEDMap Cam</b> &amp; allow Camera permission — it will auto-appear in the Studio Camera Fleet!
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
