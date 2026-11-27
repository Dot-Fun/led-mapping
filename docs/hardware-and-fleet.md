# Hardware & Fleet Reference

> **Maintenance:** Update this document whenever changing `android-cam/`, `macos-cam/`, `rust-engine/`, `host/src/ledmap/wled/`, or `host/src/ledmap/cameras/`.

Authoritative protocol, device, and runtime reference for camera nodes, WLED DDP streaming, and the Rust player.

---

## 1. Unified Camera Node Protocol

All Android (`android-cam/`) and macOS (`macos-cam/`) camera nodes expose the same HTTP/1.1 interface (CORS `*`, `OPTIONS` → `204`, `Connection: close`).

- **Default ports:** Android `:8788` (binds first free port in `8788..8798`, else ephemeral `:0`); macOS `:8781` (`--port <n>`).

| Endpoint | Method | Behavior |
|---|---|---|
| `/health` | `GET` | `{"ok": true}` |
| `/devices` | `GET` | **macOS only.** `{"devices": [...]}` for all `AVCaptureDevice` video inputs, external cameras sorted ahead of built-in MacBook cameras. Select a specific device on all other routes via `?device=<unique_id>`. |
| `/info` | `GET` | Returns `id`, `name`, `model`, `platform`, `width`, `height`, `supports_manual_exposure`, `intrinsics` (`fx`, `fy`, `cx`, `cy`, `dist`, `source`). Android adds `hardware_level`, `battery`, `api_level`, `jpeg_orientation`, `device_orientation`, `gravity_cam`, AE/ISO/EV/focus ranges, `current_config`. macOS adds `unique_id`, `config_locked_by_other_app`. |
| `/config` | `POST` | Applies JSON settings. Android: `led_preset`, `auto_preset`, or raw fields (`manual_exposure`, `exposure_time_ns`, `exposure_ms`, `iso`, `ev_compensation`, `ae_lock`, `awb_lock`, `manual_focus`, `focus_distance`, `jpeg_quality`). macOS: `ae_lock`, `awb_lock`, `af_lock`, `jpeg_quality` (`0.3..0.98`). Host `lock_all_for_leds()` sends `{"led_preset": true}` to Android and `{"ae_lock": true, "awb_lock": true, "af_lock": true}` to macOS/HTTP nodes. |
| `/capture` | `GET` | Returns `image/jpeg`. Android always triggers a fresh `TEMPLATE_STILL_CAPTURE` (3.5 s timeout). macOS waits via semaphore for `frameSeq + 1` (3.0 s timeout) unless `?fresh=0` (returns latest buffered frame, used by Studio `/snapshot`). |
| `/preview` | `GET` | `multipart/x-mixed-replace; boundary=ledcamframe` MJPEG stream (~8 Hz: 125 ms delay Android, 120 ms macOS). |

### Protocol Headers

- **Response (`/capture`):**
  - `X-Capture-Timestamp-Ns`: Monotonic capture timestamp in nanoseconds (`SystemClock.elapsedRealtimeNanos()` on Android; `DispatchTime.now().uptimeNanoseconds` on macOS).
  - `X-Frame-Width`, `X-Frame-Height`: Captured JPEG dimensions in pixels.
- **Request (Host → Node):**
  - `X-Studio-Host: http://<lan-ip>:8780`: Sent on every `CameraFleetManager` request. Android `CameraHttpServer` records this header as `studioHostUrl` to enable on-phone field remote buttons and trigger `/api/apk-version` checks.

### Discovery & Transports (`host/src/ledmap/cameras/manager.py`)

`CameraFleetManager` runs four automatic discovery paths plus manual add:

