#!/usr/bin/env python3
"""
PREDICT — FMC150 car simulator: a virtual car fitted with a Teltonika FMC150
wired CAN tracker. Speaks REAL Codec 8 Extended over TCP to the app, exactly
like the physical device: IMEI handshake → AVL data packets → waits for ACK.

Unlike tools/replay.py (FMC001 OBD-II dongle), this streams the FMC150 CAN
parameter set (AVL IDs per server/teltonika/avl/fmc150.json): CAN speed, RPM,
coolant (×0.1 °C), throttle, fuel %/liters/consumed, CAN mileage in meters,
engine hours, CAN battery voltage, oil temp/pressure/level, ambient temp,
distance-to-service, remaining range — plus the VIN and fault codes in the
Codec 8E variable-length X group, and device-native eco-driving events.

Register the car first (Settings → Add car, model FMC150) with the same IMEI,
or pass --register to do it over the REST API before streaming.

Usage (app running on localhost):
    python tools/simulate_fmc150.py --register
    python tools/simulate_fmc150.py --scenario overheat
    python tools/simulate_fmc150.py --scenario dtc --interval 0.2
    python tools/simulate_fmc150.py --scenario burst          # store-and-forward

Time model: records are spaced --step virtual seconds apart (default 10 s,
the recommended device send period) but emitted every --interval REAL second,
so a 10-minute drive replays in a minute and sustained-duration rules still
fire. Timestamps end a few seconds ahead of "now", so the latest records are
always inside the server's freshness window.

Scenarios (all but overheat end parked with the ignition off, so trips close
cleanly and scenarios can be chained in any order):
    commute       cold start → city → highway (device overspeed event) →
                  harsh brake → park (ignition off: trip closes, score).
                  The full dashboard demo. (default)
    idle          parked with engine running — derived IDLING event fires
                  after the 5-minute virtual idle window, then key off
    overheat      engine already hot, cooling fails: 106 → 118 °C fires
                  CRITICAL "Engine overheating" AND "Coolant running hot"
    weak_battery  CAN battery voltage sags to 11.5 V → "Car battery low"
    dtc           check-engine: reports P0128 + P0300 → fault-code issues
    service       distance-to-service under 500 km → "Service due soon"
    burst         ONE store-and-forward packet of OLD records (stored, but
                  no rules fire — replay protection)
"""
from __future__ import annotations

import argparse
import json
import math
import random
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# ── Codec 8E frame building (independent copy — mirrors the wire spec) ────────
def crc16_arc(data: bytes) -> int:
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_record(ts_ms: int, *, lon: int, lat: int, alt: int, angle: int,
                 sats: int, speed: int, priority: int = 0,
                 io_1b=None, io_2b=None, io_4b=None, io_xb=None,
                 event_id: int = 0) -> bytes:
    """One Codec 8E AVL record: ALL IO IDs and counts are 2 bytes, plus the
    variable-length X group (used for the VIN and fault-code strings)."""
    io_1b = io_1b or {}
    io_2b = io_2b or {}
    io_4b = io_4b or {}
    io_xb = io_xb or {}

    rec = ts_ms.to_bytes(8, "big") + bytes([priority])
    rec += struct.pack(">i", lon) + struct.pack(">i", lat)
    rec += struct.pack(">h", alt) + struct.pack(">H", angle % 360)
    rec += bytes([sats]) + struct.pack(">H", max(0, min(int(speed), 65535)))

    total = len(io_1b) + len(io_2b) + len(io_4b) + len(io_xb)
    rec += event_id.to_bytes(2, "big") + total.to_bytes(2, "big")
    for group, vlen in ((io_1b, 1), (io_2b, 2), (io_4b, 4)):
        rec += len(group).to_bytes(2, "big")
        for k, v in group.items():
            rec += k.to_bytes(2, "big") + int(v).to_bytes(vlen, "big")
    rec += b"\x00\x00"  # 8-byte group: empty
    rec += len(io_xb).to_bytes(2, "big")
    for k, v in io_xb.items():
        rec += k.to_bytes(2, "big") + len(v).to_bytes(2, "big") + v
    return rec


