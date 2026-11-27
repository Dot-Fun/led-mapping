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

const CTRL_PALETTE = [
  0x38bdf8, 0xa78bfa, 0x34d399, 0xfbbf24, 0xf87171, 0xf472b6, 0x2dd4bf, 0xfb923c, 0x818cf8,
]

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
  const visibleLedsRef = useRef<MappedLED3D[]>([])
  const sceneRef = useRef<THREE.Scene | null>(null)
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)

  useEffect(() => {
    const el = mountRef.current
    if (!el) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x070a10)
    sceneRef.current = scene

    const camera = new THREE.PerspectiveCamera(55, el.clientWidth / Math.max(1, el.clientHeight), 0.01, 200)
    camera.position.set(2.5, 1.8, 3.2)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(el.clientWidth, el.clientHeight)
    el.appendChild(renderer.domElement)
    rendererRef.current = renderer

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08

    const grid = new THREE.GridHelper(6, 24, 0x1e293b, 0x111827)
    grid.position.y = -1.2
    scene.add(grid)

    const axes = new THREE.AxesHelper(0.6)
    scene.add(axes)

    const camGroup = new THREE.Group()
    scene.add(camGroup)
    camGroupRef.current = camGroup

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
    const animate = () => {
      raf = requestAnimationFrame(animate)
      controls.update()
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
      colors[idx * 3] = 0.2
      colors[idx * 3 + 1] = 0.85
      colors[idx * 3 + 2] = 1.0
    })

    const geom = new THREE.BufferGeometry()
    geom.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    geom.setAttribute('color', new THREE.BufferAttribute(colors, 3))

    const mat = new THREE.PointsMaterial({
      size: 0.07,
      vertexColors: true,
      sizeAttenuation: true,
    })
    const pts = new THREE.Points(geom, mat)
    scene.add(pts)
    pointsRef.current = pts

    // Draw solved camera frustums
    if (showCameras) {
      for (const cam of map3d.cameras) {
        const origin = new THREE.Vector3(...cam.position)
        const dir = new THREE.Vector3(...cam.look_dir).normalize()
        const arrow = new THREE.ArrowHelper(dir, origin, 0.35, 0xf59e0b, 0.09, 0.06)
        camGroup.add(arrow)
        const dot = new THREE.Mesh(
          new THREE.SphereGeometry(0.04, 12, 12),
          new THREE.MeshBasicMaterial({ color: 0xf59e0b })
        )
        dot.position.copy(origin)
        camGroup.add(dot)
      }
    }
  }, [map3d, showCameras])

  // Update LED colors smoothly on liveRgb or mode change
  useEffect(() => {
    const pts = pointsRef.current
    if (!pts || !map3d) return
    const colAttr = pts.geometry.getAttribute('color') as THREE.BufferAttribute
    const mapped = visibleLedsRef.current
    const ctrlIndex = new Map<string, number>()
    let nextIdx = 0
    const tmpColor = new THREE.Color()

    for (let i = 0; i < mapped.length; i++) {
      const led = mapped[i]
      if (selectedLedId === led.global_id) {
        colAttr.setXYZ(i, 1, 1, 1)
        continue
      }
      if (colorMode === 'live' && liveRgb && led.global_id * 3 + 2 < liveRgb.length) {
        const r = liveRgb[led.global_id * 3] / 255
        const g = liveRgb[led.global_id * 3 + 1] / 255
        const b = liveRgb[led.global_id * 3 + 2] / 255
        // Keep dark LEDs slightly visible as faint slate dots
        colAttr.setXYZ(i, Math.max(r, 0.08), Math.max(g, 0.09), Math.max(b, 0.12))
      } else if (colorMode === 'coverage') {
        if (led.source === 'measured') {
          colAttr.setXYZ(i, 0.15, 0.88, 0.45)
        } else {
          colAttr.setXYZ(i, 0.25, 0.55, 1.0)
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
    colAttr.needsUpdate = true
  }, [liveRgb, map3d, colorMode, selectedLedId])

  return <div ref={mountRef} style={{ width: '100%', height: '100%', minHeight: 480 }} />
}
