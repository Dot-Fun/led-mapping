import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { DotfunLogo } from './components/DotfunLogo'
import { Viewport3D } from './components/Viewport3D'
import type {
  CameraNode,
  EffectMeta,
  EffectState,
  MappedLED3D,
  ProjectData,
  WLEDController,
} from './types'
import { playEasterEgg, playLedPing, playLevelSnap, playScanComplete } from './utils/sound'

type DrawerMode = 'effects' | 'scan' | 'cameras' | 'controllers' | 'closed'

interface CmdItem {
  id: string
  group: string
  label: string
  hint?: string
  action: () => void | Promise<void>
}

const DOTFUN_MOTTOS = [
  'Run Good · Look Good · Feel Good.',
  'No Smoke, No Mirrors. Just real 3D coordinates.',
  'Fluff Free, Fun Forward. Now go light up the woods.',
  'Human first. Photons second.',
]

const SPARK_VECTORS = [
  { sx: '-44px', sy: '-32px', color: '#ff3c00' },
  { sx: '-24px', sy: '-46px', color: '#e9cf77' },
  { sx: '0px', sy: '-52px', color: '#fffbed' },
  { sx: '26px', sy: '-44px', color: '#ff3c00' },
  { sx: '46px', sy: '-28px', color: '#e9cf77' },
  { sx: '48px', sy: '14px', color: '#ff3c00' },
  { sx: '26px', sy: '38px', color: '#e9cf77' },
  { sx: '-18px', sy: '40px', color: '#fffbed' },
  { sx: '-46px', sy: '16px', color: '#ff3c00' },
  { sx: '-36px', sy: '-10px', color: '#e9cf77' },
]

