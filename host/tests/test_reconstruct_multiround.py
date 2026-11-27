"""Tests for multi-round 3D forest stitching, reflection rejection (Strip Viterbi DP), and Sparse BA."""
from __future__ import annotations

import cv2
import numpy as np

from ledmap.mapping.reconstruct import solve_multi_round_3d
from ledmap.models import CameraIntrinsics, Candidate2D, LEDDescriptor, ScanRound, ViewObservations


def _procrustes_rmse(est: np.ndarray, gt: np.ndarray) -> float:
    """Compute similarity-invariant normalized Procrustes RMSE between two (N, 3) point clouds."""
    e_c = est - est.mean(axis=0)
    g_c = gt - gt.mean(axis=0)
    e_n = e_c / np.linalg.norm(e_c)
    g_n = g_c / np.linalg.norm(g_c)
    U, _, Vt = np.linalg.svd(e_n.T @ g_n)
    R = Vt.T @ U.T
    if np.linalg.det(R) < 0:
        Vt[-1, :] *= -1
        R = Vt.T @ U.T
    aligned = e_n @ R.T
    return float(np.sqrt(np.mean(np.sum((aligned - g_n) ** 2, axis=1))))


def _make_camera_view(
    view_id: str,
    round_id: str,
    cam_id: str,
    cam_pos: np.ndarray,
    look_at: np.ndarray,
    true_xyz: np.ndarray,
    visible_ids: list[int],
    rng: np.random.Generator,
    noise_px: float = 0.4,
    reflection_ids: set[int] | None = None,
) -> ViewObservations:
    W, H = 1920, 1080
    fx = fy = 1400.0
    cx, cy = W / 2.0, H / 2.0
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros(5, dtype=np.float64)

    # Build camera look-at rotation (world -> cam, OpenCV +Z forward, -Y up)
    fwd = look_at - cam_pos
    fwd /= np.linalg.norm(fwd)
    world_up = np.array([0.0, 1.0, 0.0])
    right = np.cross(fwd, world_up)
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    R = np.vstack([right, down, fwd])
    t = -R @ cam_pos
    rvec, _ = cv2.Rodrigues(R)

    dets: dict[int, list[Candidate2D]] = {}
    for gid in visible_ids:
        X = true_xyz[gid]
        Xc = R @ X + t
        if Xc[2] <= 0.2:
            continue
        proj, _ = cv2.projectPoints(X.reshape(1, 3), rvec, t.reshape(3, 1), K, dist)
        u, v = proj[0, 0] + rng.normal(0.0, noise_px, size=2)
        if not (0 <= u < W and 0 <= v < H):
            continue
        cands = [Candidate2D(u=float(u), v=float(v), score=0.92, snr=25.0)]

        # Inject a glossy mirror/wall reflection across plane z = -2.2 with HIGHER brightness
        # to prove Strip Viterbi DP + multi-view geometry reject reflections cleanly!
        if reflection_ids and gid in reflection_ids:
            X_ref = np.array([X[0], X[1], -4.4 - X[2]])
            proj_r, _ = cv2.projectPoints(X_ref.reshape(1, 3), rvec, t.reshape(3, 1), K, dist)
            ur, vr = proj_r[0, 0] + rng.normal(0.0, noise_px, size=2)
            if 0 <= ur < W and 0 <= vr < H:
                cands.insert(0, Candidate2D(u=float(ur), v=float(vr), score=0.96, snr=28.0))
        dets[gid] = cands

    return ViewObservations(
        view_id=view_id,
        round_id=round_id,
        camera_id=cam_id,
        camera_name=cam_id,
        width=W,
        height=H,
        intrinsics=CameraIntrinsics(fx=fx, fy=fy, cx=cx, cy=cy, source="camera2_intrinsic"),
        detections=dets,
    )