def build_packet(records: list[bytes]) -> bytes:
    data = (b"\x8e" + len(records).to_bytes(1, "big")
            + b"".join(records) + len(records).to_bytes(1, "big"))
    crc = crc16_arc(data)
    return (b"\x00" * 4 + len(data).to_bytes(4, "big") + data
            + crc.to_bytes(4, "big"))


# ── AVL IDs (FMC150 — see app/server/teltonika/avl/fmc150.json) ──────────────
AVL_IGNITION = 239          # 1B meta
AVL_MOVEMENT = 240          # 1B meta
AVL_GSM = 21                # 1B, 0-5
AVL_EXT_VOLTAGE = 66        # 2B, mV — tracker supply (car battery at the wire)
AVL_TRACKER_BAT = 67        # 2B, mV — internal backup battery
AVL_SPEED = 24              # 2B, km/h (GNSS-based)
AVL_CAN_RPM = 85            # 2B — CAN standard
AVL_CAN_COOLANT = 115       # 2B, 0.1 °C (raw 910 = 91.0 °C)
AVL_CAN_SPEED = 81          # 2B, km/h — CAN standard
AVL_CAN_THROTTLE = 82       # 1B, %
AVL_CAN_FUEL_PCT = 89       # 1B, %
AVL_CAN_FUEL_L = 84         # 2B, 0.1 L
AVL_CAN_FUEL_USED = 83      # 4B, 0.1 L cumulative
AVL_CAN_ODOMETER = 87       # 4B, meters — 'Total Mileage'
AVL_CAN_ENGINE_MIN = 102    # 4B, minutes
AVL_CAN_BATTERY = 168       # 2B, V — battery voltage reported by the car
AVL_CAN_HV_PCT = 152        # 1B, % — EV/hybrid HV battery charge
AVL_CAN_AMBIENT = 1396      # 2B, °C — CAN extended
AVL_CAN_OIL_TEMP = 1270     # 2B, °C — CAN extended
AVL_CAN_OIL_PRESS = 1158    # 2B, kPa — CAN extended
AVL_CAN_OIL_LEVEL = 1159    # 1B, % — CAN extended
AVL_VIN = 325               # X group, 17-char ASCII
AVL_DTC_COUNT = 160         # 1B
AVL_DTC_CODES = 282         # X group, comma-separated ASCII
AVL_SERVICE_KM = 400        # 4B, km — distance till next service
AVL_RANGE_KM = 866          # 2B, km — remaining range
AVL_GREEN_TYPE = 253        # 1B event: 1=harsh accel 2=harsh brake 3=corner
AVL_GREEN_VALUE = 254       # 2B event magnitude (0.01 m/s²)
AVL_OVERSPEED = 255         # 2B event, km/h

VIN = "WBA3A5C51DF123456"
TANK_L = 50.0


# ── The virtual car ───────────────────────────────────────────────────────────
@dataclass
class Frame:
    """One 10 s sample of the car's physical state (pre-AVL-encoding)."""
    t: int                       # virtual seconds since scenario start
    ignition: bool
    speed: float                 # km/h
    rpm: int
    coolant_c: float
    oil_c: float
    oil_press_kpa: int
    throttle: int
    ambient_c: int
    ext_mv: int
    can_batt_v: int
    hv_pct: int | None
    fuel_l: float
    fuel_used_l: float
    odometer_m: int
    engine_min: int
    service_km: int
    range_km: int
    lat: float
    lon: float
    heading: int
    dtc_count: int = 0
    dtc_codes: str | None = None  # sent once, X group
    send_vin: bool = False
    green: tuple[int, int] | None = None   # (type, value×100)
    overspeed: int | None = None


