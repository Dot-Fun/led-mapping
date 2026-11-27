"""High-speed multi-controller DDP (Distributed Display Protocol, UDP :4048) streamer."""
from __future__ import annotations

import socket
import struct
from typing import Iterable

import numpy as np

from ledmap.models import LEDDescriptor, WLEDController

DDP_PORT = 4048
DDP_MAX_LEDS_PER_PACKET = 480  # 480 * 3 = 1440 bytes (fits cleanly in 1500 MTU)
DDP_VER1 = 0x40
DDP_PUSH = 0x01
DDP_TYPE_RGB = 0x01


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


class DDPStreamer:
    """Streams synchronized RGB frames to any number of WLED controllers over UDP :4048."""

    def __init__(self, port: int = DDP_PORT) -> None:
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._seq = 1

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq % 15) + 1
        return s

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
        """Split a unified (M, 3) RGB array by controller and blast UDP packets in parallel."""
        if not controllers or not led_table:
            return
        if brightness < 0.999:
            scaled = np.clip(global_rgb.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
        else:
            scaled = np.ascontiguousarray(global_rgb, dtype=np.uint8)

        ctrl_by_id = {c.id: c for c in controllers if c.online}
        buffers: dict[str, np.ndarray] = {
            cid: np.zeros((c.led_count, 3), dtype=np.uint8) for cid, c in ctrl_by_id.items()
        }
        for desc in led_table:
            buf = buffers.get(desc.controller_id)
            if buf is not None and 0 <= desc.led_index < buf.shape[0] and desc.global_id < scaled.shape[0]:
                buf[desc.led_index] = scaled[desc.global_id]

        seq = self._next_seq()
        for cid, buf in buffers.items():
            ip = ctrl_by_id[cid].ip
            for pkt in build_ddp_packets(buf, sequence=seq):
                try:
                    self._sock.sendto(pkt, (ip, self.port))
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
