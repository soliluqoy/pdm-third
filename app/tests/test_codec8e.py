"""
PREDICT — Codec 8 / 8E parser tests (golden frames).

Frames are built byte-by-byte here in the test (mirroring the Teltonika wiki
spec) so the parser is validated against an independent construction.
No hardware needed.
"""
import struct
from datetime import timezone

import pytest

from server.teltonika.codec8e import (
    CODEC_8,
    CODEC_8_EXT,
    CodecError,
    IncompletePacket,
    build_ack,
    build_imei_reply,
    crc16_arc,
    parse_avl_packet,
    parse_imei,
)


# ── Golden-frame builders (independent of the parser) ─────────────────────────
def build_record(ts_ms, *, lon=1038519590, lat=12911780, altitude=25, angle=90,
                 sats=9, speed=45, priority=1, id_len=2,
                 io_1b=None, io_2b=None, io_4b=None, io_8b=None, io_xb=None):
    io_1b = io_1b or {}
    io_2b = io_2b or {}
    io_4b = io_4b or {}
    io_8b = io_8b or {}
    io_xb = io_xb or {}

    rec = ts_ms.to_bytes(8, "big") + bytes([priority])
    rec += struct.pack(">i", lon) + struct.pack(">i", lat)
    rec += struct.pack(">h", altitude) + struct.pack(">H", angle)
    rec += bytes([sats]) + struct.pack(">H", speed)

    groups = [io_1b, io_2b, io_4b, io_8b]
    total = sum(len(g) for g in groups) + len(io_xb)
    event_id = next((k for g in groups for k in g), 0)

    rec += event_id.to_bytes(id_len, "big") + total.to_bytes(id_len, "big")
    for group, vlen in zip(groups, (1, 2, 4, 8)):
        rec += len(group).to_bytes(id_len, "big")
        for k, v in group.items():
            rec += k.to_bytes(id_len, "big") + v.to_bytes(vlen, "big")
    if id_len == 2:  # Codec 8E has the variable-length X group
        rec += len(io_xb).to_bytes(2, "big")
        for k, v in io_xb.items():
            rec += k.to_bytes(2, "big") + len(v).to_bytes(2, "big") + v
    return rec


def build_packet(records, codec_id=CODEC_8_EXT):
    data = bytes([codec_id, len(records)]) + b"".join(records) + bytes([len(records)])
    crc = crc16_arc(data)
    return b"\x00" * 4 + len(data).to_bytes(4, "big") + data + crc.to_bytes(4, "big")


TS_MS = 1785512345000  # fixed epoch ms for reproducibility


# ── CRC ───────────────────────────────────────────────────────────────────────
def test_crc16_arc_known_vector():
    # CRC-16/ARC check value for ASCII "123456789" is 0xBB3D
    assert crc16_arc(b"123456789") == 0xBB3D


# ── IMEI handshake ────────────────────────────────────────────────────────────
def test_parse_imei_roundtrip():
    frame = b"\x00\x0f350424061234001"
    imei, consumed = parse_imei(frame)
    assert imei == "350424061234001"
    assert consumed == 17


def test_parse_imei_incomplete():
    with pytest.raises(IncompletePacket):
        parse_imei(b"\x00\x0f35042")


def test_parse_imei_rejects_garbage():
    with pytest.raises(CodecError):
        parse_imei(b"\x00\x0fXXXXXXXXXXXXXXX")


def test_imei_reply_bytes():
    assert build_imei_reply(True) == b"\x01"
    assert build_imei_reply(False) == b"\x00"


# ── ACK ───────────────────────────────────────────────────────────────────────
def test_build_ack():
    assert build_ack(3) == b"\x00\x00\x00\x03"


# ── AVL packet parsing (Codec 8E) ─────────────────────────────────────────────
def test_parse_8e_single_record():
    rec = build_record(
        TS_MS,
        io_1b={239: 1, 240: 1, 21: 4},          # ignition, movement, gsm
        io_2b={66: 13800, 24: 55},               # ext voltage mV, speed
        io_4b={16: 80234567},                    # odometer m
    )
    packet = build_packet([rec])

    records, consumed = parse_avl_packet(packet)
    assert consumed == len(packet)
    assert len(records) == 1

    r = records[0]
    assert r.timestamp.tzinfo == timezone.utc
    assert int(r.timestamp.timestamp() * 1000) == TS_MS
    assert r.latitude == pytest.approx(1.291178)
    assert r.longitude == pytest.approx(103.851959)
    assert r.altitude == 25
    assert r.satellites == 9
    assert r.speed == 45
    assert r.io[239] == 1
    assert r.io[66] == 13800
    assert r.io[16] == 80234567


def test_parse_8e_multi_record_and_trailing_bytes():
    recs = [build_record(TS_MS + i * 10000, io_1b={239: 1}) for i in range(3)]
    packet = build_packet(recs)
    records, consumed = parse_avl_packet(packet + b"\x00\x00\x00\x00extra")
    assert len(records) == 3
    assert consumed == len(packet)  # trailing bytes left for the next frame
    assert records[2].timestamp.timestamp() > records[0].timestamp.timestamp()


def test_parse_8e_x_group_variable_length():
    rec = build_record(TS_MS, io_1b={239: 1}, io_xb={10001: b"\xde\xad\xbe\xef"})
    records, _ = parse_avl_packet(build_packet([rec]))
    assert records[0].io[10001] == b"\xde\xad\xbe\xef"


def test_parse_codec8_one_byte_ids():
    rec = build_record(TS_MS, id_len=1, io_1b={239: 0}, io_2b={66: 12100})
    records, consumed = parse_avl_packet(build_packet([rec], codec_id=CODEC_8))
    assert consumed > 0
    assert records[0].io[239] == 0
    assert records[0].io[66] == 12100


# ── Negative / robustness ─────────────────────────────────────────────────────
def test_incomplete_packet_raises():
    packet = build_packet([build_record(TS_MS, io_1b={239: 1})])
    with pytest.raises(IncompletePacket):
        parse_avl_packet(packet[:20])  # truncated


def test_bad_crc_rejected():
    packet = bytearray(build_packet([build_record(TS_MS, io_1b={239: 1})]))
    packet[-1] ^= 0xFF  # corrupt CRC
    with pytest.raises(CodecError):
        parse_avl_packet(bytes(packet))


def test_bad_preamble_rejected():
    packet = bytearray(build_packet([build_record(TS_MS, io_1b={239: 1})]))
    packet[0] = 0xFF
    with pytest.raises(CodecError):
        parse_avl_packet(bytes(packet))


def test_record_count_mismatch_rejected():
    rec = build_record(TS_MS, io_1b={239: 1})
    data = bytes([CODEC_8_EXT, 1]) + rec + bytes([2])  # Number of Data 2 = 2
    crc = crc16_arc(data)
    packet = b"\x00" * 4 + len(data).to_bytes(4, "big") + data + crc.to_bytes(4, "big")
    with pytest.raises(CodecError):
        parse_avl_packet(packet)
