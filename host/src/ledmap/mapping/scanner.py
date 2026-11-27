"""Decode-First, Localize-Afterward multi-camera optical scanner with multi-PSF separation."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import cv2
import numpy as np
from scipy.optimize import nnls

from ledmap.cameras.manager import CameraFleetManager
from ledmap.mapping.codebook import TernaryECCCodebook
from ledmap.models import (
    CameraIntrinsics,
    Candidate2D,
    LEDDescriptor,
    ScanRound,
    ViewObservations,
    WLEDController,
)
from ledmap.wled.ddp import DDPStreamer


def estimate_color_matrix(
    dark_bgr: np.ndarray,
    red_bgr: np.ndarray,
    green_bgr: np.ndarray,
    blue_bgr: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Fit a camera-wide 3x3 linear color crosstalk matrix M (RGB_measured = M @ RGB_true).

    Returns (M, M_inv) in RGB channel order, excluding saturated pixels.
    """
    dark = dark_bgr[:, :, ::-1].astype(np.float32)  # BGR -> RGB
    r_diff = np.clip(red_bgr[:, :, ::-1].astype(np.float32) - dark, 0.0, 255.0)
    g_diff = np.clip(green_bgr[:, :, ::-1].astype(np.float32) - dark, 0.0, 255.0)
    b_diff = np.clip(blue_bgr[:, :, ::-1].astype(np.float32) - dark, 0.0, 255.0)

    cols = []
    for ch_idx, diff in enumerate((r_diff, g_diff, b_diff)):
        peak = np.max(diff, axis=2)
        # Non-saturated bright LED pixels
        mask = (peak >= 22.0) & (peak <= 242.0)
        if np.count_nonzero(mask) < 10:
            mask = peak >= 15.0
        if np.count_nonzero(mask) < 5:
            col = np.zeros(3, dtype=np.float32)
            col[ch_idx] = 1.0
        else:
            samples = diff[mask]
            # Weight by brightness and normalize L1
            vec = np.median(samples / np.maximum(np.sum(samples, axis=1, keepdims=True), 1e-4), axis=0)
            col = vec / max(float(np.sum(vec)), 1e-4)
        cols.append(col)

    M = np.stack(cols, axis=1).astype(np.float32)  # columns are observed R, G, B chromaticities
    # Guard against near-singular matrix (e.g., if camera saw no LEDs)
    if abs(float(np.linalg.det(M))) < 0.05:
        M = np.eye(3, dtype=np.float32)
    M_inv = np.linalg.inv(M).astype(np.float32)
    return M, M_inv


