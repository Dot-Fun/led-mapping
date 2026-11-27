use rayon::prelude::*;
use serde::Deserialize;
use std::collections::HashMap;
use std::env;
use std::fs;
use std::net::UdpSocket;
use std::thread;
use std::time::{Duration, Instant};

const DDP_PORT: u16 = 4048;
const DDP_MAX_LEDS_PER_PACKET: usize = 480;
const DDP_VER1: u8 = 0x40;
const DDP_PUSH: u8 = 0x01;
const DDP_TYPE_RGB: u8 = 0x01;

#[derive(Debug, Deserialize)]
struct Controller {
    id: String,
    ip: String,
    led_count: usize,
    #[serde(default = "default_true")]
    online: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Deserialize)]
struct MappedLed {
    global_id: usize,
    controller_id: String,
    led_index: usize,
    x: f32,
    y: f32,
    z: f32,
    source: String,
}

#[derive(Debug, Deserialize)]
struct Map3D {
    leds: Vec<MappedLed>,
}

#[derive(Debug, Deserialize)]
struct ProjectFile {
    controllers: Vec<Controller>,
    map_3d: Option<Map3D>,
}

struct PreparedController {
    addr: String,
    rgb_buf: Vec<u8>,
    packets: Vec<(usize, usize, Vec<u8>)>,
}

fn hsv_to_rgb(h: f32, s: f32, v: f32) -> [u8; 3] {
    let h = h.rem_euclid(1.0);
    let i = (h * 6.0).floor() as i32;
    let f = h * 6.0 - i as f32;
    let p = v * (1.0 - s);
    let q = v * (1.0 - f * s);
    let t = v * (1.0 - (1.0 - f) * s);
    let (r, g, b) = match i.rem_euclid(6) {
        0 => (v, t, p),
        1 => (q, v, p),
        2 => (p, v, t),
        3 => (p, q, v),
        4 => (t, p, v),
        _ => (v, p, q),
    };
    [
        (r.clamp(0.0, 1.0) * 255.0) as u8,
        (g.clamp(0.0, 1.0) * 255.0) as u8,
        (b.clamp(0.0, 1.0) * 255.0) as u8,
    ]
}

fn eval_effect(effect: &str, p: [f32; 3], t: f32, speed: f32) -> [u8; 3] {
    match effect {
        "expanding_sphere" => {
            let r = (p[0] * p[0] + p[1] * p[1] + p[2] * p[2]).sqrt();
            let wave = 0.5 + 0.5 * (r * 2.4 * std::f32::consts::PI - t * speed * 2.0 * std::f32::consts::PI).sin();
            hsv_to_rgb(r * 0.35 - t * speed * 0.2, 0.92, wave.powf(2.2))
        }
        "plasma_3d" => {
            let tt = t * speed;
            let r = (p[0] * p[0] + p[1] * p[1] + p[2] * p[2]).sqrt();
            let field = ((p[0] * 2.4 + tt).sin()
                + (p[1] * 2.6 - tt * 1.3).sin()
                + (p[2] * 2.2 + tt * 0.7).sin()
                + (r * 2.9 - tt * 1.1).sin())
                * 0.25;
            let val = (0.35 + 0.65 * (0.5 + 0.5 * (field * std::f32::consts::PI).cos())).clamp(0.0, 1.0);
            hsv_to_rgb(0.5 + 0.5 * field + tt * 0.06, 0.9, val)
        }
        "helix_vortex" => {
            let angle = p[2].atan2(p[0]) / (2.0 * std::f32::consts::PI);
            hsv_to_rgb(angle + p[1] * 0.7 - t * speed * 0.35, 0.95, 0.95)
        }
        "axis_test" => [
            (((p[0] + 1.0) * 0.5).clamp(0.0, 1.0) * 255.0) as u8,
            (((p[1] + 1.0) * 0.5).clamp(0.0, 1.0) * 255.0) as u8,
            (((p[2] + 1.0) * 0.5).clamp(0.0, 1.0) * 255.0) as u8,
        ],
        _ => {
            let pos = (t * speed * std::f32::consts::PI).sin();
            let d = (p[1] - pos).abs() / 0.22;
            let val = (-d * d).exp();
            hsv_to_rgb(0.55 + 0.25 * (p[1] + 1.0) * 0.5 + t * 0.05, 0.85, val)
        }
    }
}