export default function App() {
  const [drawer, setDrawer] = useState<DrawerMode>('effects')
  const [showPip, setShowPip] = useState(true)
  const [cmdOpen, setCmdOpen] = useState(false)
  const [cmdQuery, setCmdQuery] = useState('')
  const [qrOpen, setQrOpen] = useState(false)
  const [qrLoading, setQrLoading] = useState(false)
  const [installUrls, setInstallUrls] = useState<Array<{ label: string; ip: string; url: string }>>([])

  const [controllers, setControllers] = useState<WLEDController[]>([])
  const [cameras, setCameras] = useState<CameraNode[]>([])
  const [project, setProject] = useState<ProjectData | null>(null)
  const [effects, setEffects] = useState<EffectMeta[]>([])
  const [effState, setEffState] = useState<EffectState | null>(null)
  const [liveRgb, setLiveRgb] = useState<Uint8Array | null>(null)

  const [scanMsg, setScanMsg] = useState<string>('')
  const [scanPct, setScanPct] = useState<number>(0)
  const [scanning, setScanning] = useState(false)
  const [walkAround, setWalkAround] = useState(false)
  const [walkIntervalSec, setWalkIntervalSec] = useState(8)
  const [walkCountdown, setWalkCountdown] = useState(0)
  const [scanMode, setScanMode] = useState<'ternary_ecc' | 'delta_ternary' | 'sequential'>('ternary_ecc')
  const [brightness, setBrightness] = useState(140)
  const [settleMs, setSettleMs] = useState(110)

  const [selectedLed, setSelectedLed] = useState<MappedLED3D | null>(null)
  const [colorMode, setColorMode] = useState<'live' | 'coverage' | 'controller'>('live')
  const [showCameras, setShowCameras] = useState(true)
  const [newWledIp, setNewWledIp] = useState('')
  const [newCamUrl, setNewCamUrl] = useState('')
  const [rotEuler, setRotEuler] = useState<[number, number, number]>([0, 0, 0])
  const [scaleM, setScaleM] = useState(1.0)
  const [toast, setToast] = useState<{ kind: 'info' | 'error' | 'celebrate'; text: string } | null>(null)
  const [sparkKey, setSparkKey] = useState<number>(0)
  const [brandSparkKey, setBrandSparkKey] = useState<number>(0)
  const [sfxEnabled, setSfxEnabled] = useState<boolean>(() => {
    try {
      return localStorage.getItem('dotfun_ledmap_sfx') !== 'off'
    } catch {
      return true
    }
  })

  const cmdInputRef = useRef<HTMLInputElement | null>(null)
  const brandClicksRef = useRef<number>(0)
  const mottoIdxRef = useRef<number>(0)
  const sfxRef = useRef<boolean>(sfxEnabled)

  useEffect(() => {
    sfxRef.current = sfxEnabled
    try {
      localStorage.setItem('dotfun_ledmap_sfx', sfxEnabled ? 'on' : 'off')
    } catch {
      // ignore
    }
  }, [sfxEnabled])

  // DevTools console easter egg for curious builders
  useEffect(() => {
    console.log(
      '%c dotfun %c 3D LED Mapping Studio — Run Good · Look Good · Feel Good · https://www.dotfun.co',
      'background:#ff3c00;color:#fffbed;font-weight:800;padding:3px 7px;border-radius:4px;',
      'color:#e9cf77;font-weight:600;padding:3px 4px;'
    )
  }, [])

  const notify = useCallback((kind: 'info' | 'error' | 'celebrate', text: string) => {
    setToast({ kind, text })
  }, [])

  const triggerCoverageSpark = useCallback(() => {
    setSparkKey((k) => k + 1)
  }, [])

  const handleBrandClick = useCallback(() => {
    brandClicksRef.current += 1
    if (brandClicksRef.current >= 5) {
      brandClicksRef.current = 0
      const motto = DOTFUN_MOTTOS[mottoIdxRef.current % DOTFUN_MOTTOS.length]
      mottoIdxRef.current += 1
      setBrandSparkKey((k) => k + 1)
      playEasterEgg(sfxRef.current)
      notify('celebrate', `dotfun — ${motto}`)
    }
  }, [notify])

  const refreshAll = useCallback(async () => {
    try {
      const [cRes, camRes, pRes, eRes] = await Promise.all([
        fetch('/api/controllers').then((r) => r.json()),
        fetch('/api/cameras').then((r) => r.json()),
        fetch('/api/project').then((r) => r.json()),
        fetch('/api/effects').then((r) => r.json()),
      ])
      setControllers(cRes.controllers || [])
      setCameras(camRes.cameras || [])
      setProject(pRes.project || null)
      setScanning(Boolean(pRes.scanning))
      setEffects(eRes.effects || [])
      setEffState(eRes.state || null)
      if (pRes.project?.map_3d) {
        setScaleM(pRes.project.map_3d.scale_meters_per_unit || 1.0)
        setRotEuler(pRes.project.map_3d.rotation_euler_deg || [0, 0, 0])
      }
    } catch (e) {
      notify('error', `Studio host offline: ${String(e)}`)
    }
  }, [notify])

  useEffect(() => {
    refreshAll()
    const timer = setInterval(refreshAll, 5000)
    return () => clearInterval(timer)
  }, [refreshAll])

  // WebSocket for live scan progress + live 3D RGB mirror
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/ws`)
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'live_rgb' && msg.rgb_b64) {
          const bin = atob(msg.rgb_b64)
          const arr = new Uint8Array(bin.length)
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
          setLiveRgb(arr)
        } else if (msg.type === 'scan_progress') {
          setScanMsg(`${msg.message} (${msg.step}/${msg.total})`)
          setScanPct(Math.round((msg.step / Math.max(1, msg.total)) * 100))
        } else if (msg.type === 'scan_complete') {
          setScanMsg(`Round ${msg.round_id} stitched`)
          setScanPct(100)
          setScanning(false)
          playScanComplete(sfxRef.current)
          triggerCoverageSpark()
          notify('celebrate', `Round ${msg.round_id} stitched into the 3D map.`)
          refreshAll()
        } else if (msg.type === 'cal_progress') {
          setScanMsg(msg.message)
        }
      } catch {
        // ignore
      }
    }
    return () => ws.close()
  }, [notify, refreshAll, triggerCoverageSpark])

  const apiPost = useCallback(
    async (url: string, body?: unknown) => {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      })
      const data = await r.json().catch(() => ({}))
      if (!r.ok) {
        notify('error', data.detail || `Request failed (${r.status})`)
        throw new Error(data.detail || 'Request failed')
      }
      return data
    },
    [notify]
  )

  const handleStartScan = useCallback(async () => {
    if (scanning) return
    setScanning(true)
    setScanPct(4)
    setScanMsg('Flashing GF(3) ternary codebook across the fleet...')
    try {
      await apiPost('/api/scan/start', {
        mode: scanMode,
        brightness,
        settle_ms: settleMs,
      })
      await refreshAll()
    } catch {
      setScanning(false)
    }
  }, [apiPost, brightness, refreshAll, scanMode, scanning, settleMs])

  const togglePlay = useCallback(async () => {
    if (effState?.running) {
      const res = await apiPost('/api/effects/stop?blackout=false')
      setEffState(res.state)
    } else {
      const res = await apiPost('/api/effects/start', {
        effect_id: effState?.effect_id || 'sweep_plane',
        brightness: effState?.brightness ?? 0.75,
      })
      setEffState(res.state)
      setColorMode('live')
    }
  }, [apiPost, effState])

  const handlePaintUnmapped = useCallback(async () => {
    await apiPost('/api/map/paint-coverage')
    setColorMode('coverage')
    notify('info', 'Accuracy heatmap live—green is locked (<3px), yellow needs another angle, red is unseen.')
  }, [apiPost, notify])

  // Hands-Free Walk-Around Refine Loop: move a phone around -> auto-scans every N seconds & paints heatmap
  useEffect(() => {
    if (!walkAround) {
      setWalkCountdown(0)
      return
    }
    let remaining = walkIntervalSec
    setWalkCountdown(remaining)
    const tick = setInterval(async () => {
      if (scanning) return
      remaining -= 1
      setWalkCountdown(Math.max(0, remaining))
      if (remaining <= 0) {
        remaining = walkIntervalSec
        setWalkCountdown(remaining)
        try {
          await handleStartScan()
          await apiPost('/api/map/paint-coverage')
        } catch {
          // keep running or let user toggle off
        }
      }
    }, 1000)
    return () => clearInterval(tick)
  }, [apiPost, handleStartScan, scanning, walkAround, walkIntervalSec])

  const handleLockAllCams = useCallback(async () => {
    await apiPost('/api/cameras/lock-all')
    playLevelSnap(sfxRef.current)
    notify('info', 'Locked exposure, white balance, and focus across active cameras.')
    await refreshAll()
  }, [apiPost, notify, refreshAll])

  const handleSelectLed = useCallback(async (led: MappedLED3D) => {
    setSelectedLed(led)
    playLedPing(sfxRef.current)
    try {
      await fetch('/api/map/blink-led', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ global_id: led.global_id, duration_ms: 900 }),
      })
    } catch {
      // ignore
    }
  }, [])

  // Global keyboard shortcuts (field-friendly zero-mouse workflow)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setCmdOpen((v) => !v)
        setCmdQuery('')
        setTimeout(() => cmdInputRef.current?.focus(), 20)
        return
      }
      if (e.key === 'Escape' && cmdOpen) {
        setCmdOpen(false)
        return
      }
      if (e.key === 'Escape' && qrOpen) {
        setQrOpen(false)
        return
      }
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || cmdOpen) return
      if (e.key === ' ') {
        e.preventDefault()
        togglePlay()
      } else if (e.key.toLowerCase() === 's') {
        handleStartScan()
      } else if (e.key.toLowerCase() === 'p') {
        handlePaintUnmapped()
      } else if (e.key.toLowerCase() === 'l') {
        handleLockAllCams()
      } else if (e.key === '1') {
        setDrawer('effects')
      } else if (e.key === '2') {
        setDrawer('scan')
      } else if (e.key === '3') {
        setDrawer('cameras')
      } else if (e.key === '4') {
        setDrawer('controllers')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cmdOpen, handleLockAllCams, handlePaintUnmapped, handleStartScan, qrOpen, togglePlay])

  const loadInstallUrls = useCallback(async () => {
    setQrLoading(true)
    try {
      const r = await fetch('/api/install-urls')
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const data = await r.json()
      setInstallUrls(data.urls || [])
    } catch {
      setTimeout(async () => {
        try {
          const r2 = await fetch('/api/install-urls')
          if (r2.ok) {
            const d2 = await r2.json()
            setInstallUrls(d2.urls || [])
          }
        } catch {
          // ignore
        } finally {
          setQrLoading(false)
        }
      }, 600)
      return
    }
    setQrLoading(false)
  }, [])

  useEffect(() => {
    loadInstallUrls()
  }, [loadInstallUrls])

  const openQrModal = useCallback(() => {
    setQrOpen(true)
    loadInstallUrls()
  }, [loadInstallUrls])

  const commands = useMemo<CmdItem[]>(() => {
    const base: CmdItem[] = [
      { id: 'qr', group: 'Fleet', label: 'Add Android Phone (Show Install QR Code)', action: openQrModal },
      { id: 'scan', group: 'Field Actions', label: 'Run Multi-Camera Scan Round', hint: 'S', action: handleStartScan },
      { id: 'paint', group: 'Field Actions', label: 'Paint Unmapped Physical LEDs (Green / Yellow / Red)', hint: 'P', action: handlePaintUnmapped },
      { id: 'lock', group: 'Field Actions', label: 'Lock All Camera Exposures for LEDs', hint: 'L', action: handleLockAllCams },
      { id: 'play', group: 'Playback', label: effState?.running ? 'Stop DDP 3D Stream' : 'Start DDP 3D Stream', hint: 'Space', action: togglePlay },
      { id: 'pip', group: 'View', label: showPip ? 'Hide Live Camera PIP Tray' : 'Show Live Camera PIP Tray', action: () => setShowPip((v) => !v) },
      { id: 'sfx', group: 'View', label: sfxEnabled ? 'Mute Tactile Sound Cues' : 'Enable Tactile Sound Cues', action: () => setSfxEnabled((v) => !v) },
      { id: 'refresh', group: 'Fleet', label: 'Refresh WLED & Camera Fleet (mDNS / UDP / ADB)', action: async () => { await apiPost('/api/controllers/refresh'); await apiPost('/api/cameras/refresh'); await refreshAll() } },
    ]
    for (const e of effects) {
      base.push({
        id: `eff:${e.id}`,
        group: '3D Effects',
        label: `Play Effect: ${e.name}`,
        action: async () => {
          const res = await apiPost('/api/effects/start', { effect_id: e.id, params: e.defaults })
          setEffState(res.state)
          setColorMode('live')
        },
      })
    }
    for (const c of controllers) {
      base.push({
        id: `wled:${c.id}`,
        group: 'Identify WLED',
        label: `Identify Flash: ${c.name} (${c.led_count} LEDs)`,
        hint: c.ip,
        action: () => apiPost(`/api/controllers/${encodeURIComponent(c.id)}/identify`),
      })
    }
    return base
  }, [apiPost, controllers, effState?.running, effects, handleLockAllCams, handlePaintUnmapped, handleStartScan, openQrModal, refreshAll, sfxEnabled, showPip, togglePlay])

  const filteredCmds = useMemo(() => {
    const q = cmdQuery.trim().toLowerCase()
    if (!q) return commands
    return commands.filter((c) => c.label.toLowerCase().includes(q) || c.group.toLowerCase().includes(q))
  }, [cmdQuery, commands])

  const map3d = project?.map_3d ?? null
  const enabledCams = cameras.filter((c) => c.enabled && c.online)
  const selectedCtrls = controllers.filter((c) => c.selected && c.online)
  const totalSelectedLeds = selectedCtrls.reduce((acc, c) => acc + c.led_count, 0)
  const coveragePct = map3d && map3d.total_leds > 0
    ? Math.round(((map3d.measured_count + map3d.interpolated_count) / map3d.total_leds) * 100)
    : 0

  return (
    <div className="stage-root">
      {/* FULL-BLEED 3D STAGE */}
      <div className="stage-canvas">
        <Viewport3D
          map3d={map3d}
          liveRgb={liveRgb}
          selectedLedId={selectedLed?.global_id ?? null}
          onSelectLed={handleSelectLed}
          showCameras={showCameras}
          colorMode={colorMode}
        />
      </div>

      {/* TOP BENTO TELEMETRY HUD */}
      <header className="hud-top">
        <button
          className="hud-brand"
          onClick={handleBrandClick}
          title="dotfun 3D LED Mapping Studio"
        >
          <span className="brand-logo-wrap">
            <DotfunLogo height={20} />
          </span>
          <span className="brand-divider" />
          <div className="brand-meta">
            <div className="brand-title">
              <span className={`status-orb ${effState?.running ? 'live' : ''}`} />
              <span>LEDMAP 3D</span>
            </div>
            <div className="brand-sub">{project?.name || 'default'} · {project?.rounds.length ?? 0} round{(project?.rounds.length ?? 0) === 1 ? '' : 's'}</div>
          </div>
          {brandSparkKey > 0 && (
            <span key={brandSparkKey} className="spark-layer" aria-hidden="true">
              {SPARK_VECTORS.map((v, i) => (
                <span
                  key={i}
                  className="spark-dot"
                  style={{ '--sx': v.sx, '--sy': v.sy, background: v.color } as React.CSSProperties}
                />
              ))}
            </span>
          )}
        </button>

        <div className="bento-strip">
          <button className="kpi-chip" onClick={() => setDrawer(drawer === 'controllers' ? 'closed' : 'controllers')}>
            <span className="kpi-label">WLED FLEET</span>
            <span className="kpi-val">{selectedCtrls.length} <small>({totalSelectedLeds} LEDs)</small></span>
          </button>
          <button className="kpi-chip" onClick={() => setDrawer(drawer === 'cameras' ? 'closed' : 'cameras')}>
            <span className="kpi-label">CAMERA FLEET</span>
            <span className="kpi-val">{enabledCams.length}/{cameras.length} <small>online</small></span>
          </button>
          <button className="kpi-chip" onClick={() => setDrawer(drawer === 'scan' ? 'closed' : 'scan')}>
            <span className="kpi-label">3D COVERAGE</span>
            <span className="kpi-val accent">{coveragePct}% <small>{map3d ? `${map3d.measured_count}/${map3d.total_leds}` : 'unscanned'}</small></span>
            <span className="mini-bar"><span style={{ width: `${coveragePct}%` }} /></span>
            {sparkKey > 0 && (
              <span key={sparkKey} className="spark-layer" aria-hidden="true">
                {SPARK_VECTORS.map((v, i) => (
                  <span
                    key={i}
                    className="spark-dot"
                    style={{ '--sx': v.sx, '--sy': v.sy, background: v.color } as React.CSSProperties}
                  />
                ))}
              </span>
            )}
          </button>
          <div className="kpi-chip static">
            <span className="kpi-label">PRECISION</span>
            <span className="kpi-val">{map3d ? `${map3d.median_reproj_px}px` : '—'} <small>{map3d ? `${map3d.components.length} zone${map3d.components.length === 1 ? '' : 's'}` : ''}</small></span>
          </div>
        </div>

        <div className="hud-right-tools">
          <button
            className={`sfx-btn ${sfxEnabled ? '' : 'muted-sfx'}`}
            onClick={() => setSfxEnabled((v) => !v)}
            title={sfxEnabled ? 'Mute sound cues' : 'Unmute sound cues'}
            aria-label={sfxEnabled ? 'Mute sound cues' : 'Unmute sound cues'}
          >
            {sfxEnabled ? '♪' : '✕'}
          </button>
          <button className="cmd-trigger" onClick={() => { setCmdOpen(true); setCmdQuery(''); setTimeout(() => cmdInputRef.current?.focus(), 20) }}>
            <span>Command</span>
            <kbd>⌘K</kbd>
          </button>
        </div>
      </header>

      {/* TOAST / SCAN PROGRESS PILL */}
      {(scanning || toast) && (
        <div className={`hud-toast ${toast?.kind === 'error' ? 'error' : toast?.kind === 'celebrate' ? 'celebrate' : ''}`}>
          {scanning ? (
            <div className="scan-progress">
              <div className="scan-progress-row">
                <span>{scanMsg || 'Scanning...'}</span>
                <strong>{scanPct}%</strong>
              </div>
              <div className="progress-track"><div className="progress-fill" style={{ width: `${scanPct}%` }} /></div>
            </div>
          ) : (
            <>
              <span>{toast?.text}</span>
              <button className="icon-btn" onClick={() => setToast(null)} aria-label="Dismiss">✕</button>
            </>
          )}
        </div>
      )}

      {/* COLLAPSIBLE BOTTOM-LEFT LIVE CAMERA PIP STRIP */}
      {showPip && enabledCams.length > 0 && (
        <div className="pip-tray">
          <div className="pip-header">
            <span>LIVE CAMERAS ({enabledCams.length})</span>
            <div className="pip-actions">
              <button className="mini-btn" onClick={handleLockAllCams}>Lock AE</button>
              <button className="mini-btn" onClick={() => setShowPip(false)}>Hide</button>
            </div>
          </div>
          <div className="pip-row">
            {enabledCams.map((cam) => (
              <div key={cam.id} className="pip-tile" onClick={() => setDrawer('cameras')}>
                <img src={`/api/cameras/${encodeURIComponent(cam.id)}/preview`} alt={cam.name} />
                <div className="pip-caption">
                  <span>{cam.name}</span>
                  <span className="pip-meta">{cam.transport}{cam.battery >= 0 ? ` · ${cam.battery}%` : ''}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* SELECTED LED FLOATING INSPECTOR CARD */}
      {selectedLed && (
        <div className="led-inspector">
          <div className="inspector-head">
            <strong>LED #{selectedLed.global_id}</strong>
            <button className="icon-btn" onClick={() => setSelectedLed(null)} aria-label="Close inspector">✕</button>
          </div>
          <div className="inspector-meta">
            <span>{selectedLed.controller_name} · idx {selectedLed.led_index}</span>
            <span className={`tag ${selectedLed.source}`}>{selectedLed.source}</span>
          </div>
          <div className="mono-coords">
            X {selectedLed.x.toFixed(3)} · Y {selectedLed.y.toFixed(3)} · Z {selectedLed.z.toFixed(3)}
          </div>
          <div className="inspector-foot">
            <span>{selectedLed.views_count} views · {selectedLed.reproj_error_px}px err</span>
            <button className="mini-btn primary" onClick={() => handleSelectLed(selectedLed)}>Blink LED</button>
          </div>
        </div>
      )}

      {/* RIGHT SLIDE-OVER PRECISION DRAWER */}
      {drawer !== 'closed' && (
        <aside className="hud-drawer">
          <div className="drawer-tabs">
            {(['effects', 'scan', 'cameras', 'controllers'] as const).map((m, idx) => (
              <button key={m} className={drawer === m ? 'active' : ''} onClick={() => setDrawer(m)}>
                <kbd>{idx + 1}</kbd>{' '}
                {m === 'effects' ? '3D FX' : m === 'scan' ? 'Scan' : m === 'cameras' ? 'Cams' : 'WLED'}
              </button>
            ))}
            <button className="icon-btn" title="Collapse Panel" onClick={() => setDrawer('closed')}>⟩</button>
          </div>

          <div className="drawer-body">
            {drawer === 'effects' && (
              <>
                <div className="section-label">Spatial Shaders (UDP DDP :4048)</div>
                <div className="effect-grid">
                  {effects.map((e) => (
                    <button
                      key={e.id}
                      className={`fx-tile ${effState?.effect_id === e.id ? 'selected' : ''}`}
                      onClick={async () => {
                        const res = await apiPost('/api/effects/start', { effect_id: e.id, params: e.defaults })
                        setEffState(res.state)
                        setColorMode('live')
                      }}
                    >
                      <strong>{e.name}</strong>
                      <small>{e.description}</small>
                    </button>
                  ))}
                </div>

                {effState && (
                  <div className="card-block">
                    <div className="section-label">Live Parameters</div>
                    <label className="slider-row">
                      <span>Brightness</span>
                      <strong>{Math.round(effState.brightness * 100)}%</strong>
                      <input
                        type="range"
                        min={0.05}
                        max={1}
                        step={0.05}
                        value={effState.brightness}
                        onChange={async (ev) => {
                          const b = parseFloat(ev.target.value)
                          setEffState({ ...effState, brightness: b })
                          await apiPost('/api/effects/update', { brightness: b })
                        }}
                      />
                    </label>
                    <label className="slider-row">
                      <span>Speed</span>
                      <strong>{Number(effState.params.speed ?? 0.8).toFixed(1)}×</strong>
                      <input
                        type="range"
                        min={0.1}
                        max={3.0}
                        step={0.1}
                        value={Number(effState.params.speed ?? 0.8)}
                        onChange={async (ev) => {
                          const speed = parseFloat(ev.target.value)
                          const params = { ...effState.params, speed }
                          setEffState({ ...effState, params })
                          await apiPost('/api/effects/update', { params })
                        }}
                      />
                    </label>
                    {effState.effect_id === 'sweep_plane' && (
                      <label className="field-col">
                        <span>Sweep Axis</span>
                        <select
                          value={String(effState.params.axis ?? 'y')}
                          onChange={async (ev) => {
                            const params = { ...effState.params, axis: ev.target.value }
                            setEffState({ ...effState, params })
                            await apiPost('/api/effects/update', { params })
                          }}
                        >
                          <option value="x">X Axis (Left–Right)</option>
                          <option value="y">Y Axis (Vertical)</option>
                          <option value="z">Z Axis (Depth)</option>
                          <option value="diag">3D Diagonal</option>
                        </select>
                      </label>
                    )}
                  </div>
                )}

                <div className="card-block">
                  <div className="section-label">3D Viewport & Alignment</div>
                  <div className="seg-group">
                    {(['live', 'coverage', 'controller'] as const).map((m) => (
                      <button key={m} className={colorMode === m ? 'active' : ''} onClick={() => setColorMode(m)}>
                        {m}
                      </button>
                    ))}
                  </div>
                  <label className="check-row">
                    <input type="checkbox" checked={showCameras} onChange={(e) => setShowCameras(e.target.checked)} />
                    <span>Show Solved Camera Frustums</span>
                  </label>
                  <label className="slider-row">
                    <span>Metric Scale</span>
                    <strong>{scaleM.toFixed(2)} m</strong>
                    <input type="range" min={0.1} max={5} step={0.1} value={scaleM} onChange={(e) => setScaleM(parseFloat(e.target.value))} />
                  </label>
                  <div className="triple-num">
                    {(['Pitch', 'Yaw', 'Roll'] as const).map((lbl, axis) => (
                      <label key={lbl}>
                        <span>{lbl}°</span>
                        <input
                          type="number"
                          value={rotEuler[axis]}
                          onChange={(e) => {
                            const next: [number, number, number] = [...rotEuler] as [number, number, number]
                            next[axis] = parseFloat(e.target.value) || 0
                            setRotEuler(next)
                          }}
                        />
                      </label>
                    ))}
                  </div>
                  <div className="inline-btns">
                    <button
                      className="mini-btn primary"
                      onClick={async () => {
                        const res = await apiPost('/api/map/auto-level')
                        if (res.rotation_euler_deg) setRotEuler(res.rotation_euler_deg)
                        playLevelSnap(sfxRef.current)
                        notify('info', `Auto-leveled upright (${res.rotation_euler_deg?.join('°, ')}°).`)
                        await refreshAll()
                      }}
                    >
                      Auto-Level Vertical
                    </button>
                    <button
                      className="mini-btn"
                      onClick={async () => {
                        await apiPost('/api/map/solve', {
                          interpolate_occluded: true,
                          scale_meters_per_unit: scaleM,
                          rotation_euler_deg: rotEuler,
                        })
                        playLevelSnap(sfxRef.current)
                        notify('info', 'Updated 3D alignment.')
                        await refreshAll()
                      }}
                    >
                      Apply Alignment
                    </button>
                  </div>
                </div>
              </>
            )}

            {drawer === 'scan' && (
              <>
                <div className="card-block">
                  <div className="section-label">Ternary ECC Optical Scanner</div>
                  <label className="field-col">
                    <span>Targeting Strategy</span>
                    <select value={scanMode} onChange={(e) => setScanMode(e.target.value as typeof scanMode)}>
                      <option value="ternary_ecc">DC-Balanced Ternary ECC (~2s, Round 1)</option>
                      <option value="delta_ternary">Delta Scan (15% Anchors + Unmapped LEDs)</option>
                      <option value="sequential">Sequential 1-by-1 (Diagnostic)</option>
                    </select>
                  </label>
                  <label className="slider-row">
                    <span>LED Brightness</span>
                    <strong>{brightness}/255</strong>
                    <input type="range" min={40} max={255} value={brightness} onChange={(e) => setBrightness(parseInt(e.target.value, 10))} />
                  </label>
                  <label className="slider-row">
                    <span>Rolling-Shutter Settle</span>
                    <strong>{settleMs} ms</strong>
                    <input type="range" min={60} max={300} step={10} value={settleMs} onChange={(e) => setSettleMs(parseInt(e.target.value, 10))} />
                  </label>
                  <button className="primary full-btn" disabled={scanning} onClick={handleStartScan}>
                    {scanning ? 'Scanning Fleet...' : `Capture Round ${(project?.rounds.length ?? 0) + 1} (${enabledCams.length} Cams)`}
                  </button>
                  <div className="inline-btns">
                    <button
                      className={`mini-btn ${walkAround ? 'primary' : ''}`}
                      onClick={() => setWalkAround((v) => !v)}
                    >
                      {walkAround ? `Stop Walk-Around (${walkCountdown}s)` : `Walk-Around Loop (${walkIntervalSec}s)`}
                    </button>
                    {project && project.rounds.length > 0 && (
                      <button
                        className="mini-btn"
                        onClick={async () => {
                          const last = project.rounds[project.rounds.length - 1]
                          await fetch(`/api/rounds/${last.round_id}`, { method: 'DELETE' })
                          notify('info', `Removed ${last.label} and re-solved the 3D map.`)
                          await refreshAll()
                        }}
                      >
                        Undo Last
                      </button>
                    )}
                  </div>
                  <label className="slider-row">
                    <span>Walk-Around Pause</span>
                    <strong>{walkIntervalSec}s</strong>
                    <input type="range" min={5} max={20} value={walkIntervalSec} onChange={(e) => setWalkIntervalSec(parseInt(e.target.value, 10))} />
                  </label>
                </div>

                {map3d && map3d.components.length > 1 && (
                  <div className="warn-box">
                    <strong>{map3d.components.length} separate 3D zones:</strong> Capture one bridging round that sees at least 8 LEDs from both zones to stitch them together.
                  </div>
                )}

                <div className="section-label">Captured Rounds ({project?.rounds.length ?? 0})</div>
                <div className="stack-list">
                  {(!project || project.rounds.length === 0) ? (
                    <div className="empty-state">
                      <strong>No rounds captured yet</strong>
                      <span>Point at least two cameras at your lights from different angles, lock exposure, and press <kbd>S</kbd>.</span>
                    </div>
                  ) : (
                    project.rounds.map((r) => (
                      <div key={r.round_id} className="item-card">
                        <div>
                          <strong>{r.label}</strong> <small className="muted">({r.mode} · {r.duration_sec}s)</small>
                          <div className="muted tiny">
                            {r.views.map((v) => `${v.camera_name}: ${Object.keys(v.detections).length}`).join(' · ')}
                          </div>
                        </div>
                        <div className="inline-btns">
                          <button
                            className="mini-btn"
                            onClick={async () => {
                              await apiPost(`/api/rounds/${r.round_id}/toggle`, { enabled: !r.enabled })
                              await apiPost('/api/map/solve', { interpolate_occluded: true })
                              await refreshAll()
                            }}
                          >
                            {r.enabled ? 'On' : 'Off'}
                          </button>
                          <button
                            className="mini-btn"
                            onClick={async () => {
                              await fetch(`/api/rounds/${r.round_id}`, { method: 'DELETE' })
                              await refreshAll()
                            }}
                          >
                            ✕
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>
                <a className="btn-link full-btn" href="/api/map/export" download="ledmap_3d.json">
                  Export 3D Map JSON
                </a>
              </>
            )}

            {drawer === 'cameras' && (
              <>
                <div className="inline-btns">
                  <button className="mini-btn" onClick={() => apiPost('/api/cameras/refresh').then(refreshAll)}>Refresh Fleet</button>
                  <button className="mini-btn primary" onClick={handleLockAllCams}>Lock All AE</button>
                  <a className="mini-btn" href="/api/calibration/charuco.png" target="_blank" rel="noreferrer">ChArUco</a>
                  <button className="mini-btn primary" onClick={openQrModal}>Add Phone QR</button>
                </div>
                <div className="inline-add">
                  <input placeholder="http://<phone-or-mac>:8788" value={newCamUrl} onChange={(e) => setNewCamUrl(e.target.value)} />
                  <button
                    className="mini-btn"
                    onClick={async () => {
                      if (!newCamUrl.trim()) return
                      await apiPost('/api/cameras/add', { base_url: newCamUrl })
                      setNewCamUrl('')
                      await refreshAll()
                    }}
                  >
                    Add
                  </button>
                </div>
                <div className="stack-list">
                  {cameras.length === 0 ? (
                    <div className="empty-state">
                      <strong>No cameras on deck yet</strong>
                      <span>Click <b>Add Phone QR</b> to drop an Android phone straight into the fleet over Wi-Fi, or paste a remote Mac URL above.</span>
                    </div>
                  ) : (
                    cameras.map((cam) => (
                      <div key={cam.id} className={`cam-drawer-card ${cam.enabled ? '' : 'dim'}`}>
                        <div className="cam-row-top">
                          <div>
                            <strong>{cam.name}</strong>
                            <div className="muted tiny">
                              {cam.platform.toUpperCase()} · {cam.transport} · {cam.width}×{cam.height}
                              {cam.battery >= 0 ? ` · ${cam.battery}%` : ''}
                            </div>
                          </div>
                          <input
                            type="checkbox"
                            checked={cam.enabled}
                            onChange={async (e) => {
                              await apiPost(`/api/cameras/${encodeURIComponent(cam.id)}/enable`, { enabled: e.target.checked })
                              await refreshAll()
                            }}
                          />
                        </div>
                        {cam.config_locked_by_other_app && (
                          <div className="warn-chip">Locked by EMEET STUDIO—capture works; quit that app to lock exposure.</div>
                        )}
                        <img className="drawer-preview" src={`/api/cameras/${encodeURIComponent(cam.id)}/preview`} alt={cam.name} />
                        <div className="inline-btns">
                          <button className="mini-btn" onClick={() => apiPost(`/api/cameras/${encodeURIComponent(cam.id)}/config`, { led_preset: true, ae_lock: true, awb_lock: true, af_lock: true })}>LED Lock</button>
                          <button className="mini-btn" onClick={() => apiPost(`/api/cameras/${encodeURIComponent(cam.id)}/config`, { auto_preset: true, ae_lock: false, awb_lock: false, af_lock: false })}>Auto</button>
                          <button
                            className="mini-btn"
                            onClick={async () => {
                              notify('info', `Hold the ChArUco board in front of ${cam.name}...`)
                              const res = await apiPost(`/api/cameras/${encodeURIComponent(cam.id)}/calibrate`, { num_frames: 8, delay_ms: 850 })
                              playLevelSnap(sfxRef.current)
                              notify('celebrate', `Calibrated ${cam.name}—RMS ${res.rms_px}px.`)
                              await refreshAll()
                            }}
                          >
                            Calibrate
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </>
            )}

            {drawer === 'controllers' && (
              <>
                <div className="inline-btns">
                  <button className="mini-btn" onClick={() => apiPost('/api/controllers/refresh').then(refreshAll)}>Refresh mDNS</button>
                  <button className="mini-btn primary" onClick={() => apiPost('/api/controllers/select-all', { selected: true }).then(refreshAll)}>Select All WLEDs</button>
                  <button className="mini-btn" onClick={() => apiPost('/api/controllers/select-all', { selected: false }).then(refreshAll)}>Clear</button>
                </div>
                <div className="inline-add">
                  <input placeholder="Add WLED IP (e.g. 192.168.1.50)" value={newWledIp} onChange={(e) => setNewWledIp(e.target.value)} />
                  <button
                    className="mini-btn"
                    onClick={async () => {
                      if (!newWledIp.trim()) return
                      await apiPost('/api/controllers/add', { ip: newWledIp })
                      setNewWledIp('')
                      await refreshAll()
                    }}
                  >
                    Add
                  </button>
                </div>
                <div className="stack-list">
                  {controllers.length === 0 ? (
                    <div className="empty-state">
                      <strong>No WLED controllers found yet</strong>
                      <span>Make sure your ESP32s are on the same network, or paste a controller IP above to add it manually.</span>
                    </div>
                  ) : (
                    controllers.map((c) => (
                      <div key={c.id} className="item-card">
                        <label className="check-row">
                          <input
                            type="checkbox"
                            checked={c.selected}
                            onChange={async (e) => {
                              await apiPost(`/api/controllers/${encodeURIComponent(c.id)}/select`, { selected: e.target.checked })
                              await refreshAll()
                            }}
                          />
                          <div>
                            <strong>{c.name}</strong>
                            <div className="muted tiny">{c.ip} · {c.led_count} LEDs · v{c.version}</div>
                          </div>
                        </label>
                        <button className="mini-btn" onClick={() => apiPost(`/api/controllers/${encodeURIComponent(c.id)}/identify`)}>
                          Blink
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </>
            )}
          </div>

          <div className="drawer-footer-note">
            <span>Run Good · Look Good · Feel Good</span>
            <a href="https://www.dotfun.co" target="_blank" rel="noreferrer">dotfun.co</a>
          </div>
        </aside>
      )}

      {/* BOTTOM FLOATING FIELD DOCK */}
      <div className="floating-dock">
        <button className={`dock-btn ${effState?.running ? 'live' : ''}`} onClick={togglePlay} title="Space">
          <span>{effState?.running ? '■ Stop DDP' : '▶ Play DDP'}</span>
          <kbd>Space</kbd>
        </button>
        <button className="dock-btn primary" disabled={scanning} onClick={handleStartScan} title="S">
          <span>{scanning ? 'Scanning...' : 'Scan Round'}</span>
          <kbd>S</kbd>
        </button>
        <button className="dock-btn" onClick={handlePaintUnmapped} title="P">
          <span>Paint Unmapped</span>
          <kbd>P</kbd>
        </button>
        <button className="dock-btn" onClick={handleLockAllCams} title="L">
          <span>Lock Cams</span>
          <kbd>L</kbd>
        </button>
        <button className={`dock-btn ${showPip ? 'active' : ''}`} onClick={() => setShowPip((v) => !v)}>
          <span>PIP ({enabledCams.length})</span>
        </button>
        <button className="dock-btn" onClick={openQrModal}>
          <span>Add Phone</span>
        </button>
        <button className={`dock-btn ${drawer !== 'closed' ? 'active' : ''}`} onClick={() => setDrawer(drawer === 'closed' ? 'effects' : 'closed')}>
          <span>Inspector</span>
        </button>
      </div>

      {/* PHONE APK INSTALL QR MODAL */}
      {qrOpen && (
        <div className="cmd-backdrop" onClick={() => setQrOpen(false)}>
          <div className="qr-modal" onClick={(e) => e.stopPropagation()}>
            <div className="qr-head">
              <div>
                <div style={{ marginBottom: 6 }}><DotfunLogo height={18} /></div>
                <strong>Add an Android Phone to the Fleet</strong>
                <div className="muted tiny">Point any phone camera at a QR code below, install the APK, and open it—it joins automatically.</div>
              </div>
              <button className="icon-btn" onClick={() => setQrOpen(false)} aria-label="Close">✕</button>
            </div>
            <div className="qr-grid">
              {installUrls.length === 0 ? (
                <div className="qr-card" style={{ gridColumn: '1 / -1' }}>
                  <div className="muted">{qrLoading ? 'Loading QR codes...' : 'Studio host did not respond yet.'}</div>
                  <button className="mini-btn primary" onClick={loadInstallUrls}>Retry</button>
                </div>
              ) : (
                installUrls.map((item) => (
                  <div key={item.url} className="qr-card">
                    <div className="section-label">{item.label}</div>
                    <img
                      className="qr-img"
                      src={`/api/install-qr.png?url=${encodeURIComponent(item.url)}`}
                      alt={item.label}
                    />
                    <a className="qr-url" href={item.url} target="_blank" rel="noreferrer">{item.url}</a>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}

      {/* ⌘K COMMAND PALETTE */}
      {cmdOpen && (
        <div className="cmd-backdrop" onClick={() => setCmdOpen(false)}>
          <div className="cmd-modal" onClick={(e) => e.stopPropagation()}>
            <input
              ref={cmdInputRef}
              className="cmd-input"
              placeholder="Jump to a field action, 3D shader, or WLED controller..."
              value={cmdQuery}
              onChange={(e) => setCmdQuery(e.target.value)}
            />
            <div className="cmd-list">
              {filteredCmds.map((c) => (
                <button
                  key={c.id}
                  className="cmd-item"
                  onClick={async () => {
                    setCmdOpen(false)
                    await c.action()
                  }}
                >
                  <div>
                    <span className="cmd-group">{c.group}</span>
                    <span>{c.label}</span>
                  </div>
                  {c.hint && <kbd>{c.hint}</kbd>}
                </button>
              ))}
              {filteredCmds.length === 0 && <div className="muted" style={{ padding: 14 }}>Nothing matched that.</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
