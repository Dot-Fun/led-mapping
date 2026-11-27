import React, { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import type { Map3DResult, MappedLED3D } from '../types'

interface Props {
  map3d: Map3DResult | null
  liveRgb: Uint8Array | null
  selectedLedId: number | null
  onSelectLed: (led: MappedLED3D) => void
  showCameras: boolean
  colorMode: 'live' | 'coverage' | 'controller'
}

// Warm-forward high-contrast palette anchored on dotfun vermilion & gold
const CTRL_PALETTE = [
  0xff3c00, // dotfun vermilion
  0xe9cf77, // dotfun gold
  0x34d399, // emerald
  0xfb7185, // coral rose
  0x38bdf8, // sky
  0xf59e0b, // amber
  0xa78bfa, // violet
  0x2dd4bf, // teal
  0xfffbed, // warm cream
]

function createLedSpriteTexture(): THREE.CanvasTexture {
  const size = 64
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')!
  const cx = size / 2
  const grad = ctx.createRadialGradient(cx, cx, 0, cx, cx, cx)
  grad.addColorStop(0.0, 'rgba(255, 255, 255, 1.0)')
  grad.addColorStop(0.45, 'rgba(255, 255, 255, 0.98)')
  grad.addColorStop(0.66, 'rgba(255, 255, 255, 0.36)')
  grad.addColorStop(0.88, 'rgba(255, 255, 255, 0.06)')
  grad.addColorStop(1.0, 'rgba(255, 255, 255, 0.0)')
  ctx.fillStyle = grad
  ctx.fillRect(0, 0, size, size)
  const tex = new THREE.CanvasTexture(canvas)
  tex.needsUpdate = true
  return tex
}

export const Viewport3D: React.FC<Props> = ({
  map3d,
  liveRgb,
  selectedLedId,
  onSelectLed,
  showCameras,
  colorMode,
}) => {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const pointsRef = useRef<THREE.Points | null>(null)
  const camGroupRef = useRef<THREE.Group | null>(null)
  const selectHaloRef = useRef<THREE.Mesh | null>(null)
  const visibleLedsRef = useRef<MappedLED3D[]>([])
  const sceneRef = useRef<THREE.Scene | null>(null)
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const spriteTexRef = useRef<THREE.CanvasTexture | null>(null)
  const selectedIdRef = useRef<number | null>(null)

  useEffect(() => {
    selectedIdRef.current = selectedLedId
  }, [selectedLedId])

  useEffect(() => {
    const el = mountRef.current
    if (!el) return

    const scene = new THREE.Scene()
    // Warm dotfun obsidian canvas (tinted toward #100f0f, never sterile blue-black)
    scene.background = new THREE.Color(0x0d0b0a)
    sceneRef.current = scene

    const camera = new THREE.PerspectiveCamera(55, el.clientWidth / Math.max(1, el.clientHeight), 0.01, 200)
    camera.position.set(2.5, 1.8, 3.2)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(el.clientWidth, el.clientHeight)
    el.appendChild(renderer.domElement)
    rendererRef.current = renderer

    const spriteTex = createLedSpriteTexture()
    spriteTexRef.current = spriteTex

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08

    const grid = new THREE.GridHelper(6, 24, 0x2f2621, 0x1a1513)
    grid.position.y = -1.2
    scene.add(grid)

    const axes = new THREE.AxesHelper(0.55)
    scene.add(axes)

    const camGroup = new THREE.Group()
    scene.add(camGroup)
    camGroupRef.current = camGroup

    // Pulsing vermilion selection halo around the active LED
    const haloGeo = new THREE.RingGeometry(0.032, 0.048, 28)
    const haloMat = new THREE.MeshBasicMaterial({
      color: 0xff3c00,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.9,
      depthTest: false,
    })
    const halo = new THREE.Mesh(haloGeo, haloMat)
    halo.visible = false
    scene.add(halo)
    selectHaloRef.current = halo

    const raycaster = new THREE.Raycaster()
    raycaster.params.Points.threshold = 0.08
    const pointer = new THREE.Vector2()

    const handleClick = (ev: MouseEvent) => {
      if (!pointsRef.current || !cameraRef.current) return
      const rect = renderer.domElement.getBoundingClientRect()
      pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1
      pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1
      raycaster.setFromCamera(pointer, cameraRef.current)
      const hits = raycaster.intersectObject(pointsRef.current)
      if (hits.length > 0 && hits[0].index !== undefined) {
        const led = visibleLedsRef.current[hits[0].index]
        if (led) onSelectLed(led)
      }
    }
    renderer.domElement.addEventListener('click', handleClick)

    let raf = 0
    const clock = new THREE.Clock()
    const animate = () => {
      raf = requestAnimationFrame(animate)
      controls.update()
      if (selectHaloRef.current && selectHaloRef.current.visible && cameraRef.current) {
        const t = clock.getElapsedTime()
        const pulse = 1.0 + 0.28 * Math.sin(t * 6.5)
        selectHaloRef.current.scale.setScalar(pulse)
        selectHaloRef.current.quaternion.copy(cameraRef.current.quaternion)
      }
      renderer.render(scene, camera)
    }
    animate()

    const ro = new ResizeObserver(() => {
      if (!el) return
      const w = el.clientWidth
      const h = Math.max(1, el.clientHeight)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
    })
    ro.observe(el)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      renderer.domElement.removeEventListener('click', handleClick)
      spriteTex.dispose()
      haloGeo.dispose()
      haloMat.dispose()
      controls.dispose()
      renderer.dispose()
      el.removeChild(renderer.domElement)
    }
  }, [onSelectLed])

  // Rebuild geometry when map3d changes
  useEffect(() => {
    const scene = sceneRef.current
    const camGroup = camGroupRef.current
    if (!scene || !camGroup) return

    if (pointsRef.current) {
      scene.remove(pointsRef.current)
      pointsRef.current.geometry.dispose()
      ;(pointsRef.current.material as THREE.Material).dispose()
      pointsRef.current = null
    }
    while (camGroup.children.length > 0) {
      const ch = camGroup.children.pop()!
      camGroup.remove(ch)
    }

    if (!map3d) return

    const mapped = map3d.leds.filter((l) => l.source !== 'unmapped')
    visibleLedsRef.current = mapped

    const positions = new Float32Array(mapped.length * 3)
    const colors = new Float32Array(mapped.length * 3)
    mapped.forEach((led, idx) => {
      positions[idx * 3] = led.x
      positions[idx * 3 + 1] = led.y
      positions[idx * 3 + 2] = led.z
      colors[idx * 3] = 1.0
      colors[idx * 3 + 1] = 0.82
      colors[idx * 3 + 2] = 0.45
    })

    const geom = new THREE.BufferGeometry()
    geom.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    geom.setAttribute('color', new THREE.BufferAttribute(colors, 3))

    const mat = new THREE.PointsMaterial({
      size: 0.045,
      map: spriteTexRef.current ?? undefined,
      vertexColors: true,
      sizeAttenuation: true,
      transparent: true,
      alphaTest: 0.01,
      depthWrite: false,
    })
    const pts = new THREE.Points(geom, mat)
    scene.add(pts)
    pointsRef.current = pts

    // Draw solved camera frustums in dotfun vermilion + gold
    if (showCameras) {
      for (const cam of map3d.cameras) {
        const origin = new THREE.Vector3(...cam.position)
        const dir = new THREE.Vector3(...cam.look_dir).normalize()
        const arrow = new THREE.ArrowHelper(dir, origin, 0.32, 0xff3c00, 0.08, 0.05)
        camGroup.add(arrow)
        const dot = new THREE.Mesh(
          new THREE.SphereGeometry(0.022, 14, 14),
          new THREE.MeshBasicMaterial({ color: 0xe9cf77 })
        )
        dot.position.copy(origin)
        camGroup.add(dot)
      }
    }
  }, [map3d, showCameras])

  // Update LED colors smoothly on liveRgb or mode change + position selection halo
  useEffect(() => {
    const pts = pointsRef.current
    const halo = selectHaloRef.current
    if (!pts || !map3d) {
      if (halo) halo.visible = false
      return
    }
    const colAttr = pts.geometry.getAttribute('color') as THREE.BufferAttribute
    const mapped = visibleLedsRef.current
    const ctrlIndex = new Map<string, number>()
    let nextIdx = 0
    const tmpColor = new THREE.Color()
    let foundSelected = false

    for (let i = 0; i < mapped.length; i++) {
      const led = mapped[i]
      if (selectedLedId === led.global_id) {
        colAttr.setXYZ(i, 1.0, 0.98, 0.93)
        if (halo) {
          halo.position.set(led.x, led.y, led.z)
          halo.visible = true
        }
        foundSelected = true
        continue
      }
      if (colorMode === 'live' && liveRgb && led.global_id * 3 + 2 < liveRgb.length) {
        const r = liveRgb[led.global_id * 3] / 255
        const g = liveRgb[led.global_id * 3 + 1] / 255
        const b = liveRgb[led.global_id * 3 + 2] / 255
        // Keep unlit LEDs subtly visible as warm embers in the dark stage
        colAttr.setXYZ(i, Math.max(r, 0.14), Math.max(g, 0.11), Math.max(b, 0.09))
      } else if (colorMode === 'coverage') {
        if (led.source === 'measured') {
          colAttr.setXYZ(i, 0.18, 0.9, 0.48)
        } else {
          colAttr.setXYZ(i, 0.91, 0.81, 0.47) // dotfun gold for interpolated
        }
      } else {
        if (!ctrlIndex.has(led.controller_id)) {
          ctrlIndex.set(led.controller_id, nextIdx++)
        }
        const hex = CTRL_PALETTE[ctrlIndex.get(led.controller_id)! % CTRL_PALETTE.length]
        tmpColor.setHex(hex)
        colAttr.setXYZ(i, tmpColor.r, tmpColor.g, tmpColor.b)
      }
    }
    if (halo && !foundSelected) {
      halo.visible = false
    }
    colAttr.needsUpdate = true
  }, [liveRgb, map3d, colorMode, selectedLedId])

  return <div ref={mountRef} style={{ width: '100%', height: '100%', minHeight: 480 }} />
}