class FMC150Car:
    """Minimal but plausible physics: warm-up, gearing, fuel burn, GPS drift."""

    def __init__(self, scenario: str, hybrid: bool, seed: int | None):
        self.scenario = scenario
        self.hybrid = hybrid
        self.rng = random.Random(seed)
        # Odometer / counters (a used car)
        self.odometer_m = 80_234_500
        self.fuel_l = 34.0
        self.fuel_used_l = 4_253.7
        self.engine_min = 152_300
        self.service_km = 480 if scenario == "service" else 4_320
        # Thermal state (Singapore); overheat starts already hot — the cooling
        # system fails during the drive (every record stays above 105 °C, so
        # the 300 s sustained rule fires inside the server's freshness window)
        self.ambient_c = 31
        self.coolant_c = {"commute": 31.0, "overheat": 106.0}.get(scenario, 88.0)
        self.oil_c = float(self.ambient_c)
        self.hv_pct = 82.0 if hybrid else None
        self.dtcs: list[str] = []
        # GPS — start in central Singapore, heading NE
        self.lat = 1.2911780
        self.lon = 103.8519590
        self.heading = 55.0
        self.speed = 0.0
        self._prev_speed = 0.0
        self._harsh_accel_sent = False
        self._harsh_brake_sent = False
        self._overspeed_sent = False

    # ── Scenario speed/ignition programmes ────────────────────────────────────
    def _programme(self, t: int) -> tuple[bool, float]:
        """Return (ignition, target_speed) for virtual time t seconds.

        Every scenario except overheat ends with a few ignition-OFF records:
        trips then close via the robust ignition-edge path (never the gap
        fallback), so scenarios can be chained in any order without the
        virtual-timestamp jump confusing the server's trip segmentation.
        """
        s = self.scenario
        if s == "idle":
            return (t < 350), 0.0          # idling event at t=300, then key off
        if s == "overheat":
            return True, 62.0 if t >= 10 else 0.0   # stays on: the 300 s rule
                                                    # needs the last record hot
        if s in ("weak_battery", "dtc", "service", "burst"):
            off_at = {"weak_battery": 270, "dtc": 220,
                      "service": 170, "burst": 460}[s]
            if t >= off_at:
                return False, 0.0          # parked — trip closes
            return True, 48.0 if t >= 10 else 0.0
        # commute (default): full drive cycle, ending parked
        legs = [
            (30, 0.0),     # warm-up at the kerb
            (60, 45.0),    # pull away
            (120, 52.0),   # city streets
            (150, 0.0),    # traffic light
            (180, 48.0),
            (240, 85.0),   # onto the expressway
            (300, 95.0),
            (360, 126.0),  # overtake — device overspeed + derived speeding
            (420, 96.0),
            (450, 32.0),   # exit ramp — harsh brake at the top
        ]
        target = 0.0
        for end_t, spd in legs:
            if t < end_t:
                target = spd
                break
        if t >= 450:
            target = 0.0
        return (t < 480), target           # key off on arrival — trip closes

    # ── One virtual step ──────────────────────────────────────────────────────
    def step(self, t: int, dt: int) -> Frame:
        ignition, target = self._programme(t)
        rng = self.rng

        # Speed: smooth chase of the target, brakes bite harder than the throttle
        k = 0.55 if target < self.speed else 0.35
        self.speed += (target - self.speed) * k
        if abs(self.speed - target) < 1.5:
            self.speed = target
        speed = max(0.0, self.speed + (rng.uniform(-1.5, 1.5) if ignition else 0))
        if not ignition:
            speed = 0.0

        # Thermal: first-order approach (τ≈90 s coolant, τ≈240 s oil)
        if ignition:
            coolant_target = 91.0
            if self.scenario == "overheat":
                coolant_target = 126.0     # failed thermostat / lost coolant
            self.coolant_c += (coolant_target - self.coolant_c) * (dt / 90.0)
            self.oil_c += (self.coolant_c + 12.0 - self.oil_c) * (dt / 240.0)
        else:
            self.coolant_c += (self.ambient_c - self.coolant_c) * (dt / 600.0)
            self.oil_c += (self.ambient_c - self.oil_c) * (dt / 900.0)
        coolant = min(self.coolant_c, 118.0)

        # Engine / electrical
        moving = speed > 3.0
        if not ignition:
            rpm, throttle, oil_press = 0, 0, 0
            ext_v = 12.3 + rng.uniform(-0.05, 0.05)
        elif not moving:
            rpm, throttle, oil_press = 850, 0, 150
            ext_v = 13.9 + rng.uniform(-0.1, 0.1)
        else:
            rpm = int(min(4800, 1100 + speed * 24))
            throttle = int(min(90, 18 + speed * 0.25 + rng.uniform(0, 6)))
            oil_press = int(280 + rpm * 0.05)
            ext_v = 14.2 + rng.uniform(-0.15, 0.15)
        can_batt = round(ext_v) if self.scenario != "weak_battery" else 11

        # Fuel burn: ~7.5 L/100km on the move, ~0.9 L/h at idle
        burn = (speed * 7.5 / 360000.0 + (0.00025 if ignition and not moving else 0.0)) * dt
        self.fuel_l = max(2.0, self.fuel_l - burn)
        self.fuel_used_l += burn
        range_km = int(self.fuel_l * 13.3)

        # Counters + GPS advance
        dist_m = speed * dt / 3.6
        self.odometer_m += int(dist_m)
        self.service_km = max(0, self.service_km - int(dist_m / 1000))
        if ignition:
            self.engine_min += dt / 60.0
        self.heading = (55.0 + 40.0 * math.sin(t / 70.0)) % 360
        if moving:
            rad = math.radians(self.heading)
            self.lat += dist_m * math.cos(rad) / 111320.0
            self.lon += dist_m * math.sin(rad) / (111320.0 * math.cos(math.radians(self.lat)))
        if self.hv_pct is not None:
            regen = 0.15 if (self._prev_speed - speed) > 10 else 0.0
            self.hv_pct = min(95.0, max(15.0, self.hv_pct - (0.03 if moving else 0.0) + regen))

        # Device-native eco-driving / overspeed events (Eco-driving feature)
        green = None
        overspeed = None
        if self.scenario == "commute":
            if not self._harsh_accel_sent and speed > 20 and self._prev_speed <= 20:
                green, self._harsh_accel_sent = (1, 245), True   # 2.45 m/s²
            if not self._harsh_brake_sent and 420 <= t < 450 and self._prev_speed > 60:
                green, self._harsh_brake_sent = (2, 310), True   # 3.10 m/s²
            if not self._overspeed_sent and speed > 120:
                overspeed, self._overspeed_sent = int(speed), True
        self._prev_speed = speed

        # Check-engine: codes appear once, then only the count is reported
        dtc_codes = None
        if self.scenario == "dtc" and t >= 100 and not self.dtcs:
            self.dtcs = ["P0128", "P0300"]
            dtc_codes = ",".join(self.dtcs)

        return Frame(
            t=t, ignition=ignition, speed=speed, rpm=rpm,
            coolant_c=coolant, oil_c=self.oil_c, oil_press_kpa=oil_press,
            throttle=throttle, ambient_c=self.ambient_c,
            ext_mv=int(ext_v * 1000), can_batt_v=can_batt,
            hv_pct=int(self.hv_pct) if self.hv_pct is not None else None,
            fuel_l=self.fuel_l, fuel_used_l=self.fuel_used_l,
            odometer_m=self.odometer_m,
            engine_min=int(self.engine_min), service_km=self.service_km,
            range_km=range_km, lat=self.lat, lon=self.lon,
            heading=int(self.heading),
            dtc_count=len(self.dtcs), dtc_codes=dtc_codes,
            send_vin=(t == 0), green=green, overspeed=overspeed,
        )


