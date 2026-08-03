"""
PREDICT — Teltonika Codec 8 / Codec 8 Extended (8E) parser.

Pure-function, socket-free protocol module so it is fully unit-testable.

Protocol summary (Teltonika wiki — "Codec 8 / Codec 8 Extended"):
  IMEI handshake : device → 2-byte length + N ASCII digits
                   server → 0x01 (accept) / 0x00 (reject)
  AVL data packet:
    Preamble         4 bytes  (0x00000000)
    Data length      4 bytes  (big-endian; Codec ID .. Number of Data 2)
    Codec ID         1 byte   (0x08 = Codec 8, 0x8E = Codec 8 Extended)
    Number of Data 1 1 byte
    AVL records      × N
      Timestamp      8 bytes  (ms since Unix epoch, UTC)
      Priority       1 byte
      GPS element    lon(4s) lat(4s) alt(2s) angle(2) sats(1) speed(2)
      IO element     event id, total count, N1/N2/N4/N8 groups (+ NX for 8E)
    Number of Data 2 1 byte   (must equal Number of Data 1)
    CRC-16           4 bytes  (CRC-16/ARC over Codec ID .. Number of Data 2)
  Server ACK     : 4 bytes = number of records accepted (big-endian int32)

Codec 8  → 1-byte IO IDs, no variable-length (X) group.
Codec 8E → 2-byte IO IDs plus a variable-length X group (needed for FMC150
           CAN parameters, which use 2-byte IO IDs).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Union

CODEC_8 = 0x08
CODEC_8_EXT = 0x8E
PREAMBLE = b"\x00\x00\x00\x00"
MAX_DATA_LENGTH = 256 * 1024  # sanity bound for the 4-byte length field

IOValue = Union[int, bytes]


class IncompletePacket(Exception):
    """Raised when the buffer does not yet contain a full packet."""


class CodecError(Exception):
    """Raised when the buffer contains an invalid/corrupt packet."""


# ── CRC-16/ARC ────────────────────────────────────────────────────────────────
def crc16_arc(data: bytes) -> int:
    """CRC-16/ARC (reflected poly 0xA001, init 0x0000, no final XOR)."""
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class AvlRecord:
    """One decoded AVL record."""
    timestamp: datetime          # timezone-aware UTC
    priority: int
    longitude: float             # degrees
    latitude: float              # degrees
    altitude: int                # meters
    angle: int                   # degrees
    satellites: int
    speed: int                   # km/h
    io: Dict[int, IOValue] = field(default_factory=dict)  # AVL ID → value


# ── IMEI handshake ────────────────────────────────────────────────────────────
def parse_imei(buffer: bytes) -> Tuple[str, int]:
    """Parse the IMEI handshake. Returns (imei, bytes_consumed).

    Raises IncompletePacket if more bytes are needed, CodecError if malformed.
    """
    if len(buffer) < 2:
        raise IncompletePacket("need IMEI length")
    (length,) = struct.unpack_from(">H", buffer, 0)
    if length < 14 or length > 17:
        raise CodecError(f"implausible IMEI length {length}")
    if len(buffer) < 2 + length:
        raise IncompletePacket("need full IMEI")
    raw = buffer[2:2 + length]
    try:
        imei = raw.decode("ascii")
    except UnicodeDecodeError as e:
        raise CodecError(f"IMEI not ASCII: {e}") from e
    if not imei.isdigit():
        raise CodecError(f"IMEI not numeric: {imei!r}")
    return imei, 2 + length


def build_imei_reply(accept: bool) -> bytes:
    """Server reply to the IMEI handshake: 0x01 accept / 0x00 reject."""
    return b"\x01" if accept else b"\x00"


def build_ack(record_count: int) -> bytes:
    """Server ACK after an AVL packet: 4-byte big-endian accepted count."""
    return struct.pack(">i", record_count)


# ── AVL packet parsing ────────────────────────────────────────────────────────
def parse_avl_packet(buffer: bytes) -> Tuple[List[AvlRecord], int]:
    """Parse one AVL data packet from the front of *buffer*.

    Returns (records, bytes_consumed). The caller keeps any remainder.
    Raises IncompletePacket if more bytes are needed; CodecError if the
    packet is malformed or the CRC fails (caller decides resync strategy).
    """
    if len(buffer) < 8:
        raise IncompletePacket("need preamble + length")
    if buffer[:4] != PREAMBLE:
        raise CodecError("missing 0x00000000 preamble")
    (data_len,) = struct.unpack_from(">I", buffer, 4)
    if data_len == 0 or data_len > MAX_DATA_LENGTH:
        raise CodecError(f"implausible data length {data_len}")

    total = 8 + data_len + 4  # preamble+len | data | crc
    if len(buffer) < total:
        raise IncompletePacket(f"need {total - len(buffer)} more bytes")

    data = buffer[8:8 + data_len]
    (crc_expected,) = struct.unpack_from(">I", buffer, 8 + data_len)
    crc_actual = crc16_arc(data)
    if crc_actual != crc_expected:
        raise CodecError(
            f"CRC mismatch: expected 0x{crc_expected:08X}, got 0x{crc_actual:08X}"
        )

    codec_id = data[0]
    if codec_id == CODEC_8_EXT:
        id_len = 2
        has_x_group = True
    elif codec_id == CODEC_8:
        id_len = 1
        has_x_group = False
    else:
        raise CodecError(f"unsupported codec 0x{codec_id:02X}")

    num_data_1 = data[1]
    if num_data_1 == 0:
        raise CodecError("packet with 0 records")

    records: List[AvlRecord] = []
    offset = 2
    try:
        for _ in range(num_data_1):
            record, offset = _parse_record(data, offset, id_len, has_x_group)
            records.append(record)
        num_data_2 = data[offset]
    except (IndexError, struct.error) as e:
        raise CodecError(f"truncated record data: {e}") from e

    if num_data_2 != num_data_1:
        raise CodecError(
            f"record count mismatch: {num_data_1} vs {num_data_2}"
        )
    return records, total


def _parse_record(
    data: bytes, offset: int, id_len: int, has_x_group: bool
) -> Tuple[AvlRecord, int]:
    """Parse a single AVL record starting at *offset* inside *data*."""
    ts_ms, priority = struct.unpack_from(">QB", data, offset)
    offset += 9

    lon_raw, lat_raw, altitude, angle, sats, speed = struct.unpack_from(
        ">iiHHBH", data, offset
    )
    offset += 15

    record = AvlRecord(
        timestamp=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
        priority=priority,
        longitude=lon_raw / 1e7,
        latitude=lat_raw / 1e7,
        altitude=altitude if altitude < 0x8000 else altitude - 0x10000,
        angle=angle,
        satellites=sats,
        speed=speed,
    )

    # IO element: event ID + total count (id_len each), then N1/N2/N4/N8
    _event_id, offset = _read_uint(data, offset, id_len)
    _total, offset = _read_uint(data, offset, id_len)

    for value_len in (1, 2, 4, 8):
        count, offset = _read_uint(data, offset, id_len)
        for _ in range(count):
            io_id, offset = _read_uint(data, offset, id_len)
            value, offset = _read_uint(data, offset, value_len)
            record.io[io_id] = value

    if has_x_group:
        nx_count, offset = _read_uint(data, offset, id_len)
        for _ in range(nx_count):
            io_id, offset = _read_uint(data, offset, id_len)
            val_len, offset = _read_uint(data, offset, id_len)
            record.io[io_id] = bytes(data[offset:offset + val_len])
            offset += val_len

    return record, offset


def _read_uint(data: bytes, offset: int, length: int) -> Tuple[int, int]:
    """Read a big-endian unsigned int of *length* bytes at *offset*."""
    fmt = {1: ">B", 2: ">H", 4: ">I", 8: ">Q"}[length]
    (value,) = struct.unpack_from(fmt, data, offset)
    return value, offset + length
