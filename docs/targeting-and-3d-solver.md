# Optical LED Targeting & Multi-Round 3D Solver

> **Maintenance:** Update this document whenever you change [`host/src/ledmap/mapping/`](../host/src/ledmap/mapping/) or the auto-level endpoint in [`host/src/ledmap/api/server.py`](../host/src/ledmap/api/server.py).

Technical reference for the optical decoding pipeline and multi-round 3D SfM/BA solver. Keep every invariant below intact when tuning thresholds or adding capture modes.

---

## 1. DC-Balanced Ternary ECC Codebook (`codebook.py`)

Source: [`host/src/ledmap/mapping/codebook.py`](../host/src/ledmap/mapping/codebook.py) (`TernaryECCCodebook`, `DecodeResult`)

Encodes LEDs using pure primary symbols `{0: R, 1: G, 2: B}` via a systematic linear code $[n_{\text{frames}}, k_{\text{data}}, d_{\min} \ge 3]$ over $\mathrm{GF}(3)$:

- **Frame count scaling (`n_frames = k_data + r_parity`):**
  - $k_{\text{data}} = \max(3, \lceil \log_3(\max(n_{\text{leds}} + 2, 4)) \rceil)$ (reserves codeword `0` and pilot headroom).
  - $r_{\text{parity}} \ge 3$ is the smallest integer with $(3^r - 1)/2 \ge k_{\text{data}} + r$ (number of projective points in $\mathrm{PG}(r-1, 3)$).
  - Example budgets: $\le 727$ LEDs $\to k=6, r=3 \implies 9$ frames; up to $\sim 59\text{k}$ LEDs $\to k=10, r=3 \implies 13$ frames.
- **Guaranteed $d_{\min} \ge 3$:** Parity-check matrix $H = [A \mid I_r]$ uses pairwise linearly independent nonzero columns from $\mathrm{PG}(r-1, 3)$ (first nonzero entry normalized to `1`). No two columns are scalar multiples, so $d_{\min} \ge 3$ (corrects 1 symbol error, detects 2-error collisions). Systematic parity trits are $p = (-A d) \bmod 3$.
- **Golden-ratio stride + coset balancing:**
  - LED index $i$ maps to `val = ((i + 1) * stride) % 3^k` (remapping `0 -> 3^k - 1`), where `stride = round(3^k * 0.381966011)` adjusted until $\gcd(\text{stride}, 3) = 1$.
  - A deterministic coset mask `arange(n_frames) % 3` is added mod 3 to `[digits | parity]`. Adding a constant coset preserves exact linear $d_{\min} \ge 3$.
  - **Why:** Maintains $\sim 33/33/33\%$ R/G/B illumination per frame and per contiguous strip segment (prevents auto-exposure pumping on unlocked tablets/webcams) and ensures physically adjacent LEDs shine different primary colors in most frames (separating overlapping PSFs).
- **Vectorized soft-correlation decoder (`decode_traces`):**
  - `corr_matrix` has shape `(n_leds, n_frames * 3)` with `+1.0` on the target channel and `-0.5` on the two off channels, scaled by `1 / n_frames`. A pure color match scores `+1.0`; balanced gray/white scores `0.0`.
  - Evaluates $P$ active pixel traces `(P, n_frames, 3)` in `32768`-pixel chunks via BLAS matmul (`flat @ corr_matrix.T`) and `np.argpartition(..., -2)` for top-2 scores.
  - A pixel accepts `best_idx` (else `-1`) only when **all three** gates pass:
    - Hard-decision Hamming distance: `h_dist <= max_hamming` (default `1`, scanner uses `1`)
    - Soft correlation: `best_score >= min_score` (default `0.45`, scanner uses `0.42`)
    - Margin (`best - second_best`): `margin >= min_margin` (default `0.12`, scanner uses `0.10`)

---

## 2. Decode-First, Localize-Afterward Scanner (`scanner.py`, `calibration.py`)

