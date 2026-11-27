# LED Mapping — Agent Guide

Multi-camera, multi-controller 3D WLED mapping and spatial effect studio (`host/`, `web/`, `android-cam/`, `macos-cam/`, `rust-engine/`).

## Reference Docs (Load on Demand)

Read the matching doc before editing code in its scope:

- **`docs/targeting-and-3d-solver.md`** — Load when modifying ternary ECC coding, optical decoding, multi-round 3D pose estimation, bundle adjustment, reflection rejection, or auto-leveling.
- **`docs/hardware-and-fleet.md`** — Load when modifying `android-cam/`, `macos-cam/`, `rust-engine/`, camera discovery/protocols, Camera2 intrinsics/orientation/gravity/self-update, or WLED DDP streaming.
- **`docs/api-and-ui.md`** — Load when modifying FastAPI routes, WebSockets, project storage, 3D effects, or `web/` Three.js Studio UI.

## Non-Negotiable Invariants

1. **Android Camera2 distortion**: Camera2 JPEGs arrive ISP-undistorted and Camera2 `LENS_DISTORTION` ordering differs from OpenCV. Apply stored `dist` only when `intr.source == "charuco_calibrated"`; zero `dist` for all other sources (`host/src/ledmap/mapping/reconstruct.py:_dist_from_intrinsics`).
2. **Stationary multi-round baseline guard**: Enforce physical baseline `min_baseline >= 0.25` (with `min_parallax_deg >= 5.0`) in pair selection, triangulation, and post-Viterbi filtering so repeated stationary cameras across rounds produce real parallax instead of zero-baseline collapse.
3. **Append-only `led_table`**: Keep existing `global_id` indices in `project.led_table` immutable once scanned; append newly selected controllers at `max(global_id) + 1` so stored `ScanRound` observations stay valid.
4. **macOS camera config lock (`EMEET STUDIO`)**: Treat `AVCaptureDevice.lockForConfiguration()` failures in `macos-cam` as soft external locks (`configLockedByOtherApp = true`); return `ok: true` and keep streaming frames with current device settings.
5. **Remote macOS TCC camera auth (`sb1`)**: Headless SSH cannot trigger macOS TCC prompts. Require a one-time interactive desktop run of `~/ledmap-macos-cam --port <port>` until `AVCaptureDevice.authorizationStatus(for: .video) == 3`; `./scripts/deploy-macos-cam.sh` gates background launch on this check.

## Verification

Run the check for every component you modify:

```bash
cd host && uv run pytest -v
cd web && npm run build
cd android-cam && ./gradlew assembleDebug
cd macos-cam && swift build -c release
cd rust-engine && cargo build --release
```

## Doc Maintenance (Auto-Update Rule)

Whenever you change an invariant, protocol, solver rule, API contract, or hardware workflow, update the single owning doc (`AGENTS.md` or one of `docs/*.md`) in the same turn and prune any stale lines. Keep each fact in one place; link instead of duplicating.
