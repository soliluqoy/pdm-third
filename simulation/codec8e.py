"""
Standalone Teltonika Codec 8 Extended (0x8E) *encoder*.

Mirrors the wire format expected by PREDICT's parser (app/server/teltonika/codec8e.py)
but lives entirely in this folder — no imports from the main project.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Union

CODEC_8_EXT = 0x8E
PREAMBLE = b"\x00\x00\x00\x00"

IOValue = Union[int, bytes]


def crc16_arc(data: bytes) -> int:
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


@dataclass
class SimRecord:
    """One AVL record to encode."""
    timestamp: datetime
    latitude: float
    longitude: float
    altitude: int = 20
    angle: int = 0
    satellites: int = 12
    speed: int = 0  # GNSS km/h
    priority: int = 0
    event_id: int = 0
    io: Dict[int, IOValue] = field(default_factory=dict)


def build_imei_handshake(imei: str) -> bytes:
    raw = imei.encode("ascii")
    if not imei.isdigit() or not (14 <= len(raw) <= 17):
        raise ValueError(f"IMEI must be 14–17 digits, got {imei!r}")
    return struct.pack(">H", len(raw)) + raw


def _pack_uint(value: int, length: int) -> bytes:
    fmt = {1: ">B", 2: ">H", 4: ">I", 8: ">Q"}[length]
    return struct.pack(fmt, value)


def _io_value_size(value: IOValue) -> int | None:
    """Return 1/2/4/8 for numeric IO, or None for variable-length bytes."""
    if isinstance(value, (bytes, bytearray)):
        return None
    if value < 0:
        raise ValueError(f"IO values must be unsigned, got {value}")
    if value <= 0xFF:
        return 1
    if value <= 0xFFFF:
        return 2
    if value <= 0xFFFFFFFF:
        return 4
    return 8


def _encode_record(rec: SimRecord) -> bytes:
    ts = rec.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts_ms = int(ts.timestamp() * 1000)

    lon = int(round(rec.longitude * 1e7))
    lat = int(round(rec.latitude * 1e7))
    alt = int(rec.altitude) & 0xFFFF
    angle = int(rec.angle) % 360
    sats = max(0, min(255, int(rec.satellites)))
    speed = max(0, min(65535, int(rec.speed)))

    out = bytearray()
    out += struct.pack(">QB", ts_ms, rec.priority & 0xFF)
    out += struct.pack(">iiHHBH", lon, lat, alt, angle, sats, speed)

    # Bucket IO by wire size
    groups: dict[int, list[tuple[int, int]]] = {1: [], 2: [], 4: [], 8: []}
    x_group: list[tuple[int, bytes]] = []
    for io_id, value in rec.io.items():
        size = _io_value_size(value)
        if size is None:
            x_group.append((io_id, bytes(value)))
        else:
            groups[size].append((io_id, int(value)))

    total = sum(len(v) for v in groups.values()) + len(x_group)
    out += _pack_uint(rec.event_id, 2)
    out += _pack_uint(total, 2)

    for size in (1, 2, 4, 8):
        items = groups[size]
        out += _pack_uint(len(items), 2)
        for io_id, val in items:
            out += _pack_uint(io_id, 2)
            out += _pack_uint(val, size)

    out += _pack_uint(len(x_group), 2)
    for io_id, raw in x_group:
        out += _pack_uint(io_id, 2)
        out += _pack_uint(len(raw), 2)
        out += raw

    return bytes(out)


def build_avl_packet(records: List[SimRecord]) -> bytes:
    if not records:
        raise ValueError("need at least one record")
    body = bytearray()
    body.append(CODEC_8_EXT)
    body.append(len(records) & 0xFF)
    for rec in records:
        body += _encode_record(rec)
    body.append(len(records) & 0xFF)

    data = bytes(body)
    crc = crc16_arc(data)
    return PREAMBLE + struct.pack(">I", len(data)) + data + struct.pack(">I", crc)
