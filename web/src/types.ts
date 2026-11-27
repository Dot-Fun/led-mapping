export interface WLEDController {
  id: string
  name: string
  ip: string
  led_count: number
  version: string
  arch: string
  online: boolean
  selected: boolean
}

export interface CameraNode {
  id: string
  name: string
  model: string
  platform: 'macos' | 'android' | 'http'
  transport: 'local' | 'wifi' | 'usb_adb' | 'manual'
  base_url: string
  width: number
  height: number
  hardware_level: string
  battery: number
  online: boolean
  enabled: boolean
  supports_manual_exposure: boolean
  supports_manual_focus: boolean
  config_locked_by_other_app: boolean
  ev_min: number
  ev_max: number
  current_config: Record<string, unknown>
  intrinsics?: {
    fx: number
    fy: number
    cx: number
    cy: number
    source: string
  }
}

export interface MappedLED3D {
  global_id: number
  controller_id: string
  controller_name: string
  led_index: number
  x: number
  y: number
  z: number
  source: 'measured' | 'interpolated' | 'unmapped'
  views_count: number
  reproj_error_px: number
  confidence: number
  component_id: number
  uncertainty: number
}

export interface SolvedCameraPose {
  view_id: string
  round_id: string
  camera_id: string
  camera_name: string
  position: [number, number, number]
  look_dir: [number, number, number]
  up_dir: [number, number, number]
  component_id: number
  inlier_count: number
  median_reproj_px: number
}

export interface ComponentSummary {
  component_id: number
  view_ids: string[]
  led_count: number
  median_reproj_px: number
}

export interface Map3DResult {
  leds: MappedLED3D[]
  cameras: SolvedCameraPose[]
  components: ComponentSummary[]
  total_leds: number
  measured_count: number
  interpolated_count: number
  single_view_count: number
  unmapped_count: number
  median_reproj_px: number
  solve_time_sec: number
  scale_meters_per_unit: number
  rotation_euler_deg: [number, number, number]
}

export interface ScanRound {
  round_id: string
  label: string
  mode: string
  created_at: number
  duration_sec: number
  enabled: boolean
  views: Array<{
    view_id: string
    camera_id: string
    camera_name: string
    detections: Record<string, unknown[]>
  }>
}

export interface ProjectData {
  name: string
  controllers: WLEDController[]
  rounds: ScanRound[]
  map_3d: Map3DResult | null
}

export interface EffectMeta {
  id: string
  name: string
  description: string
  defaults: Record<string, unknown>
}

export interface EffectState {
  running: boolean
  effect_id: string
  fps: number
  brightness: number
  params: Record<string, unknown>
}