Sources: [`host/src/ledmap/mapping/scanner.py`](../host/src/ledmap/mapping/scanner.py), [`host/src/ledmap/mapping/calibration.py`](../host/src/ledmap/mapping/calibration.py)

### Capture & Preamble (`run_scan_round`)
- **Ternary ECC modes (`ternary_ecc`, `delta_ternary`):** `4 + n_frames` captures total — 1 dark blackout baseline (`settle_sec + 0.08s`), 3 full-strip calibration frames (R, G, B at `brightness`), then `n_frames` codebook frames, followed by blackout. Stacks decode in parallel via `asyncio.to_thread(decode_camera_stack, ...)`.
- **Sequential fallback (`sequential`):** Lights one white LED per frame and extracts up to 3 connected-component centroids per LED.
- **One-time ChArUco calibration (`calibration.py`):** `7x5` `DICT_5X5_100` board (`40 mm` square / `30 mm` marker), requires `>= 3` frames with `>= 6` corners each, emits `CameraIntrinsics(..., source="charuco_calibrated")` with 5-element OpenCV distortion vector. Uncalibrated cameras default to `fx = fy = 0.85 * width`, `source="estimated_fov"`.

### Decode-First Pipeline (`decode_camera_stack`)
1. **Camera-wide 3×3 crosstalk calibration (`estimate_color_matrix`):**
   - Subtracts dark baseline from R/G/B preamble frames in RGB order.
   - Selects unsaturated bright pixels (`22 <= peak <= 242`, fallback `>= 15`), taking the L1-normalized median chromaticity per primary as columns of $M$ (`RGB_measured = M @ RGB_true`). Guards near-singular scenes (`|det(M)| < 0.05` $\to I_3$) and inverts to $M^{-1}$.
2. **Temporal chromatic-swing mask:**
   - Estimates `noise_sigma` via MAD (`1.4826 * median(|sub - median(sub)|)`) on a `1/16` strided subsample (`[::4, ::4]`) of `median_peak` — fast even on 4K frames.
   - `thresh = max(min_activity=16.0, 5.5 * max(noise_sigma, 1.5))`.
   - Active pixels must satisfy **all three**:
     - `median_peak >= thresh`
     - `chroma_max = max_t(max_ch - min_ch) >= max(14.0, 0.65 * thresh)`
     - `ch_swing = sum_c(max_t - min_t) >= max(35.0, 1.8 * thresh)`
   - **Why:** Static ambient lamps, lit walls, and broadband AE brightness drift lack per-channel R/G/B temporal switching and are discarded before decoding.
   - Safety cap: if `> 120,000` pixels pass on a noisy unlocked 4K frame, keeps the top `120,000` by `ch_swing` (`np.argpartition`).
3. **Color unmixing, L1 normalization, and vectorized decode:**
   - Applies `cal_traces = clip(traces @ M_inv.T, 0, None)` and normalizes each pixel/frame by its RGB channel sum (`cal_traces / max(sum, 1e-3)`), making decoding invariant to exposure drift and AE pumping.
   - Runs one vectorized `codebook.decode_traces(..., max_hamming=1, min_score=0.42, min_margin=0.10)` pass over all active pixels and populates `id_map`.
4. **Bounded NNLS multi-PSF unmixing (`refine_overlapping_cluster_nnls`):**
   - Runs 8-connected components on `binary_valid` (all decoded pixels).
   - For clusters with `bw <= 28`, `bh <= 28`, and `2 <= len(unique_ids) <= 6` (capped at **25 clusters/view** to keep runtime `< 0.5 s`), crops a 2px-padded `M_inv`-calibrated ROI and solves per-pixel `scipy.optimize.nnls(A, y_vec)` against the `(3*T, K)` binary codeword design matrix `A` (skipping pixels with `max(y_vec) < 8.0`).
   - Computes un-merged sub-pixel centroids per LED over `amap >= 0.3 * peak` (when `peak >= 10.0`) and stores them in `nnls_overrides[(cc_id, cid)]`.