def refine_overlapping_cluster_nnls(
    roi_stack_rgb: np.ndarray,
    codebook: TernaryECCCodebook,
    candidate_code_indices: list[int],
    y0: int,
    x0: int,
) -> dict[int, tuple[float, float, float]]:
    """Unmix overlapping LEDs in a small spatial ROI using Non-Negative Least Squares.

    For each pixel in the ROI, its temporal trace (3*T,) is a non-negative linear combination
    of the overlapping LEDs' known temporal signatures. Solving NNLS per pixel recovers clean
    spatial amplitude maps for each LED independently, yielding un-merged sub-pixel centroids!
    """
    H, W, T, _ = roi_stack_rgb.shape
    K = len(candidate_code_indices)
    # Build (3*T, K) non-negative design matrix from pure ternary codewords
    A = np.zeros((T * 3, K), dtype=np.float32)
    for k, cid in enumerate(candidate_code_indices):
        for t in range(T):
            A[t * 3 + int(codebook.codes[cid, t]), k] = 1.0

    amps = np.zeros((K, H, W), dtype=np.float32)
    flat = roi_stack_rgb.reshape(H * W, T * 3)
    for p_idx in range(H * W):
        y_vec = flat[p_idx]
        if float(np.max(y_vec)) < 8.0:
            continue
        w, _ = nnls(A, y_vec)
        amps[:, p_idx // W, p_idx % W] = w

    refined: dict[int, tuple[float, float, float]] = {}
    yy, xx = np.mgrid[0:H, 0:W]
    for k, cid in enumerate(candidate_code_indices):
        amap = amps[k]
        peak = float(np.max(amap))
        if peak < 10.0:
            continue
        mask = amap >= 0.3 * peak
        weights = amap[mask]
        wsum = float(np.sum(weights))
        if wsum <= 1e-6:
            continue
        cx = float(np.sum(xx[mask] * weights) / wsum) + x0
        cy = float(np.sum(yy[mask] * weights) / wsum) + y0
        refined[cid] = (cx, cy, peak)
    return refined


def decode_camera_stack(
    dark_bgr: np.ndarray,
    rgb_cal_bgrs: tuple[np.ndarray, np.ndarray, np.ndarray],
    code_bgrs: list[np.ndarray],
    codebook: TernaryECCCodebook,
    active_global_ids: list[int],
    max_candidates_per_led: int = 3,
    min_activity: float = 16.0,
) -> tuple[dict[int, list[Candidate2D]], list[list[float]]]:
    """Decode-First, Localize-Afterward extraction for one camera view.

    Returns:
      - detections: {global_led_id: [Candidate2D, ...]} sorted best-first (retains reflections as
        secondary hypotheses so 3D geometry + Strip Viterbi DP can reject them cleanly).
      - color_matrix: 3x3 list of floats fitted from the RGB calibration preamble.
    """
    H, W = dark_bgr.shape[:2]
    T = codebook.n_frames
    assert len(code_bgrs) == T, f"Expected {T} code frames, got {len(code_bgrs)}"

    M, M_inv = estimate_color_matrix(dark_bgr, rgb_cal_bgrs[0], rgb_cal_bgrs[1], rgb_cal_bgrs[2])
    dark_rgb = dark_bgr[:, :, ::-1].astype(np.float32)

    # Stack code frames minus dark baseline: (T, H, W, 3) in RGB order
    stack = np.empty((T, H, W, 3), dtype=np.float32)
    for t, bgr in enumerate(code_bgrs):
        stack[t] = np.clip(bgr[:, :, ::-1].astype(np.float32) - dark_rgb, 0.0, 255.0)

    # Compute pixel activity AND temporal chromatic modulation!
    # Requiring real RGB color switching across the ternary code frames immediately rejects
    # static ambient lamps, walls, and auto-exposure brightness shifts (even on unlocked cameras!).
    frame_peaks = np.max(stack, axis=3)  # (T, H, W)
    frame_mins = np.min(stack, axis=3)   # (T, H, W)
    median_peak = np.median(frame_peaks, axis=0)  # (H, W)
    max_peak = np.max(frame_peaks, axis=0)
    # Chromaticity purity per frame: (max_ch - min_ch)
    chroma_max = np.max(frame_peaks - frame_mins, axis=0)
    # Channel range across time: real ternary LEDs switch between R, G, and B
    ch_swing = np.sum(np.max(stack, axis=0) - np.min(stack, axis=0), axis=2)

    # Estimate noise floor from a fast 1/16th strided subsample (instant even on 4K!)
    sub_med = median_peak[::4, ::4]
    noise_sigma = float(np.median(np.abs(sub_med - np.median(sub_med))) * 1.4826)
    thresh = max(min_activity, 5.5 * max(noise_sigma, 1.5))

    active_mask = (median_peak >= thresh) & (chroma_max >= max(14.0, thresh * 0.65)) & (ch_swing >= max(35.0, thresh * 1.8))
    ys, xs = np.nonzero(active_mask)
    if len(ys) == 0:
        return {}, M.tolist()

    # Safety cap for extremely bright/noisy unlocked 4K scenes: keep top 120,000 most chromatically active pixels
    if len(ys) > 120_000:
        scores_act = ch_swing[ys, xs]
        top_k = np.argpartition(scores_act, -120_000)[-120_000:]
        ys = ys[top_k]
        xs = xs[top_k]

    # Extract active pixel temporal traces: (P, T, 3)
    traces = stack[:, ys, xs, :].transpose(1, 0, 2)  # (P, T, 3)
    # Apply camera-wide inverse color crosstalk matrix: calibrated = traces @ M_inv.T
    cal_traces = np.clip(traces @ M_inv.T, 0.0, None)
    # Per-frame intensity normalization (makes decoding invariant to exposure drift / AE pumping!)
    frame_sum = np.sum(cal_traces, axis=2, keepdims=True)
    norm_traces = cal_traces / np.maximum(frame_sum, 1e-3)

    # DECODE FIRST across all active pixels in one vectorized pass
    dec = codebook.decode_traces(norm_traces, max_hamming=1, min_score=0.42, min_margin=0.10)

    valid_mask = dec.led_ids >= 0
    if not np.any(valid_mask):
        return {}, M.tolist()

    # Build decoded ID map for spatial grouping & touching-cluster detection
    id_map = np.full((H, W), -1, dtype=np.int32)
    v_ys = ys[valid_mask]
    v_xs = xs[valid_mask]
    v_ids = dec.led_ids[valid_mask]
    v_scores = dec.best_scores[valid_mask]
    v_margins = dec.margins[valid_mask]
    v_hamming = dec.hamming_dists[valid_mask]
    id_map[v_ys, v_xs] = v_ids

    # Detect spatially touching multi-ID clusters for NNLS unmixing (bounded for guaranteed <0.5s runtime)
    binary_valid = np.zeros((H, W), dtype=np.uint8)
    binary_valid[v_ys, v_xs] = 1
    n_cc, cc_labels, cc_stats, _ = cv2.connectedComponentsWithStats(binary_valid, connectivity=8)

    nnls_overrides: dict[tuple[int, int], tuple[float, float, float]] = {}
    nnls_count = 0
    for cc_id in range(1, n_cc):
        if nnls_count >= 25:
            break
        x0 = int(cc_stats[cc_id, cv2.CC_STAT_LEFT])
        y0 = int(cc_stats[cc_id, cv2.CC_STAT_TOP])
        bw = int(cc_stats[cc_id, cv2.CC_STAT_WIDTH])
        bh = int(cc_stats[cc_id, cv2.CC_STAT_HEIGHT])
        if bw > 28 or bh > 28:
            continue
        sub_ids = id_map[y0 : y0 + bh, x0 : x0 + bw]
        sub_cc = cc_labels[y0 : y0 + bh, x0 : x0 + bw] == cc_id
        present_ids = np.unique(sub_ids[sub_cc])
        present_ids = present_ids[present_ids >= 0]
        if 2 <= len(present_ids) <= 6:
            pad = 2
            rx0, ry0 = max(0, x0 - pad), max(0, y0 - pad)
            rx1, ry1 = min(W, x0 + bw + pad), min(H, y0 + bh + pad)
            roi_raw = stack[:, ry0:ry1, rx0:rx1, :].transpose(1, 2, 0, 3)  # (rh, rw, T, 3)
            roi_cal = np.clip(roi_raw @ M_inv.T, 0.0, None)
            refined = refine_overlapping_cluster_nnls(
                roi_cal, codebook, [int(x) for x in present_ids], ry0, rx0
            )
            for cid, vals in refined.items():
                nnls_overrides[(cc_id, cid)] = vals
            nnls_count += 1

    # O(P log P) sorted-slice grouping by decoded ID (avoids scanning full 4K image per LED!)
    order = np.argsort(v_ids, kind="mergesort")
    s_ids = v_ids[order]
    s_ys = v_ys[order]
    s_xs = v_xs[order]
    s_scores = v_scores[order]
    s_margins = v_margins[order]
    s_hamming = v_hamming[order]
    s_peaks = max_peak[s_ys, s_xs]

    split_pts = np.flatnonzero(np.diff(s_ids)) + 1
    starts = np.concatenate(([0], split_pts))
    ends = np.concatenate((split_pts, [len(s_ids)]))

    raw_candidates: dict[int, list[Candidate2D]] = {}
    for st, en in zip(starts, ends):
        cid = int(s_ids[st])
        if cid >= len(active_global_ids):
            continue
        gid = active_global_ids[cid]
        c_ys = s_ys[st:en]
        c_xs = s_xs[st:en]
        c_sc = s_scores[st:en]
        c_mg = s_margins[st:en]
        c_hm = s_hamming[st:en]
        c_pk = s_peaks[st:en]

        # Fast spatial clustering via local bounding box or small-ROI connectedComponents
        min_y, max_y = int(c_ys.min()), int(c_ys.max())
        min_x, max_x = int(c_xs.min()), int(c_xs.max())
        box_h = max_y - min_y + 1
        box_w = max_x - min_x + 1

        if box_h <= 256 and box_w <= 256:
            sub = np.zeros((box_h, box_w), dtype=np.uint8)
            sub[c_ys - min_y, c_xs - min_x] = 1
            num_blobs, blob_lbls = cv2.connectedComponents(sub, connectivity=8)
            p_blob = blob_lbls[c_ys - min_y, c_xs - min_x]
        else:
            # Split widely-separated candidate blobs (e.g. direct LED + far reflection) by 16px grid cells
            cell_y = c_ys // 16
            cell_x = c_xs // 16
            cell_keys = cell_y.astype(np.int64) * 100000 + cell_x.astype(np.int64)
            uniq_cells, p_blob = np.unique(cell_keys, return_inverse=True)
            p_blob = p_blob + 1
            num_blobs = len(uniq_cells) + 1

        cands: list[Candidate2D] = []
        for b in range(1, num_blobs):
            bm = p_blob == b
            gys = c_ys[bm]
            gxs = c_xs[bm]
            weights = c_pk[bm] * np.maximum(c_sc[bm], 0.1)
            wsum = float(np.sum(weights))
            if wsum <= 1e-6:
                continue
            cx = float(np.sum(gxs * weights) / wsum)
            cy = float(np.sum(gys * weights) / wsum)
            peak_val = float(np.max(c_pk[bm]))
            mean_sc = float(np.mean(c_sc[bm]))
            mean_mrg = float(np.mean(c_mg[bm]))
            min_ham = int(np.min(c_hm[bm]))
            var_r = float(np.sum(weights * ((gxs - cx) ** 2 + (gys - cy) ** 2)) / wsum)
            psf_sigma = float(np.sqrt(max(var_r, 0.25)))

            cc_id = int(cc_labels[gys[0], gxs[0]])
            if (cc_id, cid) in nnls_overrides:
                cx, cy, _ = nnls_overrides[(cc_id, cid)]

            snr = peak_val / max(noise_sigma, 1.0)
            compact_bonus = 1.0 / (1.0 + 0.15 * max(0.0, psf_sigma - 2.5))
            combined_score = mean_sc * (1.0 + 0.5 * mean_mrg) * compact_bonus * (1.0 - 0.15 * min_ham)
            cands.append(
                Candidate2D(
                    u=cx,
                    v=cy,
                    score=float(combined_score),
                    snr=float(snr),
                    hamming_dist=min_ham,
                    margin=mean_mrg,
                    psf_sigma=psf_sigma,
                    peak_intensity=peak_val,
                )
            )

        cands.sort(key=lambda c: c.score, reverse=True)
        if cands:
            raw_candidates[gid] = cands[:max_candidates_per_led]

    return raw_candidates, M.tolist()


async def run_scan_round(
    round_id: str,
    label: str,
    mode: str,
    controllers: list[WLEDController],
    led_table: list[LEDDescriptor],
    fleet: CameraFleetManager,
    ddp: DDPStreamer,
    active_global_ids: list[int] | None = None,
    brightness: int = 140,
    settle_ms: int = 110,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> ScanRound:
    """Execute one full multi-camera optical scan round across all enabled cameras & selected WLEDs."""
    t_start = time.monotonic()
    cameras = fleet.get_enabled_cameras()
    if not cameras:
        raise RuntimeError("No enabled online cameras available for scanning.")
    if not led_table:
        raise RuntimeError("No WLED controllers selected.")

    target_ids = active_global_ids if active_global_ids is not None else [d.global_id for d in led_table]
    total_leds = len(led_table)
    settle_sec = max(0.03, settle_ms / 1000.0)

    async def emit(stage: str, step: int, total: int, message: str) -> None:
        if on_progress:
            await on_progress(
                {
                    "type": "scan_progress",
                    "round_id": round_id,
                    "stage": stage,
                    "step": step,
                    "total": total,
                    "message": message,
                }
            )

    # 1. Dark baseline frame
    ddp.blackout(controllers)
    await asyncio.sleep(settle_sec + 0.08)
    dark_frames = await fleet.capture_all_enabled()
    if not dark_frames:
        raise RuntimeError("Failed to capture dark baseline frame from any camera.")

    cam_ids = sorted(dark_frames.keys())
    views: list[ViewObservations] = []

    if mode == "sequential":
        total_steps = len(target_ids)
        per_cam_dets: dict[str, dict[int, list[Candidate2D]]] = {cid: {} for cid in cam_ids}
        for step_idx, gid in enumerate(target_ids):
            await emit("sequential", step_idx + 1, total_steps, f"Scanning LED {step_idx + 1}/{total_steps}")
            frame_rgb = np.zeros((total_leds, 3), dtype=np.uint8)
            frame_rgb[gid] = (brightness, brightness, brightness)
            ddp.send_global_frame(controllers, led_table, frame_rgb)
            await asyncio.sleep(settle_sec)
            caps = await fleet.capture_all_enabled()
            for cid in cam_ids:
                if cid not in caps:
                    continue
                img = caps[cid][0]
                dark = dark_frames[cid][0]
                diff = cv2.absdiff(img, dark)
                gray = np.max(diff, axis=2)
                peak = float(np.max(gray))
                if peak < 20.0:
                    continue
                mask = (gray >= max(18.0, 0.5 * peak)).astype(np.uint8)
                n_b, lbls = cv2.connectedComponents(mask, connectivity=8)
                cands: list[Candidate2D] = []
                for b in range(1, n_b):
                    bys, bxs = np.nonzero(lbls == b)
                    w = gray[bys, bxs].astype(np.float32)
                    wsum = float(np.sum(w))
                    if wsum <= 0:
                        continue
                    cx = float(np.sum(bxs * w) / wsum)
                    cy = float(np.sum(bys * w) / wsum)
                    pk = float(np.max(w))
                    cands.append(Candidate2D(u=cx, v=cy, score=pk / 255.0, snr=pk / 3.0, peak_intensity=pk))
                cands.sort(key=lambda c: c.score, reverse=True)
                if cands:
                    per_cam_dets[cid][gid] = cands[:3]

        ddp.blackout(controllers)
        for cid in cam_ids:
            cam = fleet.cameras[cid]
            img = dark_frames[cid][0]
            intr = cam.intrinsics or CameraIntrinsics(
                fx=img.shape[1] * 0.85,
                fy=img.shape[1] * 0.85,
                cx=img.shape[1] / 2.0,
                cy=img.shape[0] / 2.0,
            )
            views.append(
                ViewObservations(
                    view_id=f"{round_id}:{cid}",
                    round_id=round_id,
                    camera_id=cid,
                    camera_name=cam.name,
                    width=img.shape[1],
                    height=img.shape[0],
                    intrinsics=intr,
                    detections=per_cam_dets[cid],
                    gravity_cam=cam.gravity_cam,
                    timestamp=time.time(),
                )
            )
    else:
        # Fast DC-Balanced Ternary ECC (works for both ternary_ecc and delta_ternary)
        codebook = TernaryECCCodebook(len(target_ids))
        total_steps = 4 + codebook.n_frames
        await emit("preamble", 1, total_steps, "Captured dark baseline")

        # RGB Calibration Preamble (3 frames)
        cal_colors = [
            ("red", (brightness, 0, 0)),
            ("green", (0, brightness, 0)),
            ("blue", (0, 0, brightness)),
        ]
        cal_captures: list[dict[str, tuple[np.ndarray, int]]] = []
        for idx, (cname, col) in enumerate(cal_colors):
            frame_rgb = np.zeros((total_leds, 3), dtype=np.uint8)
            frame_rgb[target_ids] = col
            ddp.send_global_frame(controllers, led_table, frame_rgb)
            await asyncio.sleep(settle_sec)
            cal_captures.append(await fleet.capture_all_enabled())
            await emit("preamble", 2 + idx, total_steps, f"Color calibration ({cname})")

        # Ternary ECC Code Frames
        code_captures: list[dict[str, tuple[np.ndarray, int]]] = []
        for f_idx in range(codebook.n_frames):
            frame_rgb = np.zeros((total_leds, 3), dtype=np.uint8)
            pat = codebook.frame_rgb_pattern(f_idx, brightness=brightness)
            frame_rgb[target_ids] = pat
            ddp.send_global_frame(controllers, led_table, frame_rgb)
            await asyncio.sleep(settle_sec)
            code_captures.append(await fleet.capture_all_enabled())
            await emit(
                "coding",
                5 + f_idx,
                total_steps,
                f"Ternary ECC frame {f_idx + 1}/{codebook.n_frames}",
            )

        ddp.blackout(controllers)
        await emit("decoding", total_steps, total_steps, f"Decoding {len(cam_ids)} camera stacks...")

        # Decode each camera stack (CPU-bound vectorized NumPy in thread pool)
        def _decode_one(cid: str) -> ViewObservations | None:
            if cid not in dark_frames:
                return None
            if any(cid not in cap for cap in cal_captures) or any(cid not in cap for cap in code_captures):
                return None
            dark = dark_frames[cid][0]
            cal_t = (cal_captures[0][cid][0], cal_captures[1][cid][0], cal_captures[2][cid][0])
            code_l = [cap[cid][0] for cap in code_captures]
            dets, color_mat = decode_camera_stack(dark, cal_t, code_l, codebook, target_ids)
            cam = fleet.cameras[cid]
            intr = cam.intrinsics or CameraIntrinsics(
                fx=dark.shape[1] * 0.85,
                fy=dark.shape[1] * 0.85,
                cx=dark.shape[1] / 2.0,
                cy=dark.shape[0] / 2.0,
            )
            return ViewObservations(
                view_id=f"{round_id}:{cid}",
                round_id=round_id,
                camera_id=cid,
                camera_name=cam.name,
                width=dark.shape[1],
                height=dark.shape[0],
                intrinsics=intr,
                detections=dets,
                color_matrix=color_mat,
                gravity_cam=cam.gravity_cam,
                timestamp=time.time(),
            )

        decoded = await asyncio.gather(*[asyncio.to_thread(_decode_one, cid) for cid in cam_ids])
        views = [v for v in decoded if v is not None]

    duration = time.monotonic() - t_start
    return ScanRound(
        round_id=round_id,
        label=label or round_id,
        mode=mode,  # type: ignore[arg-type]
        created_at=time.time(),
        duration_sec=round(duration, 2),
        enabled=True,
        views=views,
    )