# ── Frame → FMC150 AVL IO groups ──────────────────────────────────────────────
def frame_to_io(f: Frame) -> dict:
    """Map one physics frame onto FMC150 AVL IDs, raw-encoded to match the
    scales in avl/fmc150.json (coolant ×10, fuel ×10, odometer in m, ...)."""
    io_1b = {AVL_IGNITION: int(f.ignition), AVL_MOVEMENT: int(f.speed > 3.0),
             AVL_GSM: 4}
    io_2b = {AVL_EXT_VOLTAGE: f.ext_mv, AVL_TRACKER_BAT: 4100,
             AVL_SPEED: int(f.speed)}
    io_4b = {AVL_CAN_ODOMETER: f.odometer_m,          # counters report even
             AVL_CAN_FUEL_USED: int(f.fuel_used_l * 10),  # with ignition off
             AVL_CAN_ENGINE_MIN: f.engine_min,
             AVL_SERVICE_KM: f.service_km}
    io_xb: dict[int, bytes] = {}

    if f.ignition:
        # Live CAN parameters — the bus sleeps when the ignition is off
        io_1b[AVL_CAN_THROTTLE] = f.throttle
        io_1b[AVL_CAN_FUEL_PCT] = int(f.fuel_l / TANK_L * 100)
        io_1b[AVL_CAN_OIL_LEVEL] = 78
        io_2b[AVL_CAN_RPM] = f.rpm
        io_2b[AVL_CAN_SPEED] = int(f.speed)
        io_2b[AVL_CAN_COOLANT] = int(f.coolant_c * 10)
        io_2b[AVL_CAN_FUEL_L] = int(f.fuel_l * 10)
        io_2b[AVL_CAN_BATTERY] = f.can_batt_v
        io_2b[AVL_CAN_OIL_TEMP] = int(f.oil_c)
        io_2b[AVL_CAN_OIL_PRESS] = f.oil_press_kpa
        io_2b[AVL_CAN_AMBIENT] = f.ambient_c
        io_2b[AVL_RANGE_KM] = f.range_km
        if f.hv_pct is not None:
            io_1b[AVL_CAN_HV_PCT] = f.hv_pct

    if f.dtc_count:
        io_1b[AVL_DTC_COUNT] = f.dtc_count
    if f.dtc_codes:
        io_xb[AVL_DTC_CODES] = f.dtc_codes.encode("ascii")
    if f.send_vin:
        io_xb[AVL_VIN] = VIN.encode("ascii")
    if f.green:
        io_1b[AVL_GREEN_TYPE] = f.green[0]
        io_2b[AVL_GREEN_VALUE] = f.green[1]
    if f.overspeed:
        io_2b[AVL_OVERSPEED] = f.overspeed

    priority = 1 if (f.green or f.overspeed or f.dtc_codes) else 0
    event_id = (AVL_GREEN_TYPE if f.green else
                AVL_OVERSPEED if f.overspeed else
                AVL_DTC_CODES if f.dtc_codes else 0)
    return {"io_1b": io_1b, "io_2b": io_2b, "io_4b": io_4b, "io_xb": io_xb,
            "priority": priority, "event_id": event_id}