5. **$O(P \log P)$ sorted-slice grouping & multi-hypothesis retention:**
   - Sorts valid pixels once by decoded ID (`np.argsort(v_ids, kind="mergesort")`) and slices via `np.flatnonzero(np.diff(s_ids))` — avoids scanning full 4K images per LED.
   - Clusters each LED's pixels with local 8-connected components when the bounding box is $\le 256 \times 256$, or `16 px` grid cells (`cell_y * 100000 + cell_x`) when widely separated (e.g., direct LED + distant reflection).
   - Scores each blob by `mean_sc * (1 + 0.5 * mean_mrg) * compact_bonus * (1 - 0.15 * min_ham)` with `compact_bonus = 1 / (1 + 0.15 * max(0, psf_sigma - 2.5))`, applying `nnls_overrides` when present.
   - Retains the top `max_candidates_per_led = 3` `Candidate2D` hypotheses per LED so reflections survive for downstream 3D epipolar + Strip Viterbi DP rejection.

---

## 3. Multi-Round 3D Graph Solver (`reconstruct.py` & Auto-Level)

Sources: [`host/src/ledmap/mapping/reconstruct.py`](../host/src/ledmap/mapping/reconstruct.py) (`solve_multi_round_3d`), [`host/src/ledmap/api/server.py`](../host/src/ledmap/api/server.py) (`POST /api/map/auto-level`)

### Pipeline Stages (per Co-Visibility Component)

1. **Co-visibility connected components (`_find_connected_components`):**
   - Builds an undirected view graph across all enabled rounds; connects two views when they share `>= 8` detected LEDs (falls back to `min_shared=5` if all components are singletons). Sorts largest LED coverage first. Disjoint forest/room zones solve independently without corrupting one another.
2. **Seed pair ranking & fallback:**
   - Scores every pair sharing `>= 6` LEDs: `len(shared) * diff_cam * (1.0 + 1.5 * (cal_i + cal_j))`, where `diff_cam = 1.0` for distinct `camera_id` else `0.15`, and `cal = 1.0` for `source != "estimated_fov"` else `0.0`.
   - Tries the **top 12** ranked pairs in order until `_solve_best_seed_pose` yields `>= 8` triangulated inliers. Fallback prevents zero-baseline stationary repeats from stalling initialization.
3. **Dual-model initialization (`_solve_best_seed_pose`, `_evaluate_seed_pose`):**
   - Evaluates **both** Model A (Essential Matrix: `findEssentialMat` RANSAC `prob=0.9995`, threshold `4.5 / max(mean_f, 200)` $\to$ `decomposeEssentialMat`) and Model B (Homography: `findHomography` RANSAC threshold `6.0 / max(mean_f, 200)` $\to$ `decomposeHomographyMat`).
   - **Why:** Planar/near-planar wall installations make $E$ degenerate and collapse into a cone; homography decomposition solves flat walls cleanly while $E$ handles full 3D volumes.
   - Scores every `(R, t)` candidate via DLT triangulation (`0.1 < z < 200`, parallax `>= 1.5°`, `max_reproj_px = 14.0`, `n_in >= 6`): `n_in * (1 + 0.5 * min(med_ang, 30) / 15) - 2 * med_err`.
   - Runs an immediate 2-view BA (`max_nfev=30`) so subsequent PnP views lock onto clean 3D structure.
4. **Incremental spanning-tree registration:**
   - Greedily picks the unregistered view with the most matches to `pts_3d` (`>= 4` required), solves `SOLVEPNP_EPNP` RANSAC (`400` iters; `25.0 px` for `estimated_fov` vs `10.0 px` for calibrated cameras), and refines inliers with `SOLVEPNP_ITERATIVE`.
   - **Intermediate BA gating:** Runs intermediate BA (`max_nfev=30`) **only** when the newly added view is `estimated_fov` (to refine its `(fx, k1)` immediately). Skips intermediate BA for already-calibrated cameras (e.g., Android `camera2_intrinsic`, `charuco_calibrated`) so 10+ round house/forest solves stay fast.
   - Triangulates newly co-visible LEDs after each registered view (`max_reproj_px=20.0`, `min_parallax_deg=5.0`, `min_baseline=0.25`).
