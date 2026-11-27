"""Tests for DDP packet encoding, vectorized scatter, real-time thread, 2D ledmap export, and 3D effects."""
from __future__ import annotations

import asyncio
import struct
import time

import numpy as np
import pytest

from ledmap.engine.effects import BUILTIN_EFFECTS
from ledmap.engine.runner import EffectEngine
from ledmap.models import LEDDescriptor, Map3DResult, MappedLED3D, ProjectData, WLEDController
from ledmap.wled.ddp import DDP_PUSH, DDP_VER1, DDPStreamer, build_ddp_packets
from ledmap.wled.optimize import build_wled_2d_ledmap, evaluate_wled_issues


def test_ddp_multi_packet_splitting_and_push_flag():
    # 1,000 LEDs -> 480 + 480 + 40 = 3 packets
    rgb = np.full((1000, 3), 128, dtype=np.uint8)
    pkts = build_ddp_packets(rgb, sequence=5)
    assert len(pkts) == 3

    for i, pkt in enumerate(pkts):
        flags, seq, dtype, out_id, offset, length = struct.unpack("!BBBB I H", pkt[:10])
        assert (flags & DDP_VER1) == DDP_VER1
        assert seq == 5
        assert dtype == 1 and out_id == 1
        assert length == len(pkt) - 10
        if i < 2:
            assert (flags & DDP_PUSH) == 0
            assert length == 480 * 3
        else:
            assert (flags & DDP_PUSH) == DDP_PUSH
            assert offset == 960 * 3
            assert length == 40 * 3


def test_ddp_prepared_layout_fast_and_exact():
    ctrls = [
        WLEDController(id="c1", name="One", ip="127.0.0.1", led_count=600, online=True),
        WLEDController(id="c2", name="Two", ip="127.0.0.1", led_count=250, online=True),
    ]
    table = [
        LEDDescriptor(global_id=i, controller_id="c1", controller_name="One", led_index=i)
        for i in range(600)
    ] + [
        LEDDescriptor(global_id=600 + i, controller_id="c2", controller_name="Two", led_index=i)
        for i in range(250)
    ]
    ddp = DDPStreamer()
    try:
        prepared = ddp.prepare_layout(ctrls, table)
        assert len(prepared) == 2
        assert len(prepared[0].packets) == 2  # 480 + 120
        assert len(prepared[1].packets) == 1  # 250

        rng = np.random.default_rng(7)
        rgb = rng.integers(0, 255, size=(850, 3), dtype=np.uint8)
        # Capture packets without hitting network
        sent: list[tuple[bytes, tuple[str, int]]] = []
        class _DummySock:
            def sendto(self, data, addr):
                sent.append((bytes(data), addr))
        ddp._sock.close()
        ddp._sock = _DummySock()  # type: ignore[assignment]

        t0 = time.perf_counter()
        ddp.send_global_frame(ctrls, table, rgb, brightness=1.0)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        assert dt_ms < 25.0
        assert len(sent) == 3
        # Verify payload matches reference build_ddp_packets for c2 (last chunk)
        ref_c2 = build_ddp_packets(rgb[600:], sequence=1)[0]
        assert sent[1][0] == ref_c2
    finally:
        pass


def test_all_3d_effects_render_fast_for_2000_leds():
    rng = np.random.default_rng(0)
    xyz = rng.uniform(-2.0, 2.0, size=(2000, 3)).astype(np.float32)
    for eid, meta in BUILTIN_EFFECTS.items():
        meta["fn"](xyz, 1.0, meta["defaults"])
        t0 = time.perf_counter()
        rgb = meta["fn"](xyz, 1.25, meta["defaults"])
        dt_ms = (time.perf_counter() - t0) * 1000.0
        assert rgb.shape == (2000, 3) and rgb.dtype == np.uint8
        assert dt_ms < 50.0, f"Effect {eid} took {dt_ms:.2f}ms (expected <50ms)"


@pytest.mark.asyncio
async def test_realtime_effect_thread_pacing_and_telemetry():
    ddp = DDPStreamer()
    class _SinkSock:
        def sendto(self, data, addr):
            return len(data)
        def close(self):
            pass
    ddp._sock.close()
    ddp._sock = _SinkSock()  # type: ignore[assignment]

    ctrls = [WLEDController(id="c1", name="Test", ip="127.0.0.1", led_count=100, online=True)]
    table = [LEDDescriptor(global_id=i, controller_id="c1", controller_name="Test", led_index=i) for i in range(100)]
    engine = EffectEngine(ddp)
    engine.set_scene(ctrls, table, None)

    await engine.start(effect_id="plasma_3d", fps=60.0)
    await asyncio.sleep(0.55)
    st = engine.state_dict()
    await engine.stop(blackout=False)

    assert st["frames_sent"] >= 20
    assert st["actual_fps"] >= 45.0


def test_wled_diagnostics_and_2d_ledmap_export():
    info = {"live": True, "lm": "DDP", "leds": {"count": 426, "fps": 42}, "wifi": {"rssi": -40, "signal": 100}}
    cfg_capped = {
        "wifi": {"sleep": False},
        "hw": {"led": {"fps": 42, "ins": [{"len": 426}]}},
        "if": {"live": {"en": True, "no-gc": True, "timeout": 25}},
    }
    ok, issues, meta = evaluate_wled_issues(info, cfg_capped)
    assert not ok
    assert any("FPS cap" in i for i in issues)
    assert meta["hw_max_fps"] >= 70

    cfg_opt = {
        "wifi": {"sleep": False},
        "hw": {"led": {"fps": 0, "ins": [{"len": 150}, {"len": 100}]}},
        "if": {"live": {"en": True, "no-gc": True, "timeout": 25}},
    }
    ok2, issues2, meta2 = evaluate_wled_issues(info, cfg_opt)
    assert ok2 and issues2 == []
    assert meta2["hw_max_fps"] >= 180

    # Verify 2D ledmap places every mapped LED uniquely
    leds = [
        MappedLED3D(
            global_id=i,
            controller_id="c1",
            controller_name="Tree",
            led_index=i,
            x=float(i % 10) * 0.1,
            y=float(i // 10) * 0.1,
            z=0.0,
            source="measured",
        )
        for i in range(50)
    ]
    proj = ProjectData(name="demo", created_at=0.0, map_3d=Map3DResult(leds=leds, total_leds=50, measured_count=50))
    lm = build_wled_2d_ledmap(proj, "c1", max_dim=32)
    assert lm["width"] * lm["height"] == len(lm["map"])
    placed = sorted(v for v in lm["map"] if v >= 0)
    assert placed == list(range(50))
