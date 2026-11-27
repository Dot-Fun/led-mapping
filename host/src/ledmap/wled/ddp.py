"""High-speed multi-controller DDP (Distributed Display Protocol, UDP :4048) streamer.

Optimized for low-jitter 60-120 FPS real-time streaming:
- Precomputes per-controller NumPy scatter indices & reusable packet bytearrays once per scene.
- Zero Python per-LED loops and zero per-frame packet header allocations on the hot path.
- Sets UDP SO_SNDBUF (256 KB) and IP_TOS (DSCP CS5 / WMM priority) so Wi-Fi APs prioritize DDP.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from ledmap.models import LEDDescriptor, WLEDController

DDP_PORT = 4048
DDP_MAX_LEDS_PER_PACKET = 480  # 480 * 3 = 1440 bytes (fits cleanly in 1500 MTU)
DDP_VER1 = 0x40
DDP_PUSH = 0x01
DDP_TYPE_RGB = 0x01
DDP_IP_TOS = 0xA0  # DSCP CS5 (WMM Video/Voice priority over best-effort Wi-Fi traffic)


def build_ddp_packets(rgb: np.ndarray, sequence: int = 1) -> list[bytes]:
    """Encode an (N, 3) uint8 RGB array into 1 or more DDP v1 UDP packets.

    Only the final packet carries the PUSH flag so WLED latches the whole frame atomically.
    """
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError(f"Expected (N, 3) RGB array, got shape {arr.shape}")
    total_leds = arr.shape[0]
    if total_leds == 0:
        return []

    raw = arr.tobytes()
    max_bytes = DDP_MAX_LEDS_PER_PACKET * 3
    seq = ((sequence - 1) % 15) + 1  # DDP sequence numbers are 1..15
    packets: list[bytes] = []

    for offset in range(0, len(raw), max_bytes):
        chunk = raw[offset : offset + max_bytes]
        is_last = (offset + len(chunk)) >= len(raw)
        flags = DDP_VER1 | (DDP_PUSH if is_last else 0)
        header = struct.pack(
            "!BBBB I H",
            flags,
            seq,
            DDP_TYPE_RGB,
            1,  # output ID 1 (default display)
            offset,
            len(chunk),
        )
        packets.append(header + chunk)
    return packets


@dataclass
class _PreparedController:
    controller_id: str
    addr: tuple[str, int]
    src_idx: np.ndarray  # global_id indices into global_rgb
    dst_idx: np.ndarray  # local led_index into buf
    buf: np.ndarray  # reusable (led_count, 3) uint8
    packets: list[tuple[int, int, bytearray]]  # (byte_offset, byte_len, preallocated packet)


class DDPStreamer:
    """Streams synchronized RGB frames to any number of WLED controllers over UDP :4048."""

    def __init__(self, port: int = DDP_PORT) -> None:
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
        except OSError:
            pass
        try:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DDP_IP_TOS)
        except OSError:
            pass
        self._seq = 1
        self._layout_sig: tuple | None = None
        self._prepared: list[_PreparedController] = []

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq % 15) + 1
        return s

    def prepare_layout(
        self,
        controllers: list[WLEDController],
        led_table: list[LEDDescriptor],
    ) -> list[_PreparedController]:
        """Precompute vectorized scatter tables and reusable DDP packet buffers for a scene."""
        sig = (
            tuple((c.id, c.ip, c.led_count, c.online) for c in controllers),
            len(led_table),
            led_table[0].global_id if led_table else -1,
            led_table[-1].global_id if led_table else -1,
            led_table[-1].controller_id if led_table else "",
        )
        if sig == self._layout_sig:
            return self._prepared

        ctrl_by_id = {c.id: c for c in controllers if c.online and c.led_count > 0}
        src_lists: dict[str, list[int]] = {cid: [] for cid in ctrl_by_id}
        dst_lists: dict[str, list[int]] = {cid: [] for cid in ctrl_by_id}

        for desc in led_table:
            c = ctrl_by_id.get(desc.controller_id)
            if c is not None and 0 <= desc.led_index < c.led_count:
                src_lists[desc.controller_id].append(desc.global_id)
                dst_lists[desc.controller_id].append(desc.led_index)

        max_bytes = DDP_MAX_LEDS_PER_PACKET * 3
        prepared: list[_PreparedController] = []
        for cid, c in ctrl_by_id.items():
            total_bytes = c.led_count * 3
            pkts: list[tuple[int, int, bytearray]] = []
            for offset in range(0, total_bytes, max_bytes):
                chunk_len = min(max_bytes, total_bytes - offset)
                is_last = (offset + chunk_len) >= total_bytes
                flags = DDP_VER1 | (DDP_PUSH if is_last else 0)
                pkt = bytearray(10 + chunk_len)
                struct.pack_into(
                    "!BBBB I H",
                    pkt,
                    0,
                    flags,
                    1,
                    DDP_TYPE_RGB,
                    1,
                    offset,
                    chunk_len,
                )
                pkts.append((offset, chunk_len, pkt))
            prepared.append(
                _PreparedController(
                    controller_id=cid,
                    addr=(c.ip, self.port),
                    src_idx=np.asarray(src_lists[cid], dtype=np.intp),
                    dst_idx=np.asarray(dst_lists[cid], dtype=np.intp),
                    buf=np.zeros((c.led_count, 3), dtype=np.uint8),
                    packets=pkts,
                )
            )

        self._layout_sig = sig
        self._prepared = prepared
        return prepared

    def send_controller_rgb(self, ip: str, rgb: np.ndarray) -> None:
        packets = build_ddp_packets(rgb, sequence=self._next_seq())
        for pkt in packets:
            try:
                self._sock.sendto(pkt, (ip, self.port))
            except OSError:
                pass

    def send_global_frame(
        self,
        controllers: list[WLEDController],
        led_table: list[LEDDescriptor],
        global_rgb: np.ndarray,
        brightness: float = 1.0,
    ) -> None:
        """Split a unified (M, 3) RGB array by controller via vectorized scatter and blast UDP packets."""
        if not controllers or not led_table:
            return
        if brightness < 0.999:
            scaled = np.clip(
                global_rgb.astype(np.float32, copy=False) * np.float32(brightness),
                0.0,
                255.0,
            ).astype(np.uint8)
        else:
            scaled = np.ascontiguousarray(global_rgb, dtype=np.uint8)

        prepared = self.prepare_layout(controllers, led_table)
        if not prepared:
            return

        seq = self._next_seq()
        n_global = scaled.shape[0]
        max_chunks = 0

        for pc in prepared:
            pc.buf.fill(0)
            if pc.src_idx.size > 0:
                if int(pc.src_idx.max()) < n_global:
                    pc.buf[pc.dst_idx] = scaled[pc.src_idx]
                else:
                    valid = pc.src_idx < n_global
                    pc.buf[pc.dst_idx[valid]] = scaled[pc.src_idx[valid]]
            raw = memoryview(pc.buf).cast("B")
            for offset, chunk_len, pkt in pc.packets:
                pkt[1] = seq
                pkt[10:] = raw[offset : offset + chunk_len]
            if len(pc.packets) > max_chunks:
                max_chunks = len(pc.packets)

        # Interleave packet chunks across controllers to avoid back-to-back RX bursts on one ESP32
        for chunk_i in range(max_chunks):
            for pc in prepared:
                if chunk_i < len(pc.packets):
                    try:
                        self._sock.sendto(pc.packets[chunk_i][2], pc.addr)
                    except OSError:
                        pass

    def blackout(self, controllers: Iterable[WLEDController]) -> None:
        seq = self._next_seq()
        for c in controllers:
            if not c.online or c.led_count <= 0:
                continue
            zeros = np.zeros((c.led_count, 3), dtype=np.uint8)
            for pkt in build_ddp_packets(zeros, sequence=seq):
                try:
                    self._sock.sendto(pkt, (c.ip, self.port))
                except OSError:
                    pass

    def fill_solid(self, controllers: Iterable[WLEDController], r: int, g: int, b: int) -> None:
        seq = self._next_seq()
        for c in controllers:
            if not c.online or c.led_count <= 0:
                continue
            arr = np.empty((c.led_count, 3), dtype=np.uint8)
            arr[:, 0] = r
            arr[:, 1] = g
            arr[:, 2] = b
            for pkt in build_ddp_packets(arr, sequence=seq):
                try:
                    self._sock.sendto(pkt, (c.ip, self.port))
                except OSError:
                    pass