5. **Multi-hypothesis enumeration (`_enumerate_led_hypotheses`):**
   - **Distinct-position seed view deduplication:** Greedily selects up to `12` seed views spaced `>= 0.20` apart in 3D camera center space (fallback to `obs_views[:12]` only if `< 2` distinct positions exist). Keeps stationary desk cameras spanning 10+ rounds from crowding out moved phone viewpoints.
   - **Physical baseline & parallax gates:** Rejects seed pairs with `||c_a - c_b|| < 0.25` (`min_baseline`) or triangulation angle `< 5.0°` (`min_parallax_deg`), enforces cheirality `0.05 < z < 200.0`, collects consensus across all observing views (top 3 blobs each), and re-triangulates with linear multi-view DLT.
   - Ranks up to 5 hypotheses per LED by `4.0 * distinct_cams + 1.5 * len(chosen) + 0.1 * min(ang, 45) + 2.0 * mean(cand_scores) - 0.5 * mean_err`.
   - Uses a generous `max_reproj_px = 24.0` before global distortion BA so wide-angle edge LEDs enter before `(fx, k1)` calibration.
6. **Strip Viterbi DP reflection rejection (`_viterbi_select_strip_hypotheses`):**
   - Runs per `controller_id` sorted by `led_index`.
   - Estimates `typical_step` as the 75th percentile of per-index step distances across unambiguous (`len(hyps) == 1`) adjacent pairs (fallback: top-1 hypotheses) and sets `max_step = max(2.8 * typical_step, 1e-3)`.
   - Minimizes unary cost `-quality` plus transition cost `min(excess * 3.0, 25.0)` where `excess = max(0, dist - max_step * min(gap, 8)) / max(typical_step, 1e-3)`. Zero penalty within physical strip reach (allows tight 3D bends/draping), capped steep penalty on reflection jumps, and `min(gap, 8)` allows clean breaks across large occlusions.
7. **Sparse TRF Bundle Adjustment (`_run_sparse_bundle_adjustment`):**
   - `scipy.optimize.least_squares(..., method="trf", loss="cauchy", f_scale=2.0, x_scale="jac")` with explicit `lil_matrix` `jac_sparsity`.
   - Holds camera 0 fixed and adds a scale-anchor residual `10.0 * (||t1|| - cam1_t_norm)` at index `2 * n_obs`.
   - **Joint `(fx, k1)` self-calibration:** Optimizes isotropic focal length (`fx = fy >= 200.0`) and radial distortion (`k1 in [-0.8, 0.8]`) for `estimated_fov` cameras **only when** `has_calibrated` is true (at least one calibrated camera anchors scale/focal length in the component).
8. **Post-calibration re-triangulation, pruning, and spike filtering:**
   - **4a. Re-triangulation:** After global BA (`max_nfev=60`), re-enumerates hypotheses at tight `max_reproj_px = 12.0` + re-runs Strip Viterbi DP; accepts when `len(viterbi_post) >= max(4, int(0.8 * len(pts_3d)))`, followed by BA (`max_nfev=50`).
   - **4b. Per-observation outlier pruning:** For LEDs with `> 2` views, drops any single view with reprojection error `> 6.0 px` if `>= 2` clean views remain **and** their max pairwise camera baseline stays `>= 0.25`. Runs polish BA (`max_nfev=35`) if anything was pruned.
   - **4c. Strip-jump spike filter:** Per controller (`>= 5` measured LEDs), drops any isolated LED jumping `> med_step * max(5.0, gap * 3.5)` from **both** neighbors while the neighbor-to-neighbor chord `d_skip < 0.6 * (d_prev + d_next)`. Final gate drops any point with `mean_reproj_px > 15.0`.