fn prepare_controllers(controllers: &[Controller]) -> (Vec<PreparedController>, HashMap<String, usize>) {
    let max_bytes = DDP_MAX_LEDS_PER_PACKET * 3;
    let mut prepared = Vec::new();
    let mut id_to_slot = HashMap::new();

    for c in controllers {
        if !c.online || c.led_count == 0 {
            continue;
        }
        let total = c.led_count * 3;
        let mut packets = Vec::new();
        let mut offset = 0usize;
        while offset < total {
            let chunk_len = max_bytes.min(total - offset);
            let is_last = offset + chunk_len >= total;
            let flags = DDP_VER1 | if is_last { DDP_PUSH } else { 0 };
            let mut pkt = vec![0u8; 10 + chunk_len];
            pkt[0] = flags;
            pkt[1] = 1;
            pkt[2] = DDP_TYPE_RGB;
            pkt[3] = 1;
            pkt[4..8].copy_from_slice(&(offset as u32).to_be_bytes());
            pkt[8..10].copy_from_slice(&(chunk_len as u16).to_be_bytes());
            packets.push((offset, chunk_len, pkt));
            offset += chunk_len;
        }
        let slot = prepared.len();
        id_to_slot.insert(c.id.clone(), slot);
        prepared.push(PreparedController {
            addr: format!("{}:{DDP_PORT}", c.ip),
            rgb_buf: vec![0u8; total],
            packets,
        });
    }
    (prepared, id_to_slot)
}

