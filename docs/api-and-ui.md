# API, Effect Engine & Studio UI Reference

> **Maintenance:** Update this file whenever modifying `host/src/ledmap/api/`, `host/src/ledmap/engine/`, `host/src/ledmap/storage.py`, `host/src/ledmap/models.py`, or `web/`.

---

## 1. Data Model & Persistence

Files live under `projects/` at repo root (`host/src/ledmap/storage.py`, `models.py`, `cameras/manager.py`):

- **`projects/<safe_name>/project.json`** — Serialized `ProjectData` (`name`, `created_at`, `controllers`, `led_table`, `rounds`, `map_3d`). `_safe_name()` collapses non-`[A-Za-z0-9._-]` characters to `-` (fallback `"default"`). Server boots into `"default"` via `ProjectStore.get_or_create_default()`.
- **`projects/camera_profiles.json`** — Map of `{camera_id: CameraIntrinsics}` (`fx`, `fy`, `cx`, `cy`, `dist` [5], `source`). Loaded by `CameraFleetManager` on startup and updated by ChArUco calibration.

### Core Models (`models.py`)

| Model | Key Fields |
|---|---|
| `WLEDController` | `id`, `name`, `ip`, `led_count`, `version`, `arch`, `online`, `selected`, `mac` |
| `CameraNode` | `id`, `name`, `platform` (`macos\|android\|http`), `transport` (`local\|wifi\|usb_adb\|manual`), `base_url`, `device_param`, `width`, `height`, `battery`, `online`, `enabled`, `config_locked_by_other_app`, `intrinsics`, `gravity_cam` |
| `LEDDescriptor` | `global_id`, `controller_id`, `controller_name`, `led_index` |
| `ScanRound` | `round_id` (`round_001`), `label`, `mode` (`ternary_ecc\|delta_ternary\|sequential`), `created_at`, `duration_sec`, `enabled`, `views: ViewObservations[]` |
| `ViewObservations` | `view_id` (`"{round_id}:{camera_id}"`), `round_id`, `camera_id`, `camera_name`, `width`, `height`, `intrinsics`, `detections: dict[int, list[Candidate2D]]` (best-first), `color_matrix`, `gravity_cam`, `timestamp` |
| `Map3DResult` | `leds: MappedLED3D[]` (`source`: `measured\|interpolated\|unmapped`, `views_count`, `reproj_error_px`, `confidence`, `component_id`, `uncertainty`), `cameras: SolvedCameraPose[]`, `components: ComponentSummary[]`, counts, `median_reproj_px`, `solve_time_sec`, `scale_meters_per_unit`, `rotation_euler_deg` |

### Append-Only `led_table` Invariant

Once `project.led_table` exists, `POST /api/scan/start` refreshes `ip`/`online` on existing project controllers and **appends** newly selected controllers starting at `max(global_id) + 1`. Existing `global_id`s are preserved intact so earlier `ScanRound.views[*].detections` remain valid when expanding to additional WLED controllers mid-project.

---

## 2. FastAPI REST & WebSocket Contract

Server entry: `host/src/ledmap/api/server.py` (port `8780`, CORS `*`). Lifespan starts `WLEDRegistry` + `CameraFleetManager`, syncs the effect scene, and cleanly stops engine/fleet/DDP on shutdown. Camera routes use `{camera_id:path}` so IDs containing colons or slashes route safely.

### REST Endpoints

