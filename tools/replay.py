#!/usr/bin/env python3
"""
PREDICT — dev feeder: speaks REAL Teltonika Codec 8E over TCP to the app,
exactly like a physical tracker. Replaces the old MQTT simulator — it tests
the production path end to end (listener → decode → DB → rules → dashboard).

Usage (app running on localhost):
    python tools/replay.py --imei 352999001234567 --scenario drive
    python tools/replay.py --imei 352999001234567 --scenario overheat
    python tools/replay.py --scenario burst --records 200 --interval 0

Register the car first (Settings → Add car) with the same IMEI, or records
are dropped as unknown.

Scenarios:
    drive     ignition on, moving, normal vitals; a couple of harsh events
    idle      ignition on, stationary (idling)
    overheat  coolant ramps past 110 °C and stays — fires the overheat rule
                (after the 120 s sustain window — watch Repairs → Suggestions)
    burst     one big store-and-forward flush of OLD records (stored, but no
                rules fire — replay protection)
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time


# ── Codec 8E frame building (independent copy — mirrors the wire spec) ────────
def crc16_arc(data: bytes) -> int:
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_record(ts_ms: int, *, lon: int, lat: int, speed: int,
                 io_1b=None, io_2b=None, io_4b=None, io_xb=None) -> bytes:
    io_1b = io_1b or {}
    io_2b = io_2b or {}
    io_4b = io_4b or {}
    io_xb = io_xb or {}

    rec = ts_ms.to_bytes(8, "big") + b"\x01"
    rec += struct.pack(">i", lon) + struct.pack(">i", lat)
    rec += struct.pack(">h", 25) + struct.pack(">H", 90)
    rec += b"\x09" + struct.pack(">H", speed)

    total = len(io_1b) + len(io_2b) + len(io_4b) + len(io_xb)
    event_id = next(iter(io_1b), next(iter(io_2b), 0))
    rec += event_id.to_bytes(2, "big") + total.to_bytes(2, "big")
    for group, vlen in ((io_1b, 1), (io_2b, 2), (io_4b, 4)):
        rec += len(group).to_bytes(2, "big")
        for k, v in group.items():
            rec += k.to_bytes(2, "big") + v.to_bytes(vlen, "big")
    rec += b"\x00\x00"  # 8-byte group: empty
    rec += len(io_xb).to_bytes(2, "big")
    for k, v in io_xb.items():
        rec += k.to_bytes(2, "big") + len(v).to_bytes(2, "big") + v
    return rec


def build_packet(records: list[bytes]) -> bytes:
    data = b"\x8e" + len(records).to_bytes(1, "big") + b"".join(records) \
           + len(records).to_bytes(1, "big")
    crc = crc16_arc(data)
    return b"\x00" * 4 + len(data).to_bytes(4, "big") + data + crc.to_bytes(4, "big")


# ── Scenario state ────────────────────────────────────────────────────────────
class Car:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.i = 0
        self.odometer_m = 80_234_000
        self.fuel = 68.0
        self.lon = 103_851_9590   # Singapore
        self.lat = 12_911_780

    def step(self) -> dict:
        """Next 10 s sample as FMC001 AVL IO dicts."""
        i = self.i
        self.i += 1

        if self.scenario == "idle":
            speed, rpm, moving = 0, 850, 0
            coolant = 88.0
        elif self.scenario == "overheat":
            speed, rpm, moving = 60, 2400, 1
            coolant = min(96.0 + i * 2.0, 118.0)   # ramps through 110 °C
        else:  # drive / burst
            speed = 45 + (i % 6) * 8
            rpm = 1800 + (i % 5) * 300
            moving = 1
            coolant = 90.0 + (i % 3)

        self.odometer_m += int(speed * 2.78)      # ~10 s of travel in meters
        self.fuel = max(5.0, self.fuel - 0.004)
        self.lon += int(speed * 0.5)

        io_1b = {239: 1, 240: moving, 21: 4}
        io_2b = {66: 13_800, 24: speed, 32: int(coolant), 36: int(rpm),
                 48: int(self.fuel), 51: 14_200}
        io_4b = {16: self.odometer_m}
        # A couple of device-native harsh events mid-drive for realism
        if self.scenario == "drive" and i == 12:
            io_1b[253] = 2   # harsh brake
            io_2b[254] = 82
        return {"io_1b": io_1b, "io_2b": io_2b, "io_4b": io_4b,
                "speed": speed}


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="PREDICT Codec 8E dev feeder")
    ap.add_argument("--imei", default="352999001234567")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5123)
    ap.add_argument("--scenario", choices=["drive", "idle", "overheat", "burst"],
                    default="drive")
    ap.add_argument("--records", type=int, default=60)
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between packets (0 = as fast as possible)")
    ap.add_argument("--age", type=int, default=0,
                    help="records timestamps N seconds in the past (burst default: 7200)")
    args = ap.parse_args()

    car = Car(args.scenario)
    age = args.age if args.age else (7200 if args.scenario == "burst" else 0)

    print(f"Connecting to {args.host}:{args.port} as IMEI {args.imei} "
          f"({args.scenario}, {args.records} records)…")
    sock = socket.create_connection((args.host, args.port), timeout=15)

    try:
        # IMEI handshake
        imei_b = args.imei.encode("ascii")
        sock.sendall(len(imei_b).to_bytes(2, "big") + imei_b)
        reply = sock.recv(1)
        if reply != b"\x01":
            print("Server REJECTED the IMEI handshake", file=sys.stderr)
            return 1

        for n in range(args.records):
            ts_ms = int((time.time() - age) * 1000)
            s = car.step()
            rec = build_record(
                ts_ms, lon=car.lon, lat=car.lat, speed=s["speed"],
                io_1b=s["io_1b"], io_2b=s["io_2b"], io_4b=s["io_4b"],
            )
            sock.sendall(build_packet([rec]))
            ack = sock.recv(4)
            count = int.from_bytes(ack, "big", signed=True) if len(ack) == 4 else -1
            if n % 10 == 0 or n == args.records - 1:
                print(f"  record {n + 1}/{args.records} sent, ACK={count}")
            if count != 1:
                print("  ⚠ server did not ACK — stopping", file=sys.stderr)
                return 1
            if args.interval:
                time.sleep(args.interval)

        print(f"Done: {args.records} record(s) ACKed.")
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
