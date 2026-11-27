<p align="left">
  <a href="https://www.dotfun.co">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/dotfun-logo-dark.svg" />
      <source media="(prefers-color-scheme: light)" srcset="docs/assets/dotfun-logo-light.svg" />
      <img alt="dotfun" src="docs/assets/dotfun-logo-light.svg" height="36" />
    </picture>
  </a>
</p>

<p align="left">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-FF3C00?style=flat-square" /></a>
  <a href="https://www.dotfun.co"><img alt="Built by dotfun" src="https://img.shields.io/badge/built%20by-dotfun-100F0F?style=flat-square" /></a>
  <img alt="WLED DDP" src="https://img.shields.io/badge/WLED-DDP%20%3A4048-100F0F?style=flat-square" />
</p>

# 3D Multi-Camera WLED Mapping Studio

Mapping randomly strung LEDs in 3D usually turns into a weekend of pain. You light one pixel at a time, wait ten minutes per camera angle, lose half the string to window reflections, and still end up with one controller that refuses to line up with the rest of the room.

This repo skips all of that. Point whatever cameras you already own—Android phones, MacBooks, USB 4K webcams—at any number of stock ESP32 WLED controllers on your network. Each scan round encodes every LED in ~2 seconds using a DC-balanced ternary error-correcting code over $\mathrm{GF}(3)$, strips out reflections with Strip Viterbi DP, solves true 3D coordinates with sparse bundle adjustment, and drives every controller together as one 3D canvas over UDP DDP (`:4048`).

