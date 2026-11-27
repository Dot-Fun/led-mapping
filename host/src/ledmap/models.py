"""Core Pydantic models for controllers, cameras, scan rounds, and 3D maps."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class WLEDController(BaseModel):
    id: str  # e.g. "wled-f2d05c" or IP
    name: str
    ip: str
    led_count: int
    version: str = ""
    arch: str = "esp32"
    online: bool = True
    selected: bool = True
    mac: str = ""


class CameraIntrinsics(BaseModel):
    fx: float
    fy: float
    cx: float
    cy: float
    dist: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    source: str = "estimated_fov"


class CameraNode(BaseModel):
    id: str
    name: str
    model: str = ""
    platform: Literal["macos", "android", "http"] = "android"
    transport: Literal["local", "wifi", "usb_adb", "manual"] = "wifi"
    base_url: str
    device_param: str | None = None
    width: int = 1920
    height: int = 1080
    hardware_level: str = ""
    battery: int = -1
    online: bool = True
    enabled: bool = True
    supports_manual_exposure: bool = False
    supports_manual_focus: bool = False
    config_locked_by_other_app: bool = False
    ev_min: int = -12
    ev_max: int = 12
    exposure_min_ns: int = 100_000
    exposure_max_ns: int = 100_000_000
    iso_min: int = 50
    iso_max: int = 3200
    current_config: dict[str, Any] = Field(default_factory=dict)
    intrinsics: CameraIntrinsics | None = None
    gravity_cam: list[float] | None = None  # Unit gravity (down) vector in OpenCV camera coords
    last_seen: float = 0.0


class Candidate2D(BaseModel):
    u: float
    v: float
    score: float
    snr: float = 0.0
    hamming_dist: int = 0
    margin: float = 0.0
    psf_sigma: float = 1.5
    peak_intensity: float = 0.0


class ViewObservations(BaseModel):
    view_id: str  # "{round_id}:{camera_id}"
    round_id: str
    camera_id: str
    camera_name: str
    width: int
    height: int
    intrinsics: CameraIntrinsics
    # global_led_id (int as str in JSON) -> list of Candidate2D hypotheses (best first)
    detections: dict[int, list[Candidate2D]] = Field(default_factory=dict)
    color_matrix: list[list[float]] | None = None
    gravity_cam: list[float] | None = None
    timestamp: float = 0.0


class ScanRound(BaseModel):
    round_id: str
    label: str = ""
    mode: Literal["ternary_ecc", "delta_ternary", "sequential"] = "ternary_ecc"
    created_at: float
    duration_sec: float = 0.0
    enabled: bool = True
    views: list[ViewObservations] = Field(default_factory=list)


class LEDDescriptor(BaseModel):
    global_id: int
    controller_id: str
    controller_name: str
    led_index: int


class MappedLED3D(BaseModel):
    global_id: int
    controller_id: str
    controller_name: str
    led_index: int
    x: float
    y: float
    z: float
    source: Literal["measured", "interpolated", "unmapped"] = "measured"
    views_count: int = 0
    reproj_error_px: float = 0.0
    confidence: float = 1.0
    component_id: int = 0
    uncertainty: float = 0.0


class SolvedCameraPose(BaseModel):
    view_id: str
    round_id: str
    camera_id: str
    camera_name: str
    rvec: list[float]  # Rodrigues 3-vector (world -> cam)
    tvec: list[float]  # 3-vector (world -> cam)
    position: list[float]  # 3D camera center in world coordinates
    look_dir: list[float]  # Unit forward vector in world coordinates
    up_dir: list[float]  # Unit up vector in world coordinates
    component_id: int = 0
    inlier_count: int = 0
    median_reproj_px: float = 0.0


class ComponentSummary(BaseModel):
    component_id: int
    view_ids: list[str]
    led_count: int
    median_reproj_px: float = 0.0


class Map3DResult(BaseModel):
    leds: list[MappedLED3D] = Field(default_factory=list)
    cameras: list[SolvedCameraPose] = Field(default_factory=list)
    components: list[ComponentSummary] = Field(default_factory=list)
    total_leds: int = 0
    measured_count: int = 0
    interpolated_count: int = 0
    single_view_count: int = 0
    unmapped_count: int = 0
    median_reproj_px: float = 0.0
    solve_time_sec: float = 0.0
    scale_meters_per_unit: float = 1.0
    rotation_euler_deg: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class ProjectData(BaseModel):
    name: str
    created_at: float
    controllers: list[WLEDController] = Field(default_factory=list)
    led_table: list[LEDDescriptor] = Field(default_factory=list)
    rounds: list[ScanRound] = Field(default_factory=list)
    map_3d: Map3DResult | None = None
