"""Multi-Round 3D Reconstruction, Reflection Rejection (Strip Viterbi DP), and Sparse Bundle Adjustment."""
from __future__ import annotations

import math
import time
from collections import defaultdict

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from ledmap.models import (
    CameraIntrinsics,
    Candidate2D,
    ComponentSummary,
    LEDDescriptor,
    Map3DResult,
    MappedLED3D,
    ScanRound,
    SolvedCameraPose,
    ViewObservations,
)


def _K_from_intrinsics(intr: CameraIntrinsics) -> np.ndarray:
    return np.array(
        [[intr.fx, 0.0, intr.cx], [0.0, intr.fy, intr.cy], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _dist_from_intrinsics(intr: CameraIntrinsics) -> np.ndarray:
    # Only trust stored distortion vectors from true OpenCV ChArUco calibration;
    # Android Camera2 JPEGs are already ISP-undistorted (and Camera2 LENS_DISTORTION order differs from OpenCV!).
    if intr.source != "charuco_calibrated":
        return np.zeros(5, dtype=np.float64)
    d = list(intr.dist[:5])
    while len(d) < 5:
        d.append(0.0)
    return np.array(d, dtype=np.float64)


def _undistort_uv(uv: tuple[float, float], K: np.ndarray, dist: np.ndarray) -> np.ndarray:
    """Return normalized camera ray coords (x_n, y_n) with K=I."""
    pt = np.array([[[uv[0], uv[1]]]], dtype=np.float64)
    und = cv2.undistortPoints(pt, K, dist)
    return und[0, 0]


def _project_point(X: np.ndarray, rvec: np.ndarray, tvec: np.ndarray, K: np.ndarray, dist: np.ndarray) -> np.ndarray:
    pts, _ = cv2.projectPoints(X.reshape(1, 3), rvec.reshape(3, 1), tvec.reshape(3, 1), K, dist)
    return pts[0, 0]


def _triangulate_dlt(proj_mats: list[np.ndarray], norm_pts: list[np.ndarray]) -> np.ndarray | None:
    """Linear multi-view DLT triangulation using normalized coordinates (P = [R | t])."""
    if len(proj_mats) < 2:
        return None
    rows = []
    for P, (x, y) in zip(proj_mats, norm_pts):
        rows.append(x * P[2] - P[0])
        rows.append(y * P[2] - P[1])
    A = np.asarray(rows, dtype=np.float64)
    _, _, Vt = np.linalg.svd(A)
    Xh = Vt[-1]
    if abs(Xh[3]) < 1e-10:
        return None
    return Xh[:3] / Xh[3]


def _triangulation_angle_deg(X: np.ndarray, cam_centers: list[np.ndarray]) -> float:
    max_ang = 0.0
    rays = []
    for c in cam_centers:
        v = X - c
        n = float(np.linalg.norm(v))
        if n > 1e-8:
            rays.append(v / n)
    for i in range(len(rays)):
        for j in range(i + 1, len(rays)):
            cos_a = float(np.clip(np.dot(rays[i], rays[j]), -1.0, 1.0))
            ang = math.degrees(math.acos(cos_a))
            if ang > max_ang:
                max_ang = ang
    return max_ang


def _cam_center(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    return (-R.T @ t.reshape(3, 1)).ravel()


def _rotation_align_vectors(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Minimal 3x3 rotation matrix R such that R @ src == dst (both unit 3-vectors)."""
    a = src / max(float(np.linalg.norm(src)), 1e-8)
    b = dst / max(float(np.linalg.norm(dst)), 1e-8)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    s = float(np.linalg.norm(v))
    if s < 1e-7:
        return np.eye(3, dtype=np.float64) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]], dtype=np.float64)
    return np.eye(3, dtype=np.float64) + vx + (vx @ vx) * ((1.0 - c) / (s * s))


def _euler_xyz_matrix(deg: list[float]) -> np.ndarray:
    rx, ry, rz = [math.radians(a) for a in deg]
    Rx = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]], dtype=np.float64)
    Ry = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]], dtype=np.float64)
    Rz = np.array([[math.cos(rz), -math.sin(rz), 0], [math.sin(rz), math.cos(rz), 0], [0, 0, 1]], dtype=np.float64)
    return Rz @ Ry @ Rx


def _find_connected_components(
    views: list[ViewObservations], min_shared: int = 8
) -> list[list[ViewObservations]]:
    """Partition camera views across all rounds into connected co-visibility components."""
    n = len(views)
    if n == 0:
        return []
    led_sets = [set(v.detections.keys()) for v in views]
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if len(led_sets[i] & led_sets[j]) >= min_shared:
                adj[i].add(j)
                adj[j].add(i)

    visited: set[int] = set()
    comps: list[list[ViewObservations]] = []
    for i in range(n):
        if i in visited:
            continue
        stack = [i]
        comp_idx: list[int] = []
        while stack:
            u = stack.pop()
            if u in visited:
                continue
            visited.add(u)
            comp_idx.append(u)
            stack.extend(adj[u] - visited)
        comps.append([views[k] for k in sorted(comp_idx)])
    comps.sort(key=lambda c: len(set().union(*(set(v.detections.keys()) for v in c))), reverse=True)
    return comps


def _enumerate_led_hypotheses(
    gid: int,
    registered_views: list[int],
    views: list[ViewObservations],
    poses: dict[int, tuple[np.ndarray, np.ndarray]],
    Ks: dict[int, np.ndarray],
    dists: dict[int, np.ndarray],
    max_reproj_px: float = 12.0,
    min_parallax_deg: float = 5.0,
    min_baseline: float = 0.25,
) -> list[tuple[np.ndarray, dict[int, int], float, float]]:
    """Generate multi-view 3D hypotheses for one LED from all candidate 2D blobs (including reflections).

    Enforces a real physical camera baseline (`min_baseline`) and parallax angle so stationary
    repeated cameras across multiple rounds can NEVER triangulate fake floating clumps with themselves!
    """
    obs_views = [vi for vi in registered_views if gid in views[vi].detections and views[vi].detections[gid]]
    if len(obs_views) < 2:
        return []

    P_mats = {}
    centers = {}
    for vi in obs_views:
        rvec, tvec = poses[vi]
        R, _ = cv2.Rodrigues(rvec)
        P_mats[vi] = np.hstack([R, tvec.reshape(3, 1)])
        centers[vi] = _cam_center(R, tvec)

    # Seed hypotheses from well-separated pairs of views' candidate blobs (at most top 3 per view)
    hypotheses: list[tuple[np.ndarray, dict[int, int], float, float]] = []
    seen_sets: set[frozenset[tuple[int, int]]] = set()

    # Select up to 12 seed views prioritizing DISTINCT 3D camera positions (>0.20 apart)
    # so stationary desk cameras across 10+ rounds NEVER crowd out moved phone viewpoints!
    seed_views: list[int] = []
    for vi in obs_views:
        if all(float(np.linalg.norm(centers[vi] - centers[sv])) >= 0.20 for sv in seed_views):
            seed_views.append(vi)
        if len(seed_views) >= 12:
            break
    if len(seed_views) < 2:
        seed_views = obs_views[:12]
    for a_idx in range(len(seed_views)):
        va = seed_views[a_idx]
        cands_a = views[va].detections[gid][:3]
        for b_idx in range(a_idx + 1, len(seed_views)):
            vb = seed_views[b_idx]
            # Reject zero/tiny-baseline pairs (e.g. same stationary camera across Round 1 & Round 2!)
            pair_base = float(np.linalg.norm(centers[va] - centers[vb]))
            if pair_base < min_baseline:
                continue
            cands_b = views[vb].detections[gid][:3]
            for ca_i, ca in enumerate(cands_a):
                na = _undistort_uv((ca.u, ca.v), Ks[va], dists[va])
                for cb_i, cb in enumerate(cands_b):
                    nb = _undistort_uv((cb.u, cb.v), Ks[vb], dists[vb])
                    X0 = _triangulate_dlt([P_mats[va], P_mats[vb]], [na, nb])
                    if X0 is None or not np.all(np.isfinite(X0)):
                        continue
                    z_a = float((P_mats[va] @ np.append(X0, 1.0))[2])
                    z_b = float((P_mats[vb] @ np.append(X0, 1.0))[2])
                    if z_a <= 0.05 or z_b <= 0.05 or z_a > 200.0 or z_b > 200.0:
                        continue
                    if _triangulation_angle_deg(X0, [centers[va], centers[vb]]) < min_parallax_deg:
                        continue
                    pa = _project_point(X0, poses[va][0], poses[va][1], Ks[va], dists[va])
                    pb = _project_point(X0, poses[vb][0], poses[vb][1], Ks[vb], dists[vb])
                    ea = float(np.hypot(pa[0] - ca.u, pa[1] - ca.v))
                    eb = float(np.hypot(pb[0] - cb.u, pb[1] - cb.v))
                    if ea > max_reproj_px or eb > max_reproj_px:
                        continue

                    # Collect consensus across ALL observing views
                    chosen: dict[int, int] = {va: ca_i, vb: cb_i}
                    for vc in obs_views:
                        if vc in chosen:
                            continue
                        z_c = float((P_mats[vc] @ np.append(X0, 1.0))[2])
                        if z_c <= 0.05 or z_c > 200.0:
                            continue
                        pc = _project_point(X0, poses[vc][0], poses[vc][1], Ks[vc], dists[vc])
                        best_ci = -1
                        best_err = max_reproj_px
                        for ci, cand in enumerate(views[vc].detections[gid][:3]):
                            err = float(np.hypot(pc[0] - cand.u, pc[1] - cand.v))
                            if err < best_err:
                                best_err = err
                                best_ci = ci
                        if best_ci >= 0:
                            chosen[vc] = best_ci

                    key = frozenset(chosen.items())
                    if key in seen_sets:
                        continue
                    seen_sets.add(key)

                    # Re-triangulate with all supporting views
                    ps = [P_mats[v] for v in chosen]
                    ns = [
                        _undistort_uv(
                            (views[v].detections[gid][ci].u, views[v].detections[gid][ci].v),
                            Ks[v],
                            dists[v],
                        )
                        for v, ci in chosen.items()
                    ]
                    X = _triangulate_dlt(ps, ns)
                    if X is None or not np.all(np.isfinite(X)):
                        continue
                    ang = _triangulation_angle_deg(X, [centers[v] for v in chosen])
                    if ang < min_parallax_deg:
                        continue

                    errs = []
                    cand_scores = []
                    distinct_cams = len({views[v].camera_id for v in chosen})
                    for v, ci in chosen.items():
                        cand = views[v].detections[gid][ci]
                        proj = _project_point(X, poses[v][0], poses[v][1], Ks[v], dists[v])
                        errs.append(float(np.hypot(proj[0] - cand.u, proj[1] - cand.v)))
                        cand_scores.append(cand.score)
                    mean_err = float(np.mean(errs))
                    if mean_err > max_reproj_px:
                        continue
                    # Favor more distinct physical viewpoints, stronger parallax, higher blob score, lower reproj error
                    quality = 4.0 * distinct_cams + 1.5 * len(chosen) + 0.1 * min(ang, 45.0) + 2.0 * float(np.mean(cand_scores)) - 0.5 * mean_err
                    hypotheses.append((X, chosen, mean_err, quality))

    hypotheses.sort(key=lambda h: h[3], reverse=True)
    return hypotheses[:5]


def _viterbi_select_strip_hypotheses(
    led_table: list[LEDDescriptor],
    hypotheses_by_gid: dict[int, list[tuple[np.ndarray, dict[int, int], float, float]]],
) -> dict[int, tuple[np.ndarray, dict[int, int], float]]:
    """Run Strip Viterbi Dynamic Programming per WLED controller to reject reflections/ghosts.

    Adjacent LEDs on a physical strip have a robust upper-bound step distance. A reflection
    causes a sudden 3D jump away from the strip chain, so DP cleanly prefers the physically
    consistent direct-light hypothesis while allowing tight bends and arbitrary 3D draping.
    """
    selected: dict[int, tuple[np.ndarray, dict[int, int], float]] = {}

    # Group LED descriptors by controller sorted by led_index
    by_ctrl: dict[str, list[LEDDescriptor]] = defaultdict(list)
    for d in led_table:
        by_ctrl[d.controller_id].append(d)

    for cid, descs in by_ctrl.items():
        descs.sort(key=lambda d: d.led_index)
        obs_descs = [d for d in descs if d.global_id in hypotheses_by_gid and hypotheses_by_gid[d.global_id]]
        if not obs_descs:
            continue

        # Estimate typical single-step distance from high-confidence unambiguous adjacent pairs
        step_dists = []
        for a, b in zip(obs_descs, obs_descs[1:]):
            gap = b.led_index - a.led_index
            if gap <= 0:
                continue
            ha = hypotheses_by_gid[a.global_id]
            hb = hypotheses_by_gid[b.global_id]
            if len(ha) == 1 and len(hb) == 1:
                step_dists.append(float(np.linalg.norm(hb[0][0] - ha[0][0])) / gap)
        if not step_dists:
            # Fallback using top-1 hypotheses
            for a, b in zip(obs_descs, obs_descs[1:]):
                gap = max(1, b.led_index - a.led_index)
                step_dists.append(
                    float(np.linalg.norm(hypotheses_by_gid[b.global_id][0][0] - hypotheses_by_gid[a.global_id][0][0])) / gap
                )
        typical_step = float(np.percentile(step_dists, 75)) if step_dists else 0.1
        max_step = max(typical_step * 2.8, 1e-3)

        # Dynamic Programming over obs_descs
        M = len(obs_descs)
        dp_cost: list[list[float]] = []
        dp_prev: list[list[int]] = []

        for idx, d in enumerate(obs_descs):
            hyps = hypotheses_by_gid[d.global_id]
            unary = [-h[3] for h in hyps]  # minimize negative quality
            if idx == 0:
                dp_cost.append(unary)
                dp_prev.append([-1] * len(hyps))
                continue

            prev_d = obs_descs[idx - 1]
            prev_hyps = hypotheses_by_gid[prev_d.global_id]
            gap = max(1, d.led_index - prev_d.led_index)
            limit = max_step * min(gap, 8)  # allow clean breaks over large occluded gaps

            cur_costs = []
            cur_prevs = []
            for hi, h in enumerate(hyps):
                best_c = float("inf")
                best_p = 0
                for pi, ph in enumerate(prev_hyps):
                    dist = float(np.linalg.norm(h[0] - ph[0]))
                    # Robust upper-bound penalty: zero penalty if within physical wire length,
                    # steep but capped penalty if jumping impossibly far (like to a mirror reflection)
                    excess = max(0.0, dist - limit) / max(typical_step, 1e-3)
                    trans_cost = min(excess * 3.0, 25.0)
                    total = dp_cost[idx - 1][pi] + trans_cost + unary[hi]
                    if total < best_c:
                        best_c = total
                        best_p = pi
                cur_costs.append(best_c)
                cur_prevs.append(best_p)
            dp_cost.append(cur_costs)
            dp_prev.append(cur_prevs)

        # Backtrack optimal chain
        best_last = int(np.argmin(dp_cost[-1]))
        chosen_indices = [0] * M
        cur = best_last
        for idx in range(M - 1, -1, -1):
            chosen_indices[idx] = cur
            cur = dp_prev[idx][cur]

        for d, hi in zip(obs_descs, chosen_indices):
            h = hypotheses_by_gid[d.global_id][hi]
            selected[d.global_id] = (h[0], h[1], h[2])

    return selected


def _evaluate_seed_pose(
    R1: np.ndarray,
    t1: np.ndarray,
    pts0_n: np.ndarray,
    pts1_n: np.ndarray,
    uv0: np.ndarray,
    uv1: np.ndarray,
    K0: np.ndarray,
    dist0: np.ndarray,
    K1: np.ndarray,
    dist1: np.ndarray,
    max_reproj_px: float = 12.0,
) -> tuple[int, float, float, dict[int, np.ndarray]]:
    """Score a candidate relative pose (R1, t1) by inlier count, median reproj error, and parallax."""
    r0 = np.zeros(3, dtype=np.float64)
    t0 = np.zeros(3, dtype=np.float64)
    r1, _ = cv2.Rodrigues(R1)
    r1 = r1.ravel()
    t1 = t1.ravel()
    tnorm = float(np.linalg.norm(t1))
    if tnorm < 1e-6:
        return 0, 999.0, 0.0, {}
    t1 = t1 / tnorm

    P0 = np.hstack([np.eye(3), np.zeros((3, 1))])
    P1 = np.hstack([R1, t1.reshape(3, 1)])
    c0 = np.zeros(3, dtype=np.float64)
    c1 = _cam_center(R1, t1)

    pts_out: dict[int, np.ndarray] = {}
    errs: list[float] = []
    angs: list[float] = []
    for idx in range(len(pts0_n)):
        X = _triangulate_dlt([P0, P1], [pts0_n[idx], pts1_n[idx]])
        if X is None or not np.all(np.isfinite(X)):
            continue
        z0 = float(X[2])
        z1 = float((P1 @ np.append(X, 1.0))[2])
        if z0 <= 0.1 or z1 <= 0.1 or z0 > 200.0 or z1 > 200.0:
            continue
        ang = _triangulation_angle_deg(X, [c0, c1])
        if ang < 1.5:
            continue
        p0 = _project_point(X, r0, t0, K0, dist0)
        p1 = _project_point(X, r1, t1, K1, dist1)
        e0 = float(np.hypot(p0[0] - uv0[idx, 0], p0[1] - uv0[idx, 1]))
        e1 = float(np.hypot(p1[0] - uv1[idx, 0], p1[1] - uv1[idx, 1]))
        if e0 < max_reproj_px and e1 < max_reproj_px:
            pts_out[idx] = X
            errs.append(0.5 * (e0 + e1))
            angs.append(ang)

    if not errs:
        return 0, 999.0, 0.0, {}
    return len(pts_out), float(np.median(errs)), float(np.median(angs)), pts_out


def _solve_best_seed_pose(
    pts0_n: np.ndarray,
    pts1_n: np.ndarray,
    uv0: np.ndarray,
    uv1: np.ndarray,
    K0: np.ndarray,
    dist0: np.ndarray,
    K1: np.ndarray,
    dist1: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, dict[int, np.ndarray]] | None:
    """Dual-model COLMAP/ORB-SLAM initialization: evaluates BOTH Essential Matrix AND Homography
    decomposition so mostly-planar walls (like indoor wall trees) never collapse into a degenerate cone!
    """
    mean_f = 0.5 * (K0[0, 0] + K1[0, 0])
    candidates: list[tuple[np.ndarray, np.ndarray]] = []

    # Model A: Essential Matrix (strong for full 3D scenes)
    E, emask = cv2.findEssentialMat(
        pts0_n, pts1_n, np.eye(3), method=cv2.RANSAC, prob=0.9995, threshold=4.5 / max(mean_f, 200.0)
    )
    if E is not None:
        for k in range(0, E.shape[0], 3):
            Ek = E[k : k + 3, :3]
            if Ek.shape != (3, 3):
                continue
            try:
                R_a, R_b, t_a = cv2.decomposeEssentialMat(Ek)
                for R_cand in (R_a, R_b):
                    for t_cand in (t_a, -t_a):
                        candidates.append((R_cand, t_cand.ravel()))
            except cv2.error:
                pass

    # Model B: Homography Decomposition (exact on planar / near-planar scenes like wall LED trees!)
    if len(pts0_n) >= 4:
        H, _ = cv2.findHomography(pts0_n, pts1_n, method=cv2.RANSAC, ransacReprojThreshold=6.0 / max(mean_f, 200.0))
        if H is not None and H.shape == (3, 3):
            try:
                n_sols, Rs, ts, _ = cv2.decomposeHomographyMat(H, np.eye(3))
                for s in range(n_sols):
                    candidates.append((Rs[s], ts[s].ravel()))
            except cv2.error:
                pass

    best_score = -1.0
    best_out: tuple[np.ndarray, np.ndarray, dict[int, np.ndarray]] | None = None
    for R_c, t_c in candidates:
        n_in, med_err, med_ang, pts_map = _evaluate_seed_pose(
            R_c, t_c, pts0_n, pts1_n, uv0, uv1, K0, dist0, K1, dist1, max_reproj_px=14.0
        )
        if n_in < 6:
            continue
        # Strongly favor high inlier coverage (both wall AND off-plane stairs!), low reproj error,
        # and healthy triangulation parallax (rejecting near-0-baseline false solutions!)
        ang_bonus = min(med_ang, 30.0) / 15.0
        score = n_in * (1.0 + 0.5 * ang_bonus) - 2.0 * med_err
        if score > best_score:
            best_score = score
            tn = max(float(np.linalg.norm(t_c)), 1e-6)
            best_out = (R_c, t_c / tn, pts_map)

    return best_out


def _run_sparse_bundle_adjustment(
    registered_views: list[int],
    views: list[ViewObservations],
    poses: dict[int, tuple[np.ndarray, np.ndarray]],
    Ks: dict[int, np.ndarray],
    dists: dict[int, np.ndarray],
    points_3d: dict[int, np.ndarray],
    obs_by_gid: dict[int, dict[int, int]],
    max_nfev: int = 50,
) -> tuple[dict[int, tuple[np.ndarray, np.ndarray]], dict[int, np.ndarray], dict[int, np.ndarray], dict[int, np.ndarray]]:
    """Global Robust Sparse Bundle Adjustment using SciPy TRF with explicit Jacobian sparsity.

    Also jointly refines focal length + radial distortion (fx, k1) for estimated_fov cameras!
    """
    if len(registered_views) < 2 or len(points_3d) < 4:
        return poses, points_3d, Ks, dists

    cam_order = list(registered_views)
    cam_to_idx = {vi: i for i, vi in enumerate(cam_order)}
    pt_order = sorted(points_3d.keys())
    pt_to_idx = {gid: i for i, gid in enumerate(pt_order)}

    obs_cam: list[int] = []
    obs_pt: list[int] = []
    obs_uv: list[tuple[float, float]] = []
    for gid in pt_order:
        for vi, ci in obs_by_gid[gid].items():
            if vi not in cam_to_idx:
                continue
            cand = views[vi].detections[gid][ci]
            obs_cam.append(cam_to_idx[vi])
            obs_pt.append(pt_to_idx[gid])
            obs_uv.append((cand.u, cand.v))

    n_obs = len(obs_cam)
    n_cams = len(cam_order)
    n_pts = len(pt_order)
    if n_obs < 8:
        return poses, points_3d, Ks, dists

    # Identify uncalibrated (estimated_fov) cameras whose focal length + radial distortion (fx, k1)
    # should be jointly refined in BA — fixes wide-angle edge bending (e.g. EMEET PIXY)!
    has_calibrated = any(views[vi].intrinsics.source != "estimated_fov" for vi in cam_order)
    opt_f_cams = [
        i for i, vi in enumerate(cam_order) if has_calibrated and views[vi].intrinsics.source == "estimated_fov"
    ]
    cam_to_fvar = {ci: fi for fi, ci in enumerate(opt_f_cams)}

    cam0_r, cam0_t = poses[cam_order[0]]
    cam1_t_norm = max(float(np.linalg.norm(poses[cam_order[1]][1])), 1e-4)

    n_cam_vars = (n_cams - 1) * 6
    n_f_vars = len(opt_f_cams) * 2  # (fx, k1) per uncalibrated camera
    pt_offset = n_cam_vars + n_f_vars
    x0 = np.empty(pt_offset + n_pts * 3, dtype=np.float64)
    for i in range(1, n_cams):
        r, t = poses[cam_order[i]]
        base = (i - 1) * 6
        x0[base : base + 3] = r.ravel()
        x0[base + 3 : base + 6] = t.ravel()
    for fi, ci in enumerate(opt_f_cams):
        x0[n_cam_vars + fi * 2] = float(Ks[cam_order[ci]][0, 0])
        x0[n_cam_vars + fi * 2 + 1] = float(dists[cam_order[ci]][0])
    for j, gid in enumerate(pt_order):
        base = pt_offset + j * 3
        x0[base : base + 3] = points_3d[gid].ravel()

    n_res = n_obs * 2 + 1
    sparsity = lil_matrix((n_res, len(x0)), dtype=int)
    for k in range(n_obs):
        ci = obs_cam[k]
        pi = obs_pt[k]
        if ci > 0:
            c_base = (ci - 1) * 6
            sparsity[2 * k : 2 * k + 2, c_base : c_base + 6] = 1
        if ci in cam_to_fvar:
            fb = n_cam_vars + cam_to_fvar[ci] * 2
            sparsity[2 * k : 2 * k + 2, fb : fb + 2] = 1
        p_base = pt_offset + pi * 3
        sparsity[2 * k : 2 * k + 2, p_base : p_base + 3] = 1
    sparsity[2 * n_obs, 3:6] = 1

    obs_cam_arr = np.asarray(obs_cam, dtype=np.int32)
    obs_pt_arr = np.asarray(obs_pt, dtype=np.int32)
    obs_uv_arr = np.asarray(obs_uv, dtype=np.float64)
    K_base_list = [Ks[vi].copy() for vi in cam_order]
    dist_base_list = [dists[vi].copy() for vi in cam_order]
    by_cam_indices = [np.nonzero(obs_cam_arr == i)[0] for i in range(n_cams)]

    def residuals(params: np.ndarray) -> np.ndarray:
        res = np.empty(n_res, dtype=np.float64)
        pts3 = params[pt_offset:].reshape(n_pts, 3)

        for i in range(n_cams):
            idxs = by_cam_indices[i]
            if len(idxs) == 0:
                continue
            if i == 0:
                r, t = cam0_r, cam0_t
            else:
                b = (i - 1) * 6
                r = params[b : b + 3]
                t = params[b + 3 : b + 6]
            Ki = K_base_list[i]
            di = dist_base_list[i]
            if i in cam_to_fvar:
                fb = n_cam_vars + cam_to_fvar[i] * 2
                fval = max(200.0, float(params[fb]))
                k1val = float(np.clip(params[fb + 1], -0.8, 0.8))
                Ki = Ki.copy()
                Ki[0, 0] = fval
                Ki[1, 1] = fval
                di = di.copy()
                di[0] = k1val
            proj, _ = cv2.projectPoints(
                pts3[obs_pt_arr[idxs]], r.reshape(3, 1), t.reshape(3, 1), Ki, di
            )
            diff = proj.reshape(-1, 2) - obs_uv_arr[idxs]
            res[2 * idxs] = diff[:, 0]
            res[2 * idxs + 1] = diff[:, 1]

        t1 = params[3:6]
        res[2 * n_obs] = 10.0 * (float(np.linalg.norm(t1)) - cam1_t_norm)
        return res

    result = least_squares(
        residuals,
        x0,
        jac_sparsity=sparsity,
        method="trf",
        loss="cauchy",
        f_scale=2.0,
        x_scale="jac",
        max_nfev=max_nfev,
    )

    opt = result.x
    new_poses = dict(poses)
    for i in range(1, n_cams):
        b = (i - 1) * 6
        new_poses[cam_order[i]] = (opt[b : b + 3].copy(), opt[b + 3 : b + 6].copy())
    new_Ks = {vi: K.copy() for vi, K in Ks.items()}
    new_dists = {vi: d.copy() for vi, d in dists.items()}
    for fi, ci in enumerate(opt_f_cams):
        fb = n_cam_vars + fi * 2
        fval = max(200.0, float(opt[fb]))
        k1val = float(np.clip(opt[fb + 1], -0.8, 0.8))
        new_Ks[cam_order[ci]][0, 0] = fval
        new_Ks[cam_order[ci]][1, 1] = fval
        new_dists[cam_order[ci]][0] = k1val
    new_pts = {}
    pts3 = opt[pt_offset:].reshape(n_pts, 3)
    for j, gid in enumerate(pt_order):
        new_pts[gid] = pts3[j].copy()
    return new_poses, new_pts, new_Ks, new_dists


def solve_multi_round_3d(
    rounds: list[ScanRound],
    led_table: list[LEDDescriptor],
    interpolate_occluded: bool = True,
    max_interp_gap: int = 8,
    scale_meters_per_unit: float = 1.0,
    rotation_euler_deg: list[float] | None = None,
) -> Map3DResult:
    """Solve unified 3D LED coordinates + camera poses across all enabled scan rounds."""
    t0 = time.monotonic()
    rot_deg = rotation_euler_deg or [0.0, 0.0, 0.0]
    views: list[ViewObservations] = []
    for rnd in rounds:
        if rnd.enabled:
            views.extend(rnd.views)

    desc_by_gid = {d.global_id: d for d in led_table}
    if len(views) < 2:
        # Report single-view coverage cleanly without crashing
        seen_once: set[int] = set()
        if len(views) == 1:
            seen_once = set(views[0].detections.keys())
        leds = [
            MappedLED3D(
                global_id=d.global_id,
                controller_id=d.controller_id,
                controller_name=d.controller_name,
                led_index=d.led_index,
                x=0.0,
                y=0.0,
                z=0.0,
                source="unmapped",
                views_count=1 if d.global_id in seen_once else 0,
                confidence=0.0,
            )
            for d in led_table
        ]
        return Map3DResult(
            leds=leds,
            total_leds=len(led_table),
            single_view_count=len(seen_once),
            unmapped_count=len(led_table) - len(seen_once),
            solve_time_sec=round(time.monotonic() - t0, 3),
        )

    # Partition into connected co-visibility components (auto-handles disjoint forest zones!)
    components = _find_connected_components(views, min_shared=8)
    if all(len(c) == 1 for c in components):
        components = _find_connected_components(views, min_shared=5)

    all_solved_points: dict[int, tuple[np.ndarray, int, float, int, float]] = {}
    # gid -> (xyz, views_count, reproj_err, component_id, confidence)
    all_solved_cameras: list[SolvedCameraPose] = []
    comp_summaries: list[ComponentSummary] = []

    for comp_id, comp_views in enumerate(components):
        if len(comp_views) < 2:
            comp_summaries.append(
                ComponentSummary(
                    component_id=comp_id,
                    view_ids=[v.view_id for v in comp_views],
                    led_count=0,
                )
            )
            continue

        Ks = {i: _K_from_intrinsics(v.intrinsics) for i, v in enumerate(comp_views)}
        dists = {i: _dist_from_intrinsics(v.intrinsics) for i, v in enumerate(comp_views)}

        # 1. Rank candidate seed camera pairs:
        #    - Strongly prefer distinct physical cameras (avoid zero-baseline stationary repeats across rounds!)
        #    - Prefer factory/ChArUco-calibrated pairs over uncalibrated webcams
        #    - Try ranked pairs in order until a geometrically well-conditioned seed succeeds!
        ranked_pairs: list[tuple[float, int, int, list[int]]] = []
        for i in range(len(comp_views)):
            si = set(comp_views[i].detections.keys())
            cal_i = 1.0 if comp_views[i].intrinsics.source != "estimated_fov" else 0.0
            for j in range(i + 1, len(comp_views)):
                shared = sorted(si & set(comp_views[j].detections.keys()))
                if len(shared) < 6:
                    continue
                cal_j = 1.0 if comp_views[j].intrinsics.source != "estimated_fov" else 0.0
                diff_cam = 1.0 if comp_views[i].camera_id != comp_views[j].camera_id else 0.15
                pair_score = len(shared) * diff_cam * (1.0 + 1.5 * (cal_i + cal_j))
                ranked_pairs.append((pair_score, i, j, shared))

        ranked_pairs.sort(key=lambda x: x[0], reverse=True)
        seed = None
        i0 = i1 = -1
        best_shared: list[int] = []
        for _, ci0, ci1, shared in ranked_pairs[:12]:
            uv0 = np.array([[comp_views[ci0].detections[g][0].u, comp_views[ci0].detections[g][0].v] for g in shared], dtype=np.float64)
            uv1 = np.array([[comp_views[ci1].detections[g][0].u, comp_views[ci1].detections[g][0].v] for g in shared], dtype=np.float64)
            pts0_n = np.array([_undistort_uv((u, v), Ks[ci0], dists[ci0]) for u, v in uv0], dtype=np.float64)
            pts1_n = np.array([_undistort_uv((u, v), Ks[ci1], dists[ci1]) for u, v in uv1], dtype=np.float64)
            cand_seed = _solve_best_seed_pose(pts0_n, pts1_n, uv0, uv1, Ks[ci0], dists[ci0], Ks[ci1], dists[ci1])
            if cand_seed is not None and len(cand_seed[2]) >= 8:
                seed = cand_seed
                i0, i1, best_shared = ci0, ci1, shared
                break

        if seed is None:
            continue
        R1, t1, seed_pts = seed

        r0 = np.zeros(3, dtype=np.float64)
        t0_vec = np.zeros(3, dtype=np.float64)
        r1, _ = cv2.Rodrigues(R1)
        poses: dict[int, tuple[np.ndarray, np.ndarray]] = {
            i0: (r0, t0_vec),
            i1: (r1.ravel(), t1.ravel()),
        }
        registered = [i0, i1]

        pts_3d: dict[int, np.ndarray] = {}
        obs_by_gid: dict[int, dict[int, int]] = {}
        for idx, X in seed_pts.items():
            gid = best_shared[idx]
            pts_3d[gid] = X
            obs_by_gid[gid] = {i0: 0, i1: 0}

        # Run initial 2-view BA right away so subsequent PnP cameras lock onto clean 3D structure!
        poses, pts_3d, Ks, dists = _run_sparse_bundle_adjustment(
            registered, comp_views, poses, Ks, dists, pts_3d, obs_by_gid, max_nfev=30
        )

        # 2. Incremental Spanning-Tree Registration for all remaining views in this component
        unregistered = [i for i in range(len(comp_views)) if i not in poses]
        while unregistered:
            best_v = None
            best_matches: list[int] = []
            for v in unregistered:
                matches = [g for g in comp_views[v].detections.keys() if g in pts_3d]
                if len(matches) > len(best_matches):
                    best_matches = matches
                    best_v = v
            if best_v is None or len(best_matches) < 4:
                break

            obj_p = np.array([pts_3d[g] for g in best_matches], dtype=np.float64)
            img_p = np.array(
                [[comp_views[best_v].detections[g][0].u, comp_views[best_v].detections[g][0].v] for g in best_matches],
                dtype=np.float64,
            )
            # Use looser PnP threshold for uncalibrated (estimated_fov) webcams before BA refines their focal length!
            pnp_thresh = 25.0 if comp_views[best_v].intrinsics.source == "estimated_fov" else 10.0
            ok, rv, tv, inliers = cv2.solvePnPRansac(
                obj_p,
                img_p,
                Ks[best_v],
                dists[best_v],
                iterationsCount=400,
                reprojectionError=pnp_thresh,
                flags=cv2.SOLVEPNP_EPNP,
            )
            unregistered.remove(best_v)
            if not ok or inliers is None or len(inliers) < 4:
                continue
            inlier_idx = inliers.ravel()
            ok2, rv2, tv2 = cv2.solvePnP(
                obj_p[inlier_idx],
                img_p[inlier_idx],
                Ks[best_v],
                dists[best_v],
                rv,
                tv,
                useExtrinsicGuess=True,
                flags=cv2.SOLVEPNP_ITERATIVE,
            )
            poses[best_v] = (rv2.ravel() if ok2 else rv.ravel(), tv2.ravel() if ok2 else tv.ravel())
            registered.append(best_v)

            # Only run intermediate BA when adding an uncalibrated webcam (to solve its fx, k1)
            # — skip redundant BA on already-calibrated Android phones so 10+ round house/forest solves stay fast!
            for g in best_matches:
                obs_by_gid.setdefault(g, {})[best_v] = 0
            if comp_views[best_v].intrinsics.source == "estimated_fov":
                poses, pts_3d, Ks, dists = _run_sparse_bundle_adjustment(
                    registered, comp_views, poses, Ks, dists, pts_3d, obs_by_gid, max_nfev=30
                )

            # Triangulate newly co-visible LEDs with the expanded camera set
            all_seen = set().union(*(set(comp_views[v].detections.keys()) for v in registered))
            for gid in all_seen:
                if gid in pts_3d:
                    continue
                hyps = _enumerate_led_hypotheses(
                    gid, registered, comp_views, poses, Ks, dists, max_reproj_px=20.0, min_parallax_deg=5.0, min_baseline=0.25
                )
                if hyps:
                    pts_3d[gid] = hyps[0][0]
                    obs_by_gid[gid] = hyps[0][1]

        # 3. Multi-Hypothesis Enumeration + Strip Viterbi DP Reflection Rejection!
        #    Use a generous pre-distortion-BA threshold (24px) so wide-angle edge LEDs (like upper stairs)
        #    join properly across well-separated cameras, then tighten after BA refines (fx, k1)!
        all_seen = sorted(set().union(*(set(comp_views[v].detections.keys()) for v in registered)))
        hyps_by_gid: dict[int, list[tuple[np.ndarray, dict[int, int], float, float]]] = {}
        for gid in all_seen:
            hyps = _enumerate_led_hypotheses(
                gid, registered, comp_views, poses, Ks, dists, max_reproj_px=24.0, min_parallax_deg=5.0, min_baseline=0.25
            )
            if hyps:
                hyps_by_gid[gid] = hyps

        viterbi_sel = _viterbi_select_strip_hypotheses(led_table, hyps_by_gid)
        if viterbi_sel:
            pts_3d = {g: val[0] for g, val in viterbi_sel.items()}
            obs_by_gid = {g: val[1] for g, val in viterbi_sel.items()}

        # 4. Global Robust Sparse Bundle Adjustment (refines all poses + (fx, k1) + 3D points)
        poses, pts_3d, Ks, dists = _run_sparse_bundle_adjustment(
            registered, comp_views, poses, Ks, dists, pts_3d, obs_by_gid, max_nfev=60
        )

        # 4a. Post-calibration re-triangulation pass: now that lens distortion k1 & fx are solved,
        #     re-enumerate hypotheses with tight 12px gate so every remaining valid edge LED snaps cleanly!
        hyps_post: dict[int, list[tuple[np.ndarray, dict[int, int], float, float]]] = {}
        for gid in all_seen:
            hyps = _enumerate_led_hypotheses(
                gid, registered, comp_views, poses, Ks, dists, max_reproj_px=12.0, min_parallax_deg=5.0, min_baseline=0.25
            )
            if hyps:
                hyps_post[gid] = hyps
        viterbi_post = _viterbi_select_strip_hypotheses(led_table, hyps_post)
        if len(viterbi_post) >= max(4, int(0.8 * len(pts_3d))):
            pts_3d = {g: val[0] for g, val in viterbi_post.items()}
            obs_by_gid = {g: val[1] for g, val in viterbi_post.items()}
            poses, pts_3d, Ks, dists = _run_sparse_bundle_adjustment(
                registered, comp_views, poses, Ks, dists, pts_3d, obs_by_gid, max_nfev=50
            )

        # 4b. Post-BA per-observation outlier pruning: drop single noisy/shaken view observations (>6 px)
        #     whenever an LED still has >=2 well-separated clean views, then polish with a final BA pass!
        pruned_any = False
        cam_centers = {}
        for v in registered:
            Rv, _ = cv2.Rodrigues(poses[v][0])
            cam_centers[v] = _cam_center(Rv, poses[v][1])
        for gid in list(pts_3d.keys()):
            X = pts_3d[gid]
            obs_map = obs_by_gid.get(gid, {})
            if len(obs_map) <= 2:
                continue
            scored = []
            for v, ci in obs_map.items():
                cand = comp_views[v].detections[gid][ci]
                proj = _project_point(X, poses[v][0], poses[v][1], Ks[v], dists[v])
                scored.append((float(np.hypot(proj[0] - cand.u, proj[1] - cand.v)), v, ci))
            good = [(e, v, ci) for e, v, ci in scored if e <= 6.0]
            if len(good) >= 2 and len(good) < len(scored):
                # Ensure remaining clean views still span a real physical baseline!
                g_views = [v for _, v, _ in good]
                max_b = max(
                    float(np.linalg.norm(cam_centers[va] - cam_centers[vb]))
                    for i, va in enumerate(g_views)
                    for vb in g_views[i + 1 :]
                )
                if max_b >= 0.25:
                    obs_by_gid[gid] = {v: ci for _, v, ci in good}
                    pruned_any = True
        if pruned_any:
            poses, pts_3d, Ks, dists = _run_sparse_bundle_adjustment(
                registered, comp_views, poses, Ks, dists, pts_3d, obs_by_gid, max_nfev=35
            )

        # 4c. Strip-jump spike filter: drop isolated single-LED outliers jumping >5x typical step from BOTH neighbors
        by_ctrl_chk: dict[str, list[LEDDescriptor]] = defaultdict(list)
        for d in led_table:
            by_ctrl_chk[d.controller_id].append(d)
        for descs in by_ctrl_chk.values():
            descs.sort(key=lambda d: d.led_index)
            meas_descs = [d for d in descs if d.global_id in pts_3d]
            if len(meas_descs) < 5:
                continue
            steps = [
                float(np.linalg.norm(pts_3d[b.global_id] - pts_3d[a.global_id])) / max(1, b.led_index - a.led_index)
                for a, b in zip(meas_descs, meas_descs[1:])
            ]
            med_step = max(float(np.median(steps)), 1e-3)
            to_drop: list[int] = []
            for k in range(1, len(meas_descs) - 1):
                dp, dc, dn = meas_descs[k - 1], meas_descs[k], meas_descs[k + 1]
                g1 = max(1, dc.led_index - dp.led_index)
                g2 = max(1, dn.led_index - dc.led_index)
                d_prev = float(np.linalg.norm(pts_3d[dc.global_id] - pts_3d[dp.global_id]))
                d_next = float(np.linalg.norm(pts_3d[dn.global_id] - pts_3d[dc.global_id]))
                d_skip = float(np.linalg.norm(pts_3d[dn.global_id] - pts_3d[dp.global_id]))
                if d_prev > med_step * max(5.0, g1 * 3.5) and d_next > med_step * max(5.0, g2 * 3.5) and d_skip < 0.6 * (d_prev + d_next):
                    to_drop.append(dc.global_id)
            for gid_drop in to_drop:
                pts_3d.pop(gid_drop, None)
                obs_by_gid.pop(gid_drop, None)

        # Compute final per-LED and per-camera reprojection errors
        comp_errs: list[float] = []
        cam_errs: dict[int, list[float]] = {v: [] for v in registered}
        for gid, X in pts_3d.items():
            errs = []
            for v, ci in obs_by_gid[gid].items():
                cand = comp_views[v].detections[gid][ci]
                proj = _project_point(X, poses[v][0], poses[v][1], Ks[v], dists[v])
                e = float(np.hypot(proj[0] - cand.u, proj[1] - cand.v))
                errs.append(e)
                cam_errs[v].append(e)
            mean_e = float(np.mean(errs)) if errs else 0.0
            # Filter extreme post-BA outliers
            if mean_e > 15.0:
                continue
            comp_errs.append(mean_e)
            conf = float(np.clip(1.0 - mean_e / 12.0, 0.1, 1.0)) * min(1.0, len(obs_by_gid[gid]) / 3.0)
            all_solved_points[gid] = (X, len(obs_by_gid[gid]), mean_e, comp_id, conf)

        # Record solved camera poses
        for v in registered:
            rv, tv = poses[v]
            R, _ = cv2.Rodrigues(rv)
            pos = _cam_center(R, tv)
            # In OpenCV camera coords, +Z is forward, -Y is up
            look = (R.T @ np.array([0.0, 0.0, 1.0])).ravel()
            up = (R.T @ np.array([0.0, -1.0, 0.0])).ravel()
            cv_obj = comp_views[v]
            all_solved_cameras.append(
                SolvedCameraPose(
                    view_id=cv_obj.view_id,
                    round_id=cv_obj.round_id,
                    camera_id=cv_obj.camera_id,
                    camera_name=cv_obj.camera_name,
                    rvec=[float(x) for x in rv],
                    tvec=[float(x) for x in tv],
                    position=[float(x) for x in pos],
                    look_dir=[float(x) for x in look],
                    up_dir=[float(x) for x in up],
                    component_id=comp_id,
                    inlier_count=len(cam_errs[v]),
                    median_reproj_px=float(np.median(cam_errs[v])) if cam_errs[v] else 0.0,
                )
            )

        comp_summaries.append(
            ComponentSummary(
                component_id=comp_id,
                view_ids=[comp_views[v].view_id for v in registered],
                led_count=sum(1 for val in all_solved_points.values() if val[3] == comp_id),
                median_reproj_px=float(np.median(comp_errs)) if comp_errs else 0.0,
            )
        )

    # 5. Center primary component, auto-level to true gravity (if Android IMU gravity vectors exist),
    #    and apply user alignment (scale & rotation)
    if all_solved_points:
        primary_pts = np.array([v[0] for v in all_solved_points.values() if v[3] == 0], dtype=np.float64)
        if len(primary_pts) == 0:
            primary_pts = np.array([v[0] for v in all_solved_points.values()], dtype=np.float64)
        centroid = np.mean(primary_pts, axis=0)

        # Compute consensus hardware gravity direction from any registered Android cameras!
        view_by_id = {v.view_id: v for v in views}
        grav_world_vecs: list[np.ndarray] = []
        for cam in all_solved_cameras:
            if cam.component_id != 0:
                continue
            vo = view_by_id.get(cam.view_id)
            if vo and vo.gravity_cam and len(vo.gravity_cam) == 3:
                R_c, _ = cv2.Rodrigues(np.array(cam.rvec, dtype=np.float64))
                g_c = np.array(vo.gravity_cam, dtype=np.float64)
                gn = float(np.linalg.norm(g_c))
                if gn > 0.1:
                    grav_world_vecs.append((R_c.T @ (g_c / gn)).ravel())

        flip_y = np.diag([1.0, -1.0, -1.0])
        if grav_world_vecs:
            # Mean downward gravity in OpenCV world -> transform by flip_y -> align with Three.js -Y ([0, -1, 0])
            g_mean = np.mean(grav_world_vecs, axis=0)
            g_three = flip_y @ g_mean
            R_level = _rotation_align_vectors(g_three, np.array([0.0, -1.0, 0.0], dtype=np.float64))
        else:
            R_level = np.eye(3, dtype=np.float64)

        R_user = _euler_xyz_matrix(rot_deg) @ R_level @ flip_y

        def _xform_pt(p: np.ndarray) -> np.ndarray:
            return (R_user @ (p - centroid)) * scale_meters_per_unit

        for gid in list(all_solved_points.keys()):
            X, vc, me, cid, conf = all_solved_points[gid]
            all_solved_points[gid] = (_xform_pt(X), vc, me, cid, conf)

        for cam in all_solved_cameras:
            cam.position = [float(x) for x in _xform_pt(np.array(cam.position, dtype=np.float64))]
            cam.look_dir = [float(x) for x in (R_user @ np.array(cam.look_dir, dtype=np.float64))]
            cam.up_dir = [float(x) for x in (R_user @ np.array(cam.up_dir, dtype=np.float64))]

    # 6. Strip interpolation for occluded LEDs (explicitly tagged source="interpolated")
    seen_view_counts: dict[int, int] = defaultdict(int)
    for v in views:
        for gid in v.detections:
            seen_view_counts[gid] += 1

    interpolated: dict[int, tuple[np.ndarray, int, float]] = {}
    if interpolate_occluded and all_solved_points:
        by_ctrl: dict[str, list[LEDDescriptor]] = defaultdict(list)
        for d in led_table:
            by_ctrl[d.controller_id].append(d)
        for cid, descs in by_ctrl.items():
            descs.sort(key=lambda d: d.led_index)
            measured_pos = [(idx, d) for idx, d in enumerate(descs) if d.global_id in all_solved_points]
            for (pos_a, da), (pos_b, db) in zip(measured_pos, measured_pos[1:]):
                gap = pos_b - pos_a
                if 1 < gap <= max_interp_gap:
                    Xa, _, _, comp_a, _ = all_solved_points[da.global_id]
                    Xb, _, _, comp_b, _ = all_solved_points[db.global_id]
                    if comp_a != comp_b:
                        continue
                    seg_len = float(np.linalg.norm(Xb - Xa))
                    for k in range(1, gap):
                        t = k / float(gap)
                        dk = descs[pos_a + k]
                        Xk = (1.0 - t) * Xa + t * Xb
                        # Uncertainty peaks in the middle of the gap
                        unc = seg_len * min(t, 1.0 - t) * 0.5
                        interpolated[dk.global_id] = (Xk, comp_a, unc)

    # Assemble final MappedLED3D list
    mapped_leds: list[MappedLED3D] = []
    measured_count = 0
    interp_count = 0
    single_view_count = 0
    unmapped_count = 0
    all_reproj: list[float] = []

    for d in led_table:
        gid = d.global_id
        if gid in all_solved_points:
            X, vc, me, comp_id, conf = all_solved_points[gid]
            measured_count += 1
            all_reproj.append(me)
            mapped_leds.append(
                MappedLED3D(
                    global_id=gid,
                    controller_id=d.controller_id,
                    controller_name=d.controller_name,
                    led_index=d.led_index,
                    x=float(X[0]),
                    y=float(X[1]),
                    z=float(X[2]),
                    source="measured",
                    views_count=vc,
                    reproj_error_px=round(me, 3),
                    confidence=round(conf, 3),
                    component_id=comp_id,
                    uncertainty=0.0,
                )
            )
        elif gid in interpolated:
            X, comp_id, unc = interpolated[gid]
            interp_count += 1
            mapped_leds.append(
                MappedLED3D(
                    global_id=gid,
                    controller_id=d.controller_id,
                    controller_name=d.controller_name,
                    led_index=d.led_index,
                    x=float(X[0]),
                    y=float(X[1]),
                    z=float(X[2]),
                    source="interpolated",
                    views_count=seen_view_counts.get(gid, 0),
                    reproj_error_px=0.0,
                    confidence=0.35,
                    component_id=comp_id,
                    uncertainty=round(unc, 4),
                )
            )
        else:
            vc = seen_view_counts.get(gid, 0)
            if vc == 1:
                single_view_count += 1
            else:
                unmapped_count += 1
            mapped_leds.append(
                MappedLED3D(
                    global_id=gid,
                    controller_id=d.controller_id,
                    controller_name=d.controller_name,
                    led_index=d.led_index,
                    x=0.0,
                    y=0.0,
                    z=0.0,
                    source="unmapped",
                    views_count=vc,
                    confidence=0.0,
                )
            )

    return Map3DResult(
        leds=mapped_leds,
        cameras=all_solved_cameras,
        components=comp_summaries,
        total_leds=len(led_table),
        measured_count=measured_count,
        interpolated_count=interp_count,
        single_view_count=single_view_count,
        unmapped_count=unmapped_count,
        median_reproj_px=round(float(np.median(all_reproj)), 3) if all_reproj else 0.0,
        solve_time_sec=round(time.monotonic() - t0, 3),
        scale_meters_per_unit=scale_meters_per_unit,
        rotation_euler_deg=rot_deg,
    )