We built it at [dotfun](https://www.dotfun.co) to map living rooms, staircases, whole houses, and trees in the woods without buying proprietary hardware or flashing custom WLED firmware.

---

## How It Actually Works

Most optical mappers flash binary on/off codes that pump camera auto-exposure, or they scan one LED at a time until your battery dies. We took a different call:

1. **Every LED flashes pure R, G, or B in ~2 seconds.** A systematic linear code over $\mathrm{GF}(3)$ with minimum Hamming distance $d_{\min} \ge 3$ corrects single-frame errors and keeps ~33/33/33% RGB balance across every frame so unlocked cameras don't pump exposure. For up to 727 LEDs, that's 9 frames; for ~59,000 LEDs, that's 13 frames.
2. **Decode first, localize second.** Instead of guessing blob centroids first and losing overlapping LEDs, the host calibrates a 3×3 camera color-crosstalk matrix, discards ambient lamps and lit walls with a temporal chromatic-swing mask, decodes every active pixel in one vectorized pass, and unmixes tight multi-LED overlaps with bounded NNLS.
3. **Walk around with your phone to close gaps.** Fixed cameras handle the backbone. You walk around the space with an Android phone, tap **SCAN ROUND** right on the phone screen, and add new vantage points. The solver enforces physical baseline and parallax guards (`min_baseline >= 0.25`, `min_parallax_deg >= 5°`) so stationary cameras across repeated rounds never collapse into fake floating clumps.
4. **Auto-level and paint the blind spots.** Android phones stream hardware gravity vectors (`TYPE_GRAVITY`) so the 3D scene lands upright instead of tilted. Hit **Paint Unmapped LEDs** and the physical strings light up in green (locked), yellow (loose or interpolated), orange (1 camera only), and red (unseen) so you know exactly where to point a phone next.
5. **Drive every WLED controller as one 3D canvas.** Built-in NumPy 3D shaders run at 30–60 FPS from Studio, custom Python shaders hot-reload from `host/effects_custom/`, and a standalone Rust binary (`ledmap-rt`) blasts zero-jitter DDP frames at ~0.32 ms per 2,000-LED frame.

**Stated limits:** Every camera needs to see a few LEDs in common with at least one other view (`>= 8` shared LEDs, fallback `5`) to land in the same 3D zone; disjoint rooms or distant trees solve as separate components until you capture a bridging round. Reflections on mirrors or glass still need real parallax across at least two physical camera positions to reject cleanly.

---

## What's in This Repo

| Directory | Stack | What it owns |
| :--- | :--- | :--- |
| **[`host/`](host/)** | Python 3.11+ (`uv`), FastAPI, OpenCV, SciPy | WLED mDNS discovery, UDP DDP (`:4048`) streamer, camera fleet manager, ternary ECC scanner, multi-round 3D solver + sparse BA, 3D spatial effect engine |
| **[`web/`](web/)** | Vite, React, TypeScript, Three.js | Full-bleed 3D Studio UI, camera fleet & live PIP tray, QR APK installer, walk-around scan loop, 3D point cloud & camera frustum inspector |
| **[`android-cam/`](android-cam/)** | Kotlin, Camera2 (`minSdk 24`) | Android camera node with manual AE/ISO/focus lock (plus EV+AE-lock fallback for older tablets), hardware gravity sensor, on-phone remote scan bar, and 1-tap Wi-Fi self-updater |
| **[`macos-cam/`](macos-cam/)** | Swift, `AVFoundation` | Native HTTP camera bridge (`:8781`) for local and remote Macs; tolerates external camera locks (`EMEET STUDIO`) without dropping frames |
| **[`rust-engine/`](rust-engine/)** | Rust, `rayon` (`ledmap-rt`) | Standalone zero-jitter 3D DDP player and Rayon parallel benchmark |

---

## Quick Start

Start the macOS camera bridge (`:8781`), FastAPI backend (`:8780`), and Three.js Studio UI (`:5173`) in one shot:

```bash
./scripts/dev.sh
```

Then open `http://localhost:5173`. Studio auto-discovers WLED controllers over mDNS, local macOS cameras over `:8781`, and Android phones over mDNS (`_ledcam._tcp`), UDP broadcast (`:8089`), or USB ADB.

Prefer running each piece by hand?

```bash
(cd macos-cam && swift build -c release)
(cd host && uv sync && uv run uvicorn ledmap.api.server:app --host 0.0.0.0 --port 8780)
(cd web && npm install && npm run dev -- --host)
```

---

## Phone Fleet & Field Workflow

You shouldn't have to sprint back to a laptop between camera angles.

- **Install over Wi-Fi via QR code:** Click **Add Phone QR** in Studio's bottom dock and point any Android phone camera at the QR code (or visit `http://<host-ip>:8780/install`). Download, install, open—the phone joins the fleet automatically.
- **Install over USB (batch):** Plug in one or more Android phones with USB debugging on and run `./scripts/install-android.sh`, then unplug.
- **1-tap Wi-Fi updates:** When you rebuild the APK (`cd android-cam && ./gradlew assembleDebug`), phones compare `/api/apk-version` against their installed build and light up an amber **Update** button. One tap pulls the new APK over Wi-Fi and installs in place.
- **Trigger scans from the field:** Once the Studio host talks to a phone, the phone's bottom bar unlocks **SCAN ROUND**, **MAP** (physical accuracy heatmap), **Undo**, and **FX** toggle. Walk to the next corner, aim, tap.

---

## Adding Remote Macs

Deploy `macos-cam` to another Mac on your LAN or Tailnet:

```bash
./scripts/deploy-macos-cam.sh <remote-mac> 8781
```

One macOS catch: headless SSH cannot pop the macOS camera permission prompt. On a brand-new remote Mac, open Terminal once on that Mac's desktop, run `~/ledmap-macos-cam --port 8781`, and click **Allow**. After `authorizationStatus == 3`, `deploy-macos-cam.sh` launches headless automatically.

---

## High-Speed Rust Player (`ledmap-rt`)

Once a project is mapped, you can close Python and run effects straight from compiled Rust:

```bash
cd rust-engine && cargo build --release

# Drive all mapped WLED controllers in a saved project at 60 FPS
./target/release/ledmap-rt play ../projects/<project>/project.json plasma_3d 60 0.8

# Run the Rayon 3D shader + ternary decoder benchmark
./target/release/ledmap-rt bench
```

Built-in `ledmap-rt` effects: `plasma_3d`, `expanding_sphere`, `helix_vortex`, `axis_test`, and `sweep_plane`.

---

## Deep Docs & Verification

Every subsystem has one owning reference doc under `docs/` (and [`AGENTS.md`](AGENTS.md) enforces keeping them current):

- **[`docs/targeting-and-3d-solver.md`](docs/targeting-and-3d-solver.md)**—Ternary ECC codebook, Decode-First NNLS unmixing, multi-round 3D pose solver, Strip Viterbi DP, sparse BA, and auto-leveling.
- **[`docs/hardware-and-fleet.md`](docs/hardware-and-fleet.md)**—Camera HTTP/MJPEG protocol, discovery transports, Android Camera2 specifics, `macos-cam`, WLED DDP framing, and `ledmap-rt`.
- **[`docs/api-and-ui.md`](docs/api-and-ui.md)**—FastAPI REST/WebSocket contract, append-only `led_table` storage, custom Python shaders, and the Three.js Studio UI.

Verify every component you touch:

```bash
(cd host && uv run pytest -v)
(cd web && npm run build)
(cd android-cam && ./gradlew assembleDebug)
(cd macos-cam && swift build -c release)
(cd rust-engine && cargo build --release)
```

---

## License & No-Babysitting Disclaimer

Released under the **[MIT License](LICENSE)**.

**Use this at your own risk.** We aren't in charge of your rig, and we aren't responsible for what happens when you run it. This software comes strictly **"AS IS"** with zero warranty and zero liability. If you overload a 5V power supply, drop a phone off a ladder, point a tripod into a puddle in the woods, or blast 2,000 LEDs at 100% white into your neighbor's bedroom at midnight—that's on you.

---

<p align="left">
  Built with curiosity and too many ESP32s by <a href="https://www.dotfun.co"><strong>dotfun</strong></a>—<strong>Run Good · Look Good · Feel Good</strong>.<br />
  <em>No Smoke, No Mirrors. Fluff Free, Fun Forward.</em>
</p>
