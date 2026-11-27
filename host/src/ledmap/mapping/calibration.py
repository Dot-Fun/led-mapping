"""One-time ChArUco board generation and per-camera intrinsic calibration."""
from __future__ import annotations

import cv2
import numpy as np

from ledmap.models import CameraIntrinsics

CHARUCO_SQUARES_X = 7
CHARUCO_SQUARES_Y = 5
CHARUCO_SQUARE_LEN = 0.04  # 40 mm
CHARUCO_MARKER_LEN = 0.03  # 30 mm


def _get_charuco_board() -> tuple[cv2.aruco.Dictionary, cv2.aruco.CharucoBoard]:
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)
    board = cv2.aruco.CharucoBoard(
        (CHARUCO_SQUARES_X, CHARUCO_SQUARES_Y),
        CHARUCO_SQUARE_LEN,
        CHARUCO_MARKER_LEN,
        aruco_dict,
    )
    return aruco_dict, board


def render_charuco_png(width_px: int = 1400, height_px: int = 1000) -> bytes:
    """Render a crisp ChArUco calibration board PNG for displaying on-screen or printing."""
    _, board = _get_charuco_board()
    img = board.generateImage((width_px, height_px), marginSize=40, borderBits=1)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Failed to encode ChArUco PNG")
    return buf.tobytes()


def detect_charuco_corners(bgr: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Detect ChArUco chessboard corners in a single BGR image."""
    _, board = _get_charuco_board()
    detector = cv2.aruco.CharucoDetector(board)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    corners, ids, _, _ = detector.detectBoard(gray)
    if corners is None or ids is None or len(ids) < 6:
        return None, None
    return corners, ids


def calibrate_camera_from_frames(frames: list[np.ndarray]) -> tuple[CameraIntrinsics, float, int]:
    """Calibrate camera matrix + distortion from multiple ChArUco frames.

    Returns (CameraIntrinsics, rms_reproj_error_px, usable_frame_count).
    """
    if not frames:
        raise ValueError("No calibration frames provided")
    _, board = _get_charuco_board()
    obj_points: list[np.ndarray] = []
    img_points: list[np.ndarray] = []
    img_size: tuple[int, int] | None = None

    for bgr in frames:
        h, w = bgr.shape[:2]
        img_size = (w, h)
        corners, ids = detect_charuco_corners(bgr)
        if corners is None or ids is None:
            continue
        obj_p, img_p = board.matchImagePoints(corners, ids)
        if obj_p is not None and img_p is not None and len(obj_p) >= 6:
            obj_points.append(obj_p.astype(np.float32))
            img_points.append(img_p.astype(np.float32))

    if len(obj_points) < 3 or img_size is None:
        raise ValueError(f"Need at least 3 valid ChArUco detections, got {len(obj_points)}")

    rms, K, dist, _, _ = cv2.calibrateCamera(
        obj_points,
        img_points,
        img_size,
        None,
        None,
    )
    d_flat = [float(x) for x in dist.ravel()[:5]]
    while len(d_flat) < 5:
        d_flat.append(0.0)
    intr = CameraIntrinsics(
        fx=float(K[0, 0]),
        fy=float(K[1, 1]),
        cx=float(K[0, 2]),
        cy=float(K[1, 2]),
        dist=d_flat,
        source="charuco_calibrated",
    )
    return intr, float(rms), len(obj_points)