1. **Local macOS bridge (`local`):** On startup, spawns `macos-cam/.build/{release,debug}/macos-cam --port 8781` if nothing is bound on `:8781`, waits up to 2 s, and polls `GET /devices`.
2. **mDNS (`wifi`):** Browses `_ledcam._tcp.local.` (registered by Android `DiscoveryBeacon` via `NsdManager`).
3. **UDP `:8089` broadcast beacons (`wifi`):** Listens on `0.0.0.0:8089` (`SO_REUSEADDR` + `SO_REUSEPORT`) for broadcasts to `255.255.255.255:8089`:
   - `type: "ledcam_beacon"` (Android, every 2.0 s) → `register_http_camera("http://<ip>:<port>", transport="wifi")`.
   - `type: "ledcam_macos_host"` (macOS, every 2.5 s) → `poll_macos_cameras(..., host_label, transport="wifi")`, ignoring local host IPs to prevent duplicating the local bridge, and namespacing IDs as `macos:<host_label>:<unique_id>`.
4. **USB ADB forwarding (`usb_adb`):** Every 5 s (`_periodic_refresh`), scans authorized `adb devices`, forwards `adb -s <serial> forward tcp:<18088+n> tcp:8788`, and registers `http://127.0.0.1:<local_port>`. If the same camera ID was seen on `wifi` within 8 s, `wifi` wins. `wifi` nodes flip `online=False` after 20 s without refresh.
5. **Manual / Tailscale (`manual`):** `POST /api/cameras/add` with `{"base_url": "http://<ip>:<port>"}` for cross-subnet or Tailscale hosts where mDNS and UDP broadcasts do not traverse.

---

## 2. Android Companion (`android-cam/`)

### Build & Install

- **Toolchain:** JDK 17 (`JavaVersion.VERSION_17`, `jvmTarget = "17"`), `compileSdk = 36`, `minSdk = 24` (Android 7.0+), `targetSdk = 35`, package `com.ledmapping.cam`.
- **Build APK:** `cd android-cam && ./gradlew assembleDebug` → `android-cam/app/build/outputs/apk/debug/app-debug.apk`.
- **USB batch install:** `scripts/install-android.sh` (builds if missing, runs `adb -s <serial> install -r -g` and launches `.MainActivity` on every attached device).
- **Wi-Fi initial install:** Open `http://<studio-ip>:8780/install` (or scan QR from `/api/install-urls` / `/api/install-qr.png`) to download `/download/ledmap-cam.apk`.
- **Runtime locks:** Holds `FLAG_KEEP_SCREEN_ON`, a 6-hour `PARTIAL_WAKE_LOCK`, and `WIFI_MODE_FULL_HIGH_PERF`.

### Camera2 Levels & Tablet Fallback (`CameraController.kt`)

- Selects first `LENS_FACING_BACK` camera. Picks rectangular formats (aspect `1.25..1.85`) closest to `1920`w (max `2560`w) for JPEG stills and `1280`w for preview.
- Always sets `CONTROL_VIDEO_STABILIZATION_MODE_OFF` and `LENS_OPTICAL_STABILIZATION_MODE_OFF` so pixel coordinates remain fixed across scan frames.
- `supportsManualAe` requires `CONTROL_AE_MODE_OFF`, non-null exposure + ISO ranges, and `hardwareLevel != LEGACY` (`FULL`/`LEVEL_3`, plus `LIMITED` devices exposing manual AE).
- **`applyLedScanPreset()`:**
  - **Manual AE supported:** `CONTROL_AE_MODE_OFF`, `exposureTimeNs = 4_000_000` (1/250 s), `iso = 100`.
  - **`LEGACY` / `LIMITED` tablet fallback:** Keeps `CONTROL_AE_MODE_ON`, sets `evCompensation = evRange.lower` (darkest EV), and `aeLock = true`.
  - **Both paths:** Sets `awbLock = true`; if `supportsManualFocus` (`CONTROL_AF_MODE_OFF` and `minFocusDist > 0`), locks `focusDistance = 0.35f` diopters (~3 m, clamped to `[0, minFocusDist]`).

### Orientation, Preview Sizing & Intrinsics

