"""Tests for DC-Balanced Ternary ECC Codebook and Decode-First multi-PSF optical decoding."""
from __future__ import annotations

import cv2
import numpy as np

from ledmap.mapping.codebook import TernaryECCCodebook
from ledmap.mapping.scanner import decode_camera_stack


def test_ternary_ecc_minimum_hamming_distance_and_balance():
    cb = TernaryECCCodebook(300)
    assert cb.n_frames == 9  # 6 data + 3 parity for up to 729 LEDs

    # Check exact pairwise Hamming distance >= 3 across all pairs
    codes = cb.codes
    min_d = cb.n_frames
    for i in range(len(codes)):
        dists = np.sum(codes[i + 1 :] != codes[i], axis=1)
        if len(dists):
            min_d = min(min_d, int(np.min(dists)))
    assert min_d >= 3, f"Expected d_min >= 3, got {min_d}"

    # Check DC balance across every frame (~33% R, ~33% G, ~33% B)
    for f in range(cb.n_frames):
        counts = np.bincount(codes[:, f], minlength=3)
        ratios = counts / float(cb.n_leds)
        assert np.all(ratios > 0.25) and np.all(ratios < 0.42), f"Unbalanced frame {f}: {ratios}"


def test_decode_first_with_crosstalk_noise_single_error_and_dense_cluster():
    rng = np.random.default_rng(42)
    H, W = 240, 320
    n_leds = 40
    cb = TernaryECCCodebook(n_leds)

    # Place 36 well-separated LEDs + 2 pairs of very close touching LEDs (4 px apart!)
    true_pts: list[tuple[float, float]] = []
    for i in range(36):
        r, c = divmod(i, 6)
        true_pts.append((35.0 + c * 48.0 + rng.uniform(-3, 3), 25.0 + r * 35.0 + rng.uniform(-3, 3)))
    # Touching dense pairs
    true_pts.append((150.0, 120.0))
    true_pts.append((154.0, 120.5))
    true_pts.append((210.0, 160.0))
    true_pts.append((210.5, 164.0))

    # Realistic non-ideal camera color crosstalk matrix (R bleeds into G, G into B, etc.)
    M_true = np.array(
        [
            [0.82, 0.12, 0.04],
            [0.14, 0.78, 0.12],
            [0.04, 0.10, 0.84],
        ],
        dtype=np.float32,
    )

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    dark = rng.normal(8.0, 1.5, size=(H, W, 3)).clip(0, 255).astype(np.uint8)

    def render_frame(led_rgb_true: np.ndarray, corrupt_frame: bool = False) -> np.ndarray:
        canvas = np.zeros((H, W, 3), dtype=np.float32)
        for idx, (ux, uy) in enumerate(true_pts):
            col = M_true @ led_rgb_true[idx].astype(np.float32)
            # Gaussian PSF with sigma=1.6px
            g = np.exp(-((xx - ux) ** 2 + (yy - uy) ** 2) / (2.0 * 1.6**2))
            canvas += g[:, :, None] * col[None, None, :]
        if corrupt_frame:
            # Corrupt LED #5 in this single frame to test single-error ECC recovery!
            ux, uy = true_pts[5]
            g = np.exp(-((xx - ux) ** 2 + (yy - uy) ** 2) / (2.0 * 1.6**2))
            canvas += g[:, :, None] * np.array([0.0, 0.0, 220.0])[None, None, :]
        noisy = dark[:, :, ::-1].astype(np.float32) + canvas + rng.normal(0, 1.8, size=(H, W, 3))
        rgb_u8 = np.clip(noisy, 0, 255).astype(np.uint8)
        return rgb_u8[:, :, ::-1]  # BGR

    cal_r = render_frame(np.tile([180, 0, 0], (n_leds, 1)))
    cal_g = render_frame(np.tile([0, 180, 0], (n_leds, 1)))
    cal_b = render_frame(np.tile([0, 0, 180], (n_leds, 1)))

    code_frames = [
        render_frame(cb.frame_rgb_pattern(t, brightness=180), corrupt_frame=(t == 2))
        for t in range(cb.n_frames)
    ]

    dets, _ = decode_camera_stack(
        dark,
        (cal_r, cal_g, cal_b),
        code_frames,
        cb,
        list(range(n_leds)),
    )

    assert len(dets) >= 39, f"Expected >=39/40 LEDs recovered, got {len(dets)}"
    assert 5 in dets, "Single-frame corrupted LED #5 must be recovered by ECC!"

    errs = []
    for gid, (tx, ty) in enumerate(true_pts):
        if gid in dets:
            c = dets[gid][0]
            errs.append(float(np.hypot(c.u - tx, c.v - ty)))
    assert float(np.median(errs)) < 0.6, f"Median sub-pixel error too high: {np.median(errs)}"