# ── Optional: register the car over the REST API ─────────────────────────────
def register_car(api: str, imei: str, name: str) -> None:
    body = {
        "name": name, "imei": imei, "device_type": "fmc150",
        "make": "Toyota", "model": "Corolla Altis", "year": 2019,
        "license_plate": "SMA1234A",
    }
    req = urllib.request.Request(
        api.rstrip("/") + "/api/v1/cars", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            car = json.load(resp)
        print(f"Registered car #{car['id']}: {car['name']} (FMC150, IMEI {imei})")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print(f"Car with IMEI {imei} already registered — streaming anyway.")
        else:
            print(f"Registration failed: HTTP {e.code} {e.read()!r}",
                  file=sys.stderr)
            raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"Cannot reach the API at {api} ({e.reason}). Is the app up?",
              file=sys.stderr)
        raise SystemExit(1)


# ── Main ──────────────────────────────────────────────────────────────────────
SCENARIO_DEFAULTS = {
    # overheat N=34: every record >105 °C and the run spans 330 s, so the
    # 300 s sustained rule starts its timer at the first fresh record (i≈4)
    # and fires at the last one — for any send interval 0–2 s, even with
    # several seconds of processing lag
    "commute": 54, "idle": 40, "overheat": 34, "weak_battery": 30,
    "dtc": 25, "service": 20, "burst": 50,
}