- `MainActivity` is portrait-locked (`android:screenOrientation="portrait"`). `OrientationEventListener` snaps physical orientation to `0/90/180/270` (`deviceOrientationDeg`).
- **Stills & preview:** Stills set `JPEG_ORIENTATION = (sensorOrientation + deviceOrientationDeg) % 360`. MJPEG preview (`grabPreviewJpeg`, max side 540 px) rotates the `TextureView` bitmap by `deviceOrientationDeg`.
- **Centered 3:4 `TextureView` (`configurePreviewTransform`):** In a portrait Activity, Android auto-rotates the landscape back-camera stream 90°, giving natural aspect `previewSize.height / previewSize.width` (3:4). Fit-center `TextureView` to `naturalAspect` inside its parent `FrameLayout` and clear any matrix (`setTransform(null)`) so the preview never stretches.
- **`fx`/`fy` & dimension swap:** Let `swapped = (currentJpegOrientation() % 180) != 0`. When `swapped` is true, `/info` exchanges `width`/`height` and swaps `fx`/`fy` (`fx = fyRaw`, `fy = fxRaw`, `cx = outW / 2`, `cy = outH / 2`).
- **Intrinsics priority:** API 28+ `LENS_INTRINSIC_CALIBRATION` scaled from `SENSOR_INFO_PRE_CORRECTION_ACTIVE_ARRAY_SIZE` (fallback `ACTIVE_ARRAY_SIZE`) to `captureSize` (`camera2_intrinsic`) → `LENS_INFO_AVAILABLE_FOCAL_LENGTHS` + `SENSOR_INFO_PHYSICAL_SIZE` (`camera2_focal_sensor`) → `0.85 * rawW` (`estimated_fov`).
- **Keep `dist = [0, 0, 0, 0, 0]`:** Android Camera2 ISP already undistorts JPEG outputs by default, and `LENS_DISTORTION` uses `[k1, k2, k3, p1, p2]` instead of OpenCV's `[k1, k2, p1, p2, k3]`. Always report zero distortion for raw Camera2 JPEGs; only override via saved ChArUco profiles (`projects/camera_profiles.json`).

### Gravity Vector (`TYPE_GRAVITY` → OpenCV Camera Frame)

- Samples `Sensor.TYPE_GRAVITY` (fallback `TYPE_ACCELEROMETER`) at `SENSOR_DELAY_UI` with an `0.85 / 0.15` exponential low-pass filter.
- `getGravityInOpenCvCamCoords()` converts Android device proper acceleration (`x=right, y=up, z=out-of-screen`, pointing UP) into a unit vector pointing **downward** in upright OpenCV image coordinates (`x=image right, y=image down, z=into scene`):
  1. Negate & normalize to downward portrait device frame: `(dx, dy, dz) = -g / ||g||`.
  2. Transform to upright portrait back-camera OpenCV frame: `(px, py, pz) = (dx, -dy, -dz)`.
  3. Rotate `(px, py)` by `-deviceOrientationDeg` to match upright rotated JPEG axes:
     - `0°`: `(px, py, pz)` · `90°`: `(-py, px, pz)` · `180°`: `(-px, -py, pz)` · `270°`: `(py, -px, pz)`.
- Published as `gravity_cam` in `/info` so the 3D solver auto-levels the scene to true vertical.

### On-Phone Field Remote Controls & 1-Tap Wi-Fi Self-Updater (`MainActivity.kt`)

Once `X-Studio-Host` populates `studioHostUrl`, the bottom bar drives the Studio directly from the field:

- **`⚡ SCAN ROUND`:** `POST /api/cameras/lock-all` → `POST /api/scan/start` (`mode: "ternary_ecc"`, `brightness: 165`, `settle_ms: 140`, 45 s timeout) → `POST /api/map/paint-coverage`.
- **`🎯 MAP`:** `POST /api/map/paint-coverage` (paints Green/Yellow/Red 3D coverage heatmap onto the physical LEDs).
- **`↩`:** `POST /api/rounds/undo-last`.
- **`▶ FX`:** `POST /api/effects/toggle`.
- **Local row:** `LED Lock` (`applyLedScanPreset`), `Auto AE` (`applyAutoPreset`), `Dim` (`screenBrightness = 0.02f`), `⬆ Update`.
- **1-tap self-updater:** On first Studio contact, `checkForUpdate()` polls `GET /api/apk-version` (`build_id` = first 12 hex chars of SHA-256 of `app-debug.apk`) and compares it to `SharedPreferences("ledmap_update")["last_build_id"]`. When newer, `btnUpdate` turns amber (`⬆ Update!`). Tapping it persists `last_build_id`, downloads `GET /download/ledmap-cam.apk` to `cacheDir/ledmap-cam-update.apk`, exposes it via `FileProvider` (`${packageName}.fileprovider`, `<cache-path>` in `res/xml/file_paths.xml`), and launches `Intent.ACTION_VIEW` (`application/vnd.android.package-archive`, `FLAG_GRANT_READ_URI_PERMISSION`, `REQUEST_INSTALL_PACKAGES`).