def test_multi_round_forest_stitching_and_reflection_rejection():
    rng = np.random.default_rng(123)
    # 2 WLED strips draped in 3D space (total 80 LEDs spanning a 4m grove)
    n_leds = 80
    led_table = [
        LEDDescriptor(
            global_id=i,
            controller_id="wled-tree1" if i < 40 else "wled-tree2",
            controller_name="Tree 1" if i < 40 else "Tree 2",
            led_index=i if i < 40 else i - 40,
        )
        for i in range(n_leds)
    ]

    true_xyz = np.zeros((n_leds, 3), dtype=np.float64)
    for i in range(40):
        t = i / 39.0
        true_xyz[i] = [-1.0 + 0.5 * np.cos(t * 9.0), -0.8 + 1.8 * t, 0.5 * np.sin(t * 9.0)]
    for i in range(40, 80):
        t = (i - 40) / 39.0
        true_xyz[i] = [1.0 + 0.5 * np.cos(t * 8.0), -0.7 + 1.7 * t, 0.4 * np.sin(t * 8.0)]

    # Deliberately occlude LEDs 15 & 16 in ALL views to verify tagged strip interpolation
    occluded = {15, 16}
    # Inject glossy reflections on LEDs 22 and 23 across multiple cameras!
    reflections = {22, 23}

    # Round 1: 2 cameras viewing Tree 1 + overlap of Tree 2 (0..54)
    r1_vis = [i for i in range(55) if i not in occluded]
    r1_v1 = _make_camera_view("r1:cam1", "r1", "cam1", np.array([-2.2, 0.3, 3.8]), np.array([-0.5, 0.1, 0.0]), true_xyz, r1_vis, rng, reflection_ids=reflections)
    r1_v2 = _make_camera_view("r1:cam2", "r1", "cam2", np.array([0.8, 0.5, 4.0]), np.array([-0.3, 0.1, 0.0]), true_xyz, r1_vis, rng, reflection_ids=reflections)
    round1 = ScanRound(round_id="r1", created_at=1.0, views=[r1_v1, r1_v2])

    # Round 2: moved cameras viewing Tree 2 + overlap of Tree 1 (25..79)
    r2_vis = [i for i in range(25, 80) if i not in occluded]
    r2_v1 = _make_camera_view("r2:cam1", "r2", "cam1", np.array([2.5, 0.2, 3.6]), np.array([0.6, 0.1, 0.0]), true_xyz, r2_vis, rng, reflection_ids=reflections)
    r2_v2 = _make_camera_view("r2:cam2", "r2", "cam2", np.array([1.2, 1.4, 3.8]), np.array([0.5, 0.1, 0.0]), true_xyz, r2_vis, rng)
    round2 = ScanRound(round_id="r2", created_at=2.0, views=[r2_v1, r2_v2])

    result = solve_multi_round_3d([round1, round2], led_table, interpolate_occluded=True)

    assert len(result.components) == 1, f"Rounds should stitch into 1 unified component, got {len(result.components)}"
    assert len(result.cameras) == 4
    assert result.measured_count >= 75, f"Expected >=75 measured LEDs, got {result.measured_count}"
    assert result.interpolated_count == 2, f"Expected 2 occluded LEDs interpolated, got {result.interpolated_count}"
    assert result.median_reproj_px < 1.2, f"Reprojection error too high: {result.median_reproj_px}"

    # Verify Procrustes 3D accuracy including the reflection-attacked LEDs #22 and #23!
    measured_leds = [l for l in result.leds if l.source == "measured"]
    est_pts = np.array([[l.x, l.y, l.z] for l in measured_leds])
    gt_pts = np.array([true_xyz[l.global_id] for l in measured_leds])
    rmse = _procrustes_rmse(est_pts, gt_pts)
    assert rmse < 0.015, f"3D reconstruction RMSE too high (reflection leak?): {rmse}"

    # Check occluded LEDs are explicitly tagged interpolated with nonzero uncertainty
    by_id = {l.global_id: l for l in result.leds}
    assert by_id[15].source == "interpolated" and by_id[15].uncertainty > 0.0
    assert by_id[16].source == "interpolated" and by_id[16].uncertainty > 0.0


def test_disjoint_forest_zones_report_separate_components():
    rng = np.random.default_rng(7)
    n_leds = 40
    led_table = [
        LEDDescriptor(global_id=i, controller_id="c1", controller_name="Forest", led_index=i)
        for i in range(n_leds)
    ]
    true_xyz = np.zeros((n_leds, 3), dtype=np.float64)
    for i in range(20):
        true_xyz[i] = [-3.0 + 0.2 * np.cos(i), -0.5 + 0.08 * i, 0.2 * np.sin(i)]
    for i in range(20, 40):
        true_xyz[i] = [3.0 + 0.2 * np.cos(i), -0.5 + 0.08 * (i - 20), 0.2 * np.sin(i)]

    # Zone A sees only 0..19; Zone B sees only 20..39 (zero overlap!)
    ra = ScanRound(
        round_id="za",
        created_at=1.0,
        views=[
            _make_camera_view("za:c1", "za", "c1", np.array([-4.0, 0.2, 2.5]), np.array([-3.0, 0.2, 0.0]), true_xyz, list(range(20)), rng),
            _make_camera_view("za:c2", "za", "c2", np.array([-2.0, 0.3, 2.5]), np.array([-3.0, 0.2, 0.0]), true_xyz, list(range(20)), rng),
        ],
    )
    rb = ScanRound(
        round_id="zb",
        created_at=2.0,
        views=[
            _make_camera_view("zb:c1", "zb", "c1", np.array([2.0, 0.2, 2.5]), np.array([3.0, 0.2, 0.0]), true_xyz, list(range(20, 40)), rng),
            _make_camera_view("zb:c2", "zb", "c2", np.array([4.0, 0.3, 2.5]), np.array([3.0, 0.2, 0.0]), true_xyz, list(range(20, 40)), rng),
        ],
    )
    res = solve_multi_round_3d([ra, rb], led_table)
    assert len(res.components) == 2
    assert res.measured_count == 40
