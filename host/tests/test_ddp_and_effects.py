"""Tests for DDP packet encoding and vectorized 3D effect speed."""
from __future__ import annotations

import struct
import time

import numpy as np

from ledmap.engine.effects import BUILTIN_EFFECTS
from ledmap.wled.ddp import DDP_PUSH, DDP_VER1, build_ddp_packets


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


def test_all_3d_effects_render_fast_for_2000_leds():
    rng = np.random.default_rng(0)
    xyz = rng.uniform(-2.0, 2.0, size=(2000, 3)).astype(np.float32)
    for eid, meta in BUILTIN_EFFECTS.items():
        # Warm up once to avoid cold-start jitter under concurrent loads
        meta["fn"](xyz, 1.0, meta["defaults"])
        t0 = time.perf_counter()
        rgb = meta["fn"](xyz, 1.25, meta["defaults"])
        dt_ms = (time.perf_counter() - t0) * 1000.0
        assert rgb.shape == (2000, 3) and rgb.dtype == np.uint8
        assert dt_ms < 50.0, f"Effect {eid} took {dt_ms:.2f}ms (expected <50ms)"
