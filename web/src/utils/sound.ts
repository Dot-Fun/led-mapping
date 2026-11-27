// Subtle, muteable Web Audio tactile cues (zero external assets, works offline).

let ctx: AudioContext | null = null

function getCtx(): AudioContext | null {
  if (typeof window === 'undefined') return null
  if (!ctx) {
    const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AudioCtx) return null
    ctx = new AudioCtx()
  }
  if (ctx.state === 'suspended') {
    ctx.resume().catch(() => {})
  }
  return ctx
}

function tone(freq: number, startOffset: number, duration: number, peakGain: number, type: OscillatorType = 'sine') {
  const c = getCtx()
  if (!c) return
  const now = c.currentTime + startOffset
  const osc = c.createOscillator()
  const gain = c.createGain()
  osc.type = type
  osc.frequency.setValueAtTime(freq, now)
  gain.gain.setValueAtTime(0.0001, now)
  gain.gain.exponentialRampToValueAtTime(peakGain, now + 0.012)
  gain.gain.exponentialRampToValueAtTime(0.0001, now + duration)
  osc.connect(gain)
  gain.connect(c.destination)
  osc.start(now)
  osc.stop(now + duration + 0.02)
}

export function playLedPing(enabled: boolean) {
  if (!enabled) return
  tone(880, 0, 0.11, 0.045, 'triangle')
  tone(1318.5, 0.03, 0.12, 0.03, 'sine')
}

export function playScanComplete(enabled: boolean) {
  if (!enabled) return
  tone(523.25, 0, 0.16, 0.05, 'triangle')
  tone(659.25, 0.08, 0.18, 0.05, 'triangle')
  tone(783.99, 0.16, 0.28, 0.055, 'sine')
}

export function playLevelSnap(enabled: boolean) {
  if (!enabled) return
  tone(440, 0, 0.09, 0.04, 'sine')
  tone(659.25, 0.06, 0.14, 0.045, 'sine')
}

export function playEasterEgg(enabled: boolean) {
  if (!enabled) return
  tone(523.25, 0, 0.12, 0.045, 'triangle')
  tone(659.25, 0.065, 0.12, 0.045, 'triangle')
  tone(783.99, 0.13, 0.14, 0.05, 'triangle')
  tone(1046.5, 0.2, 0.3, 0.055, 'sine')
}