---

## 3. macOS Camera Bridge (`macos-cam/`)

- **Build:** SwiftPM (`swift-tools-version: 5.9`, macOS 13+). Run `cd macos-cam && swift build -c release` → `macos-cam/.build/release/macos-cam`.
- **Capture pipeline (`Sources/main.swift`):** `AVCaptureSession` + `AVCaptureVideoDataOutput` (`32BGRA`, `alwaysDiscardsLateVideoFrames = true`), GPU `CIContext` → JPEG. Estimates ~68° horizontal FOV intrinsics (`fx = fy = w / (2 * tan(34°))`, `dist = [0,0,0,0,0]`, `source = "estimated_fov"`) until refined by ChArUco or bundle adjustment.
- **EMEET STUDIO `lockForConfiguration` fallback:** Probes `device.lockForConfiguration()` on init, `/devices`, and `/config`. If another app (e.g., EMEET STUDIO controlling an EMEET PIXY) owns hardware config, `macos-cam` sets `config_locked_by_other_app = true` and `supports_manual_exposure = false`, keeps streaming frames normally, and returns HTTP `200` on `/config` with `{"ok": true, "applied_hardware_config": false, "config_locked_by_other_app": true, ...}`.
- **LAN discovery beacon:** `MacOSDiscoveryBeacon` broadcasts `type: "ledcam_macos_host"` (`host_name`, `ip`, `port`, `platform: "macos"`, `device_count`) to `255.255.255.255:8089` every 2.5 s (skipping `127.*` and `169.254.*`).
- **Remote MacBook deployment (`scripts/deploy-macos-cam.sh`):**
  ```bash
  scripts/deploy-macos-cam.sh sb1 [port]   # default port 8781
  ```
  Builds release binary if absent, `scp`s to `<host>:~/ledmap-macos-cam`, and queries `AVCaptureDevice.authorizationStatus(for: .video).rawValue` over SSH:
  - **One-time desktop TCC approval:** If `rawValue != 3` (`.authorized`), macOS blocks headless SSH camera authorization. Open Terminal **once on that Mac's desktop GUI**, run `~/ledmap-macos-cam --port <port>`, and click **Allow** on the Camera prompt.
  - Once `rawValue == 3`, re-running the script starts it headless (`nohup ~/ledmap-macos-cam --port <port> >/tmp/ledmap-macos-cam.log 2>&1 &`) and verifies `GET /devices`. On Tailscale-only hosts, register via Studio `POST /api/cameras/add` (`http://<tailscale-ip>:<port>`).

---

## 4. WLED Fleet & DDP (`host/src/ledmap/wled/` + `rust-engine/`)

### WLED Discovery (`wled/discovery.py`)

- `WLEDRegistry` browses mDNS `_http._tcp.local.` via `zeroconf`, filters service instances or hostnames containing `wled` (case-insensitive), and probes `GET http://<ip>/json/info` (2.5 s timeout, requires `"leds"` key).
- Reads `name`, `leds.count`, `ver`, `arch`, `mac` (controller ID = mDNS instance, else `wled-<mac[-6:]>` or `wled-<ip>`). Add unadvertised IPs via `POST /api/controllers/add`.
- `build_led_table(selected_only=True)` assigns contiguous `global_id`s across selected online controllers sorted by lowercase name.