| Group | Method & Path | Body / Query & Behavior |
|---|---|---|
| **Controllers** | `GET /api/controllers` | List discovered WLED controllers. |
| | `POST /api/controllers/refresh` | Trigger mDNS/HTTP refresh. |
| | `POST /api/controllers/add` | `{ip}` — Probe IP and register (404 if unreachable). |
| | `POST /api/controllers/{controller_id}/select` | `{selected}` — Toggle scan inclusion. |
| | `POST /api/controllers/select-all` | `{selected}` — Bulk toggle all controllers. |
| | `POST /api/controllers/{controller_id}/identify` | Stops effect engine; flashes white/cyan/white/black via DDP. |
| **Cameras & Cal** | `GET /api/cameras` | List local macOS + Android + manual camera nodes. |
| | `POST /api/cameras/refresh` | Re-probe mDNS, UDP `:8089` beacons, and ADB devices. |
| | `POST /api/cameras/add` | `{base_url}` — Register HTTP camera node (`transport="manual"`). |
| | `POST /api/cameras/{camera_id:path}/enable` | `{enabled}` — Toggle scan participation. |
| | `POST /api/cameras/{camera_id:path}/config` | Arbitrary camera config JSON (e.g. `led_preset`, `ae_lock`, `awb_lock`, `af_lock`). |
| | `POST /api/cameras/lock-all` | Lock exposure, white balance, and focus for LEDs across enabled cameras. |
| | `GET /api/cameras/{camera_id:path}/snapshot` | Proxies `/capture?fresh=0` as `image/jpeg`. |
| | `GET /api/cameras/{camera_id:path}/preview` | Proxies MJPEG (`multipart/x-mixed-replace; boundary=ledcamframe`). |
| | `GET /api/calibration/charuco.png` | Renders printable/displayable ChArUco calibration PNG. |
| | `POST /api/cameras/{camera_id:path}/calibrate` | `{num_frames=8, delay_ms=900}` (clamped 4–20); broadcasts `cal_progress`; saves intrinsics to `camera_profiles.json`; returns `{intrinsics, rms_px, usable_frames}`. |
| **Projects & Scans** | `GET /api/projects` | Returns `{projects, active}`. |
| | `POST /api/projects` | `{name}` — Creates, saves, activates project, syncs engine scene. |
| | `POST /api/projects/{name}/load` | Loads saved project, activates, syncs engine scene. |
| | `GET /api/project` | Returns `{project, scanning}`. |
| | `POST /api/scan/start` | `{mode="ternary_ecc", label="", brightness=140, settle_ms=110}`. Guards with `scan_lock` (409 if busy), stops engine, enforces append-only `led_table`. `delta_ternary` scans ~15% spaced measured anchors + all non-measured LEDs. Auto-solves 3D map preserving prior scale/rotation, saves, syncs engine, broadcasts `scan_complete`, returns `{round, map_3d, summary_text}`. |
| | `POST /api/rounds/{round_id}/toggle` | `{enabled}` — Flips round flag and saves (caller invokes `/api/map/solve` to re-solve). |
| | `POST /api/rounds/undo-last` | Pops last round, re-solves, saves, syncs scene. |
| | `DELETE /api/rounds/{round_id}` | Removes round, re-solves, saves, syncs scene. |
| **3D Map & Tools** | `POST /api/map/solve` | `{interpolate_occluded=true, max_interp_gap=8, scale_meters_per_unit=1.0, rotation_euler_deg=[0,0,0]}` — Re-solves, saves, syncs scene. |
| | `POST /api/map/auto-level` | Re-solves base `[0,0,0]`, RANSAC-fits dominant vertical plane (`\|ny\| < 0.5`, >=10 measured LEDs in component 0), solves exact `Rx(pitch)` + `Ry(yaw)` Euler `[pitch, yaw, 0.0]` so wall faces `+Z`, re-solves, saves, syncs. |
| | `POST /api/map/paint-coverage` | Stops engine; paints physical LEDs via DDP: dim green `(0,55,12)` measured `<3.0px`, yellow `(220,190,0)` measured `>=3.0px` or interpolated, orange `(255,95,0)` 1-view unmapped, red `(255,0,0)` 0-view unmapped. |
| | `POST /api/map/blink-led` | `{global_id, duration_ms=1200}` (max 3s). Pauses engine if running, lights single LED white, restores engine or blackouts. |
| | `GET /api/map/export` | Returns raw `Map3DResult` JSON. |
| **Effects** | `GET /api/effects` | Returns `{effects, state}` (includes hot-loaded custom effects). |
| | `POST /api/effects/start` | `{effect_id?, fps?, brightness?, params?}` — Syncs scene and starts DDP render loop. |
| | `POST /api/effects/update` | Live-updates `effect_id`, `fps` (5–60), `brightness` (0.05–1.0), and merges `params`. |
| | `POST /api/effects/stop?blackout=false` | Stops loop; optionally blackouts WLEDs. |
| | `POST /api/effects/toggle` | Starts or stops engine (`blackout=false`), returns `{running, summary_text}`. |
| **Android APK / QR** | `GET /install` | Mobile HTML landing page with download button & setup steps. |
| | `GET /download/ledmap-cam.apk` | Serves `android-cam/app/build/outputs/apk/debug/app-debug.apk`. |
| | `GET /api/apk-version` | Returns `{available, build_id (sha256[:12]), mtime, size_bytes, download_path}`. |
| | `GET /api/install-urls` | Returns `{apk_available, size_mb, urls: [{label, ip, url}]}` prioritizing LAN (`192.168.0.x`/`192.168.x`/`10.x`) + Tailnet (`100.x`) on `:8780`. |
| | `GET /api/install-qr.png?url=...` | Generates PNG QR code for `url`. |