9. **Gravity auto-leveling, RANSAC vertical plane leveling, and strip interpolation:**
   - **Hardware gravity (`reconstruct.py` step 5):** Centers component 0, flips OpenCV coords to Three.js `+Y` up via `flip_y = diag(1, -1, -1)`. If registered component-0 views carry Android IMU `gravity_cam` (unit down vector in OpenCV camera coords), averages world-frame vectors `R_c.T @ (g_c / ||g_c||)` and aligns to `[0, -1, 0]` (`R_user = _euler_xyz_matrix(rot_deg) @ R_level @ flip_y`).
   - **RANSAC vertical plane auto-level (`POST /api/map/auto-level` in `server.py`):** Re-solves canonical `[0, 0, 0]`, requires `>= 10` measured component-0 LEDs, runs 250-iteration RANSAC (`dist_thresh = 0.04 * span`, mostly-vertical constraint `|n_y| <= 0.5`), fits SVD plane normal oriented `+Z`, solves exact `[pitch_deg, yaw_deg, 0.0]` so `Ry(yaw) @ Rx(pitch) @ normal == [0, 0, 1]`, and re-solves.
   - **Tagged strip interpolation (`reconstruct.py` step 6):** Linearly fills occluded gaps `1 < gap <= max_interp_gap` (default `8`) within the same controller and `component_id`, tagged `source="interpolated"`, `confidence=0.35`, and `uncertainty = seg_len * min(t, 1 - t) * 0.5`. Unresolved LEDs are `source="unmapped"` (tallied under `single_view_count` when `views_count == 1`).

---

## 4. Hard-Won Pitfalls & Verification

| Pitfall | Failure Mode | Required Invariant |
| --- | --- | --- |
| **Android `LENS_DISTORTION` bug** | Camera2 JPEGs are already ISP-undistorted and Camera2 distortion order differs from OpenCV `[k1, k2, p1, p2, k3]`; applying it warps rays and wrecks PnP/BA. | `_dist_from_intrinsics` loads `intr.dist` **only** when `source == "charuco_calibrated"`; returns zeros otherwise. |
| **Stationary-camera self-triangulation** | Fixed desk webcams across 10+ rounds form zero-baseline pairs that triangulate floating clumps and starve moved phones out of seed views. | Keep all 4 defenses: `0.15x` same-`camera_id` seed penalty + top-12 fallback; `>= 0.20` distinct-position seed deduplication; `min_baseline >= 0.25` & `min_parallax_deg >= 5.0°`; post-BA pruning `max_b >= 0.25` check. |
| **Planar wall cone collapse** | Essential-matrix-only initialization is degenerate on flat walls (e.g., wall LED trees), collapsing the map into a cone. | Always evaluate both Essential Matrix and Homography decomposition in `_solve_best_seed_pose`. |
| **Unanchored `(fx, k1)` drift** | Optimizing webcam focal length in BA with zero calibrated cameras lets scale and focal length drift together. | Gate `(fx, k1)` BA parameters on `has_calibrated` (`source != "estimated_fov"`). |
| **Intermediate BA slowdown** | Running BA on every registered phone camera makes 10+ round house/forest solves painfully slow. | Trigger intermediate BA during incremental registration **only** for `estimated_fov` cameras. |
| **Unlocked AE / ambient flood** | Dropping temporal chromatic-swing thresholds or per-frame L1 normalization lets static lamps and AE drift flood `decode_traces`. | Keep all three mask gates (`median_peak`, `chroma_max`, `ch_swing`), the `120,000`-pixel cap, and per-frame L1 normalization. |

### Verification Commands

Run from `host/` (uses `uv` virtualenv and `pyproject.toml` pytest config):

```bash
# Targeted optical codebook, Decode-First NNLS, multi-round stitching, Viterbi DP & Sparse BA tests
cd host && uv run pytest tests/test_codebook_and_decode.py tests/test_reconstruct_multiround.py

# Full host test suite
cd host && uv run pytest
```