def main() -> int:
    # Windows pipes default to cp1252 — keep the ⚡/°/→ output alive
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description="PREDICT FMC150 (wired CAN tracker) car simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Scenarios:")[-1],
    )
    ap.add_argument("--imei", default="862462051234567")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5123)
    ap.add_argument("--scenario",
                    choices=sorted(SCENARIO_DEFAULTS), default="commute")
    ap.add_argument("--records", type=int, default=0,
                    help="override the scenario's default record count")
    ap.add_argument("--step", type=int, default=10,
                    help="virtual seconds between records (device send period)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="REAL seconds between packets (0 = as fast as possible)")
    ap.add_argument("--batch", type=int, default=1,
                    help="records per AVL packet (burst always sends one packet)")
    ap.add_argument("--hybrid", action="store_true",
                    help="also report HV battery charge (AVL 152)")
    ap.add_argument("--seed", type=int, default=7, help="jitter RNG seed")
    ap.add_argument("--register", action="store_true",
                    help="register the car via the REST API before streaming")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--name", default="Simulator — FMC150")
    args = ap.parse_args()

    n = args.records or SCENARIO_DEFAULTS[args.scenario]
    step = max(1, args.step)
    car = FMC150Car(args.scenario, args.hybrid, args.seed)

    if args.register:
        register_car(args.api, args.imei, args.name)

    # Virtual timestamps: spaced `step` apart, LAST record lands ~5 s AHEAD of
    # "now". The small future skew is harmless (monotonic, well inside the
    # send period) and buys the slack the server's 300 s freshness window
    # needs: without it, the record that starts a 300 s sustained-rule timer
    # sits exactly on the stale boundary and any processing lag kills it.
    # (burst: 2 h in the past — a store-and-forward flush; no rules fire).
    age = 7200 if args.scenario == "burst" else 0
    now_ms = int(time.time() * 1000) + (0 if args.scenario == "burst" else 5000)
    ts_of = lambda i: now_ms - age * 1000 - (n - 1 - i) * step * 1000

    frames = [car.step(i * step, step) for i in range(n)]
    records = [
        build_record(
            ts_of(i),
            lon=int(round(f.lon * 1e7)), lat=int(round(f.lat * 1e7)),
            alt=25, angle=f.heading, sats=9, speed=int(f.speed),
            **frame_to_io(f),
        )
        for i, f in enumerate(frames)
    ]

    packets = ([records] if args.scenario == "burst"
               else [records[i:i + args.batch]
                     for i in range(0, len(records), args.batch)])

    print(f"FMC150 simulator → {args.host}:{args.port} as IMEI {args.imei}")
    print(f"  scenario={args.scenario}  records={n}  step={step}s virtual  "
          f"interval={args.interval}s real  packets={len(packets)}")
    hints = {
        "overheat": "watch Repairs → Suggestions for the overheating task",
        "weak_battery": "watch for a 'Car battery low' issue",
        "dtc": "watch for fault-code issues (P0128, P0300)",
        "service": "watch for 'Service due soon'",
        "commute": "watch Home go green, then Driving for the trip + score",
        "idle": "watch Driving → notable moments for idling",
        "burst": "records stored, but NO issues should appear",
    }
    print(f"  → {hints[args.scenario]}")

    sock = socket.create_connection((args.host, args.port), timeout=15)
    sent = 0
    try:
        imei_b = args.imei.encode("ascii")
        sock.sendall(len(imei_b).to_bytes(2, "big") + imei_b)
        if sock.recv(1) != b"\x01":
            print("Server REJECTED the IMEI handshake", file=sys.stderr)
            return 1

        for p, packet_records in enumerate(packets):
            sock.sendall(build_packet(packet_records))
            ack = sock.recv(4)
            count = int.from_bytes(ack, "big", signed=True) if len(ack) == 4 else -1
            if count != len(packet_records):
                print(f"  ⚠ packet {p + 1}: ACK={count}, expected "
                      f"{len(packet_records)} — stopping", file=sys.stderr)
                return 1
            for f in frames[sent:sent + len(packet_records)]:
                extras = ""
                if f.green:
                    extras += f"  ⚡ eco-event type={f.green[0]}"
                if f.overspeed:
                    extras += f"  ⚡ overspeed {f.overspeed} km/h"
                if f.dtc_codes:
                    extras += f"  ⚡ DTCs {f.dtc_codes}"
                if f.send_vin:
                    extras += f"  VIN {VIN}"
                if (sent % 5 == 0 or extras or sent == n - 1):
                    state = "ON " if f.ignition else "off"
                    print(f"  [t+{f.t:4d}s] ign={state} {f.speed:5.1f} km/h "
                          f"{f.rpm:4d} rpm  cool={f.coolant_c:5.1f}°C "
                          f"oil={f.oil_c:5.1f}°C  fuel={f.fuel_l:4.1f}L "
                          f"odo={f.odometer_m / 1000:,.1f} km  ACK={count}"
                          f"{extras}")
                sent += 1
            if args.interval and p < len(packets) - 1:
                time.sleep(args.interval)

        print(f"Done: {n} record(s) ACKed in {len(packets)} packet(s).")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted — closing connection.")
        return 130
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