### WebSocket `/ws` Messages (Server -> Client)

Clients connect to `/ws`; inbound client text is drained and ignored. Broadcast payloads:

| `type` | Fields | Emitted When |
|---|---|---|
| `scan_progress` | `round_id`, `stage` (`preamble\|coding\|decoding\|sequential`), `step`, `total`, `message` | Each step during `run_scan_round()`. |
| `scan_complete` | `round_id` | After scan round solves & saves. |
| `cal_progress` | `camera_id`, `step`, `total`, `message` | Each frame during ChArUco capture. |
| `live_rgb` | `rgb_b64` (base64 flat `uint8` `N*3` bytes), `count` (`N`) | ~20 FPS (`>= 0.05s` throttle) while `EffectEngine` runs. |

---

## 3. 3D Spatial Effect Engine

Source: `host/src/ledmap/engine/effects.py`, `runner.py`.

- **Render Loop (`EffectEngine`):** Async loop at `5–60 FPS` (default `40 FPS`, `brightness` `0.05–1.0`, default `0.75`). Evaluates mapped LEDs (`source in {"measured", "interpolated"}`), leaves unmapped LEDs `(0, 0, 0)`, streams via `DDPStreamer.send_global_frame()` (UDP `:4048`), and mirrors `live_rgb` over `/ws` at ~20 FPS. Falls back to a synthetic 1D X-axis line `[-1, 1]` if no 3D map is solved yet.
- **Coordinate Normalization:** Built-in shaders call `_normalize_bbox(xyz)` to map arbitrary 3D world coordinates isotropically into `[-1, 1]` (centered on bbox midpoint, scaled by `0.5 * max(hi - lo)`).

### Built-in Shaders (`BUILTIN_EFFECTS`)

| `id` | Defaults | Description |
|---|---|---|
| `sweep_plane` *(default)* | `speed=0.7, thickness=0.22, axis="y" (x\|y\|z\|diag), hue=0.55` | Sweeping Gaussian light plane (best calibration check). |
| `expanding_sphere` | `speed=1.0, scale=2.4, origin_x=0, origin_y=0, origin_z=0` | Spherical shockwaves from movable 3D origin. |
| `plasma_3d` | `speed=0.8, scale=2.4` | 3D trigonometric interference plasma. |
| `helix_vortex` | `speed=1.0, scale=2.0` | Cylindrical rainbow spiral around `+Y`. |
| `rising_embers` | `speed=1.1, scale=3.0` | Volumetric upward fire turbulence along `+Y`. |
| `axis_test` | `{}` | Direct normalized `XYZ -> RGB` sanity check. |

### Hot-Reloadable Custom Effects (`host/effects_custom/*.py`)

Drop any `.py` file (names starting with `_` are skipped) into `host/effects_custom/`. `load_custom_effects()` reloads modules dynamically on `list_effects()`, `start()`, and every `render_once()` frame (invalid files are silently skipped):