fn run_play(args: &[String]) {
    let project_path = args.get(2).expect("Usage: ledmap-rt play <project.json> [effect] [fps] [brightness]");
    let effect = args.get(3).map(|s| s.as_str()).unwrap_or("sweep_plane");
    let fps: f32 = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(60.0);
    let brightness: f32 = args.get(5).and_then(|s| s.parse().ok()).unwrap_or(0.8);

    let raw = fs::read_to_string(project_path).expect("Failed to read project.json");
    let proj: ProjectFile = serde_json::from_str(&raw).expect("Invalid project.json");
    let map = proj.map_3d.expect("Project has no solved map_3d yet");

    let (mut prepared, id_to_slot) = prepare_controllers(&proj.controllers);
    let max_chunks = prepared.iter().map(|p| p.packets.len()).max().unwrap_or(0);

    // Normalize bounding box to [-1, 1]
    let mapped: Vec<&MappedLed> = map.leds.iter().filter(|l| l.source != "unmapped").collect();
    let (mut lo, mut hi) = ([f32::MAX; 3], [f32::MIN; 3]);
    for l in &mapped {
        let coords = [l.x, l.y, l.z];
        for k in 0..3 {
            lo[k] = lo[k].min(coords[k]);
            hi[k] = hi[k].max(coords[k]);
        }
    }
    let center = [(lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5, (lo[2] + hi[2]) * 0.5];
    let span = ((hi[0] - lo[0]).max(hi[1] - lo[1]).max(hi[2] - lo[2])).max(1e-5) * 0.5;

    let norm_pts: Vec<(usize, usize, [f32; 3])> = mapped
        .iter()
        .filter_map(|l| {
            let slot = *id_to_slot.get(&l.controller_id)?;
            Some((
                slot,
                l.led_index * 3,
                [(l.x - center[0]) / span, (l.y - center[1]) / span, (l.z - center[2]) / span],
            ))
        })
        .collect();

    let sock = UdpSocket::bind("0.0.0.0:0").expect("UDP bind failed");
    let period = Duration::from_secs_f64(1.0 / (fps.max(1.0) as f64));
    let start = Instant::now();
    let mut next_t = start;
    let mut seq: u8 = 1;
    let mut colors: Vec<[u8; 3]> = vec![[0, 0, 0]; norm_pts.len()];

    println!(
        "ledmap-rt streaming '{}' to {} controllers ({} mapped LEDs) @ {:.0} FPS (zero-alloc deadline pacing)...",
        effect,
        prepared.len(),
        norm_pts.len(),
        fps
    );

    loop {
        let t = start.elapsed().as_secs_f32();

        colors
            .par_iter_mut()
            .zip(norm_pts.par_iter())
            .for_each(|(out, (_, _, p))| {
                let c = eval_effect(effect, *p, t, 0.9);
                *out = [
                    (c[0] as f32 * brightness) as u8,
                    (c[1] as f32 * brightness) as u8,
                    (c[2] as f32 * brightness) as u8,
                ];
            });

        for pc in &mut prepared {
            pc.rgb_buf.fill(0);
        }
        for ((slot, byte_off, _), rgb) in norm_pts.iter().zip(colors.iter()) {
            let buf = &mut prepared[*slot].rgb_buf;
            if *byte_off + 2 < buf.len() {
                buf[*byte_off] = rgb[0];
                buf[*byte_off + 1] = rgb[1];
                buf[*byte_off + 2] = rgb[2];
            }
        }

        for pc in &mut prepared {
            for (offset, chunk_len, pkt) in &mut pc.packets {
                pkt[1] = seq;
                pkt[10..10 + *chunk_len].copy_from_slice(&pc.rgb_buf[*offset..*offset + *chunk_len]);
            }
        }

        // Interleave chunks across controllers
        for chunk_i in 0..max_chunks {
            for pc in &prepared {
                if let Some((_, _, pkt)) = pc.packets.get(chunk_i) {
                    let _ = sock.send_to(pkt, &pc.addr);
                }
            }
        }
        seq = (seq % 15) + 1;

        // Monotonic deadline pacing with sub-millisecond spin finish
        next_t += period;
        let now = Instant::now();
        if next_t + period < now {
            next_t = now + period;
        }
        if let Some(rem) = next_t.checked_duration_since(now) {
            if rem > Duration::from_micros(1500) {
                thread::sleep(rem - Duration::from_micros(1000));
            }
            while Instant::now() < next_t {
                std::hint::spin_loop();
            }
        }
    }
}

fn run_bench() {
    let n_leds = 2000usize;
    let n_frames = 10usize;
    let n_pixels = 50_000usize;

    let pts: Vec<[f32; 3]> = (0..n_leds)
        .map(|i| {
            let a = i as f32 * 0.1;
            [a.cos(), (i as f32 / n_leds as f32) * 2.0 - 1.0, a.sin()]
        })
        .collect();
    let t0 = Instant::now();
    let mut checksum: u64 = 0;
    for f in 0..1000 {
        let t = f as f32 * 0.016;
        let out: Vec<[u8; 3]> = pts.par_iter().map(|&p| eval_effect("plasma_3d", p, t, 1.0)).collect();
        checksum += out[0][0] as u64;
    }
    let dt_eff = t0.elapsed();

    let code_mat: Vec<f32> = (0..n_leds * n_frames * 3)
        .map(|i| if i % 3 == 0 { 1.0 } else { -0.5 })
        .collect();
    let pix_traces: Vec<f32> = (0..n_pixels * n_frames * 3)
        .map(|i| ((i % 7) as f32) / 6.0)
        .collect();
    let stride = n_frames * 3;
    let t1 = Instant::now();
    let decoded: Vec<i32> = (0..n_pixels)
        .into_par_iter()
        .map(|p| {
            let pv = &pix_traces[p * stride..(p + 1) * stride];
            let mut best_id = 0i32;
            let mut best_sc = f32::NEG_INFINITY;
            for c in 0..n_leds {
                let cv = &code_mat[c * stride..(c + 1) * stride];
                let mut dot = 0.0f32;
                for k in 0..stride {
                    dot += pv[k] * cv[k];
                }
                if dot > best_sc {
                    best_sc = dot;
                    best_id = c as i32;
                }
            }
            best_id
        })
        .collect();
    let dt_dec = t1.elapsed();
    println!(
        "Bench OK | 3D plasma (2000 LEDs): {:.3} ms/frame (chk={}) | Rayon ternary decode (50k px x 2000 LEDs): {:.1} ms (sample={})",
        dt_eff.as_secs_f64() * 1000.0 / 1000.0,
        checksum,
        dt_dec.as_secs_f64() * 1000.0,
        decoded[0]
    );
}

fn main() {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("play") => run_play(&args),
        Some("bench") => run_bench(),
        _ => {
            println!("ledmap-rt — High-Speed Rust 3D WLED DDP Engine");
            println!("Usage:");
            println!("  ledmap-rt play <projects/<name>/project.json> [effect] [fps] [brightness]");
            println!("  ledmap-rt bench");
        }
    }
}