### DDP v1 Packet Framing & Low-Latency Streaming (`wled/ddp.py`, `wled/optimize.py`)

- **Transport:** Non-blocking UDP to port `:4048` (`DDP_PORT`), configured with `SO_SNDBUF = 262144` and `IP_TOS = 0xA0` (DSCP CS5 / WMM Video-Voice priority over best-effort Wi-Fi traffic).
- **Zero-alloc scatter (`prepare_layout`):** Precomputes per-controller NumPy `(src_idx, dst_idx)` scatter arrays, reusable `(led_count, 3)` `uint8` buffers, and pre-built 10-byte DDP header `bytearray` packet slots once per scene; interleaves multi-packet chunks across controllers.
- **MTU sizing:** `DDP_MAX_LEDS_PER_PACKET = 480` (`480 * 3 = 1440` RGB bytes + 10-byte header = 1450 bytes, fitting inside 1500-byte MTU without IP fragmentation).
- **10-byte big-endian header (`!BBBB I H`):**
  - `flags` (`u8`): `0x40` (`DDP_VER1`) `|` (`0x01` `DDP_PUSH` **only on the last packet** of a controller's frame so WLED latches multi-packet frames atomically).
  - `seq` (`u8`): Cycles `1..15`, shared across all controllers in a `send_global_frame()` call.
  - `type` (`u8`): `0x01` (`DDP_TYPE_RGB`).
  - `output_id` (`u8`): `1` (default display).
  - `offset` (`u32` BE): Byte offset into the controller's RGB array.
  - `length` (`u16` BE): Payload byte count (`<= 1440`).
- **ESP32 Diagnostics, Tuning & Non-Locking 2D Fallback (`wled/optimize.py`):**
  - `inspect_wled_controller()` reads `/json/info` + `/json/cfg`, computes physical 800 kHz WS2812B bus max FPS (`1_000_000 / (max_pin_leds * 30 + 300)`), and flags `wifi.sleep == true`, `0 < hw.led.fps < min(60, hw_max_fps)`, `if.live.en == false`, or `if.live.no-gc == false`.
  - `optimize_wled_controller()` safely preserves GPIO pins and Wi-Fi credentials while patching `wifi.sleep = false`, `hw.led.fps = 0`, `if.live.en = true`, and `if.live.no-gc = true`.
  - `build_wled_2d_ledmap()` + `upload_wled_2d_ledmap()` projects a controller's upright 3D `(x, y)` layout to a collision-free WLED `/ledmap.json` on LittleFS (`POST /upload`) for native onboard 2D effects when Studio isn't streaming (DDP still takes over automatically whenever Studio plays).

### Standalone Rust Runtime (`rust-engine/`, binary `ledmap-rt`)

- **Build:** `cd rust-engine && cargo build --release` (`opt-level = 3`, `lto = true`, `codegen-units = 1` → `rust-engine/target/release/ledmap-rt`).
- **Zero-jitter DDP player:**
  ```bash
  ./rust-engine/target/release/ledmap-rt play projects/<name>/project.json [effect] [fps] [brightness]
  ```
  - Defaults: `effect=sweep_plane`, `fps=60.0`, `brightness=0.8`.
  - Built-in effects: `sweep_plane`, `expanding_sphere`, `plasma_3d`, `helix_vortex`, `axis_test`.
  - Loads `controllers` and `map_3d.leds` (excluding `source == "unmapped"`), normalizes bounding box to `[-1, 1]`, evaluates colors in parallel via Rayon (`par_iter`), blasts identical DDP v1 packets (`480` LEDs/pkt, atomic `PUSH` bit on final chunk, `seq 1..15`), and sleeps exact remaining frame period.
- **Benchmark:**
  ```bash
  ./rust-engine/target/release/ledmap-rt bench
  ```
  Measures Rayon `plasma_3d` throughput (2,000 LEDs × 1,000 frames, ms/frame) and Rayon Decode-First ternary correlation (50,000 active pixels × 2,000 codewords × 10 RGB frames).