```python
import numpy as np

NAME = "My Custom Effect"             # optional
DESCRIPTION = "Short UI description"  # optional
DEFAULTS = {"speed": 1.0}             # optional

def render(xyz: np.ndarray, t: float, params: dict) -> np.ndarray:
    # xyz: float32 (N, 3) raw world coords of mapped LEDs (normalize if scale-invariant)
    # return: uint8 (N, 3) RGB in [0, 255]
    ...
```

Registered automatically under effect ID `custom:<file_stem>`.

---

## 4. Three.js Browser Studio UI

Source: `web/src/App.tsx`, `web/src/components/Viewport3D.tsx`, `web/src/styles.css`, `web/vite.config.ts`.

### Full-Bleed Stage (`Viewport3D.tsx`)
- Full-viewport Three.js scene (`OrbitControls` with damping, `GridHelper` at `y=-1.2`, `AxesHelper`, `ResizeObserver`).
- Mapped LEDs (`source !== 'unmapped'`) render as `THREE.Points` (`PointsMaterial`, `size: 0.07`, vertex colors). Solved camera poses render as amber `ArrowHelper` + sphere markers when `showCameras` is enabled.
- Clicking a point raycasts (`threshold: 0.08`), opens the floating LED inspector card, and calls `POST /api/map/blink-led` (`duration_ms: 900`).
- Color modes: `live` (WS `live_rgb` with dark floor `>= (0.08, 0.09, 0.12)`), `coverage` (green `measured`, blue `interpolated`), `controller` (9-color controller palette). Selected LED highlights white.

### HUD, Drawer & Live PIP Layout (`App.tsx`, `styles.css`)
- **Top Bento HUD (`.hud-top`):** Brand + live DDP orb, 4 KPI chips (WLED Fleet, Camera Fleet, 3D Coverage bar, Precision / zone count), `⌘K` Command trigger.
- **Right Slide-Over Drawer (`.hud-drawer`, 356px):** 4 tabs (`effects`, `scan`, `cameras`, `controllers`) plus `closed`. Polls REST endpoints every 5s alongside live `/ws` updates.
- **Bottom Floating Dock (`.floating-dock`):** Play/Stop DDP, Scan Round, Paint Unmapped, Lock Cams, PIP toggle, Add Phone QR, Inspector toggle.
- **Live Camera PIP Tray (`.pip-tray`):** Collapsible bottom-left strip streaming `/api/cameras/{id}/preview`. Both `.pip-tile img` and `.drawer-preview` enforce **`object-fit: contain`** on `#020617` so portrait phone streams never crop or distort.

### Field Keyboard Shortcuts
Ignored when focus is in `INPUT`/`TEXTAREA`/`SELECT` or when `⌘K` is open:
- `Space` — Play / Stop DDP stream
- `S` — Start Scan Round
- `P` — Paint physical accuracy heatmap + switch viewport to `coverage`
- `L` — Lock All Camera Exposures
- `1` / `2` / `3` / `4` — Switch right drawer to `3D FX` / `Scan` / `Cams` / `WLED`
- `⌘K` / `Ctrl+K` (`Esc` closes) — Command Palette (field actions, effects, per-controller identify flash)

### Walk-Around Refine Auto-Loop & QR Modal
- **Walk-Around Refine (`App.tsx`):** Toggles a 1s countdown timer (`walkIntervalSec`, 5–20s, default 8s), pausing while `scanning` is active. At zero, runs `handleStartScan()` then `POST /api/map/paint-coverage` so the operator can walk around and target yellow/red LEDs hands-free.
- **QR Modal Prefetch & Retry:** `loadInstallUrls()` fetches `/api/install-urls` on initial mount and again on `openQrModal()`. On failure, it automatically retries once after `600ms` (handling backend restarts) and provides a manual Retry button.

### Vite Dev Proxy (`web/vite.config.ts`, port `5173`)
Proxies `/api`, `/download`, and `/install` to `http://127.0.0.1:8780`, and `/ws` to `ws://127.0.0.1:8780` (`ws: true`).
