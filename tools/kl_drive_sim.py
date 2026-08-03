#!/usr/bin/env python3
"""
PREDICT — Kuala Lumpur real-world FMC150 drive simulator (STANDALONE).

A virtual Toyota Corolla 1.8G fitted with a Teltonika FMC150 wired CAN
tracker drives a real Kuala Lumpur route — Mid Valley Megamall →
Brickfields → KL Sentral → Masjid Jamek → Bukit Bintang → TRX →
Jalan Tun Razak → the AKLEH elevated highway → Ampang → and back
through the evening rain to the Petronas Twin Towers at KLCC — while
this script renders EVERY sensor the FMC150 reports (per
app/server/teltonika/avl/fmc150.json), live, in your terminal.

100% stdlib, zero imports from the app: a demonstration/teaching tool
that runs completely OUTSIDE the main code. By default it talks to
nothing — it just shows what the module itself reads. Pass --stream
(ideally with --register first) and every tick is ALSO sent to the
PREDICT server as a real Codec 8E packet (the production path), so the
drive appears live in the dashboard at http://localhost:8000.

    python tools/kl_drive_sim.py                  # live dashboard, 10x
    python tools/kl_drive_sim.py --rate 1         # true real-time
    python tools/kl_drive_sim.py --plain          # one log line per tick
    python tools/kl_drive_sim.py --quiet          # minimized: status + events only
    python tools/kl_drive_sim.py --avl            # + raw AVL IO values
    python tools/kl_drive_sim.py --hybrid         # Corolla Cross Hybrid
    python tools/kl_drive_sim.py --dtc            # check-engine mid-way
    python tools/kl_drive_sim.py --list-route     # print the itinerary
    python tools/kl_drive_sim.py --once --at 600  # single-frame snapshot
    python tools/kl_drive_sim.py --jsonl drive.jsonl   # export every tick
    python tools/kl_drive_sim.py --stream --register   # → PREDICT (quiet terminal)
    python tools/kl_drive_sim.py --stream --dashboard  # → PREDICT + full TTY board

Real-world flavour modelled along the way: car-park crawl, Brickfields
traffic lights, a KL Sentral pickup stop, the Bukit Bintang pedestrian
scramble (harsh brake), a spirited Jalan Tun Razak merge (harsh accel),
the tight AKLEH ramp (harsh cornering), 92 km/h on the elevated span
(device overspeed event), the Ampang toll-plaza queue, and a monsoon
downpour on Jalan Ampang that pulls the ambient sensor 33 °C → 26 °C.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ── FMC150 AVL IDs (mirror app/server/teltonika/avl/fmc150.json) ─────────────
AVL_IGNITION = 239          # 1B meta — 0/1
AVL_MOVEMENT = 240          # 1B meta — 0/1
AVL_GSM = 21                # 1B, 0-5
AVL_EXT_VOLTAGE = 66        # 2B, mV — tracker supply (car battery at the wire)
AVL_TRACKER_BAT = 67        # 2B, mV — internal backup battery
AVL_SPEED = 24              # 2B, km/h — GNSS-based
AVL_CAN_RPM = 85            # 2B
AVL_CAN_COOLANT = 115       # 2B, 0.1 °C (raw 910 = 91.0 °C)
AVL_CAN_SPEED = 81          # 2B, km/h
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

# ── Car / device identity ─────────────────────────────────────────────────────
PLATE = "WMQ 4712"
CAR_DESC = "Toyota Corolla 1.8G (2019)"
CAR_DESC_HYBRID = "Toyota Corolla Cross Hybrid (2022)"
VIN = "PN1BC3E3Z8G045123"  # 17 chars, UMW-assembled Toyota style
IMEI = "862462051234567"
TANK_L = 50.0

OVERSPEED_KMH = 90          # device Overspeeding feature threshold
HARSH_MS2 = 2.8             # Eco-driving harsh accel/brake/corner threshold
KEY_ON_WALL = 17 * 3600 + 5 * 60   # 5:05 pm — the evening rush home

# ── Geo helpers (WGS-84, good to ~1 m at city scale) ─────────────────────────
def hav_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    r = 6_371_000.0
    p0, p1 = math.radians(lat0), math.radians(lat1)
    dp, dl = math.radians(lat1 - lat0), math.radians(lon1 - lon0)
    a = math.sin(dp / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    p0, p1 = math.radians(lat0), math.radians(lat1)
    dl = math.radians(lon1 - lon0)
    y = math.sin(dl) * math.cos(p1)
    x = math.cos(p0) * math.sin(p1) - math.sin(p0) * math.cos(p1) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# ── Codec 8E frame building (independent copy — mirrors the wire spec; only
#    used with --stream, exactly like the physical device would talk) ─────────
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


# Which AVL IDs live in which Codec 8E IO group (per avl/fmc150.json sizes)
AVL_GROUP_1B = {AVL_IGNITION, AVL_MOVEMENT, AVL_GSM, AVL_CAN_THROTTLE,
                AVL_CAN_FUEL_PCT, AVL_CAN_HV_PCT, AVL_CAN_OIL_LEVEL,
                AVL_DTC_COUNT, AVL_GREEN_TYPE}
AVL_GROUP_2B = {AVL_EXT_VOLTAGE, AVL_TRACKER_BAT, AVL_SPEED, AVL_CAN_RPM,
                AVL_CAN_COOLANT, AVL_CAN_SPEED, AVL_CAN_FUEL_L,
                AVL_CAN_BATTERY, AVL_CAN_AMBIENT, AVL_CAN_OIL_TEMP,
                AVL_CAN_OIL_PRESS, AVL_RANGE_KM, AVL_GREEN_VALUE, AVL_OVERSPEED}
AVL_GROUP_4B = {AVL_CAN_ODOMETER, AVL_CAN_FUEL_USED, AVL_CAN_ENGINE_MIN,
                AVL_SERVICE_KM}
AVL_GROUP_XB = {AVL_VIN, AVL_DTC_CODES}        # variable-length ASCII


def frame_wire_groups(f: Frame) -> dict:
    """Split a Frame's raw AVL dict into Codec 8E IO groups for build_record,
    with the device event / priority flags the real module would set."""
    io_1b: dict[int, int] = {}
    io_2b: dict[int, int] = {}
    io_4b: dict[int, int] = {}
    io_xb: dict[int, bytes] = {}
    for k, v in f.avl.items():
        if k in AVL_GROUP_1B:
            io_1b[k] = int(v)
        elif k in AVL_GROUP_2B:
            io_2b[k] = int(v)
        elif k in AVL_GROUP_4B:
            io_4b[k] = int(v)
        elif k in AVL_GROUP_XB:
            io_xb[k] = str(v).encode("ascii")
    event_id = (AVL_GREEN_TYPE if AVL_GREEN_TYPE in f.avl else
                AVL_OVERSPEED if AVL_OVERSPEED in f.avl else
                AVL_DTC_CODES if AVL_DTC_CODES in f.avl else 0)
    return {"io_1b": io_1b, "io_2b": io_2b, "io_4b": io_4b, "io_xb": io_xb,
            "priority": 1 if event_id else 0, "event_id": event_id}


def register_car(api: str, imei: str, name: str) -> None:
    """Add the car over the REST API (Settings → Add car, without the UI)."""
    body = {
        "name": name, "imei": imei, "device_type": "fmc150",
        "make": "Toyota", "model": "Corolla Altis 1.8G", "year": 2019,
        "license_plate": PLATE.replace(" ", ""),
    }
    req = urllib.request.Request(
        api.rstrip("/") + "/api/v1/cars", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            car = json.load(resp)
        print(f"  registered car #{car['id']}: {car['name']} "
              f"(FMC150, IMEI {imei})")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print(f"  IMEI {imei} already registered — streaming anyway")
        else:
            print(f"  registration failed: HTTP {e.code} {e.read()!r}",
                  file=sys.stderr)
            raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"  cannot reach the API at {api} ({e.reason}) — is the app up?",
              file=sys.stderr)
        raise SystemExit(1)


# ── The Kuala Lumpur route ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Stop:
    frac: float             # fraction of the leg where the stop happens
    dwell_s: float          # how long we stand still (ignition on)
    hard: bool = False      # brake hard enough to fire a harsh-brake event


@dataclass(frozen=True)
class Leg:
    name: str
    lat0: float
    lon0: float
    lat1: float
    lon1: float
    kind: str               # crawl | city | arterial | highway | jam
    vmax: float             # km/h target cruise speed
    alt0: float = 56.0      # KL sits ~50-60 m above sea level…
    alt1: float = 56.0
    note: str = ""
    stops: tuple[Stop, ...] = ()
    curve_r: float = 0.0    # m — tight curve radius (cornering G → type 3)
    rain: bool = False
    canyon: bool = False    # urban canyon → fewer GNSS satellites

    @property
    def length_m(self) -> float:
        return hav_m(self.lat0, self.lon0, self.lat1, self.lon1)


# Coordinates are real KL: each leg is an actual road between actual places.
ROUTE: tuple[Leg, ...] = (
    Leg("Mid Valley Megamall — North Court driveway",
        3.1187, 101.6766, 3.1212, 101.6790, "crawl", 18,
        note="multi-storey car park exit, barrier queue"),
    Leg("Lingkaran Syed Putra",
        3.1212, 101.6790, 3.1260, 101.6845, "city", 45),
    Leg("Jalan Syed Putra — Brickfields",
        3.1260, 101.6845, 3.1310, 101.6895, "city", 50,
        note="traffic light @ Jalan Rozario",
        stops=(Stop(0.55, 16.0),)),
    Leg("Jalan Tun Sambanthan — KL Sentral pickup",
        3.1310, 101.6895, 3.1342, 101.6878, "crawl", 22,
        note="passenger pickup at the taxi stand",
        stops=(Stop(0.80, 24.0),), canyon=True),
    Leg("Jalan Tun Sambanthan — Little India",
        3.1342, 101.6878, 3.1395, 101.6925, "city", 40,
        stops=(Stop(0.40, 14.0),)),
    Leg("Jalan Hang Kasturi",
        3.1395, 101.6925, 3.1455, 101.6960, "city", 45),
    Leg("Jalan Tun Perak — Masjid Jamek",
        3.1455, 101.6960, 3.1492, 101.6968, "city", 40,
        note="LRT interchange junction",
        stops=(Stop(0.60, 18.0),)),
    Leg("Jalan Raja Chulan",
        3.1492, 101.6968, 3.1510, 101.7055, "city", 50),
    Leg("Jalan Bukit Bintang — Pavilion crossing",
        3.1510, 101.7055, 3.1482, 101.7135, "crawl", 25,
        note="pedestrian scramble — hard brake!",
        stops=(Stop(0.70, 20.0, hard=True),), canyon=True),
    Leg("Jalan Imbi — TRX",
        3.1482, 101.7135, 3.1440, 101.7180, "city", 45),
    Leg("Jalan Tun Razak (northbound)",
        3.1440, 101.7180, 3.1545, 101.7225, "arterial", 65,
        note="spirited merge onto the arterial"),
    Leg("AKLEH entry ramp — Jalan Jelatek",
        3.1545, 101.7225, 3.1578, 101.7285, "arterial", 46,
        alt1=68.0, note="tight elevated ramp", curve_r=55.0),
    Leg("AKLEH — Ampang–KL Elevated Highway",
        3.1578, 101.7285, 3.1612, 101.7375, "highway", 85,
        alt0=68.0, alt1=74.0),
    Leg("AKLEH — Sungai Kerayong span",
        3.1612, 101.7375, 3.1610, 101.7445, "highway", 92,
        alt0=74.0, alt1=72.0, note="limit 70 — driver keeps 92"),
    Leg("Ampang toll plaza — Touch 'n Go",
        3.1610, 101.7445, 3.1580, 101.7510, "crawl", 15,
        alt0=72.0, alt1=58.0, note="RFID lane queue",
        stops=(Stop(0.35, 10.0), Stop(0.60, 6.0))),
    Leg("Jalan Ampang — Ampang Point",
        3.1580, 101.7510, 3.1558, 101.7492, "city", 40,
        note="U-turn at Ampang Point — heading back to town"),
    Leg("Jalan Ampang (west) — Gleneagles",
        3.1558, 101.7492, 3.1590, 101.7360, "city", 45,
        rain=True, note="evening monsoon shower begins"),
    Leg("Jalan Ampang — Ampang Park crawl",
        3.1590, 101.7360, 3.1598, 101.7200, "jam", 14,
        rain=True, note="rush-hour jam in the rain", canyon=True),
    Leg("Jalan Ampang → Jalan P. Ramlee",
        3.1598, 101.7200, 3.1580, 101.7130, "city", 35),
    Leg("Suria KLCC — Petronas Twin Towers drop-off",
        3.1580, 101.7130, 3.1577, 101.7114, "crawl", 15,
        note="arrive: drop-off, idle, key off", canyon=True),
)

DESTINATION = "Suria KLCC — Petronas Twin Towers"


# ── One rendered tick ─────────────────────────────────────────────────────────
@dataclass
class Event:
    wall_s: int
    kind: str               # harsh_accel | harsh_brake | harsh_corner | overspeed | dtc | info
    text: str


@dataclass
class Frame:
    """Everything the FMC150 reports for one send period, normalized —
    keys match the project's sensor_type names in avl/fmc150.json."""
    t: float                       # virtual seconds since key-on
    wall_s: int                    # virtual wall clock (5:05 pm + t)
    road: str
    note: str
    ignition: bool
    movement: bool
    lat: float
    lon: float
    alt: float
    heading: int
    satellites: int
    speed_gnss: float
    sensors: dict                  # normalized sensor_type → value
    avl: dict                      # raw AVL ID → raw encoded value
    events: list = field(default_factory=list)   # events fired THIS tick
    progress: float = 0.0          # 0..1 of the whole route
    dist_to_dest_km: float = 0.0
    trip: dict = field(default_factory=dict)
    done: bool = False


# ── The virtual car ───────────────────────────────────────────────────────────
class KLCar:
    """Plausible physics over the scripted route: speed chases each leg's
    target with real accel/brake limits, gears and RPM follow road speed,
    coolant/oil warm up first-order, fuel burns, the monsoon cools the
    ambient sensor, and device-native eco-driving events fall out of the
    actual acceleration — nothing is faked per-sensor."""

    def __init__(self, hybrid: bool, dtc: bool, seed: int):
        self.hybrid = hybrid
        self.dtc_wanted = dtc
        self.rng = random.Random(seed)

        # Route position
        self.leg_i = 0
        self.leg_s = 0.0                 # metres into the current leg
        self.leg_entry_t = 0.0
        self.total_m = sum(l.length_m for l in ROUTE)
        self.driven_m = 0.0
        self._stops: list[dict] = []     # runtime copies for the current leg
        self._arm_stops()

        # Motion
        self.speed = 0.0                 # km/h
        self.heading = bearing_deg(ROUTE[0].lat0, ROUTE[0].lon0,
                                   ROUTE[0].lat1, ROUTE[0].lon1)
        self.dwell_left = 0.0
        self.arrived = False
        self.after_arrival_t = 0.0
        self.parked_ticks = 0

        # Car state (a six-year-old Corolla, well kept)
        self.ignition = True
        self.odometer_m = 62_480_000.0
        self.fuel_l = 29.5
        self.fuel_used_l = 3_980.4
        self.engine_min = 118_400.0
        self.service_km = 4_620.0
        self.coolant_c = 33.0            # heat-soaked in the car park
        self.oil_c = 33.0
        self.ambient_c = 33.4
        self.hv_pct = 78.0 if hybrid else None
        self.dtcs: list[str] = []
        self._dtc_reported = False

        # Event re-arm latches
        self._ha_armed = True
        self._hb_armed = True
        self._corner_done_legs: set[int] = set()
        self._overspeed_armed = True
        self._merge_boost_left = 0.0

        # Trip book-keeping
        self.trip_fuel_l = 0.0
        self.moving_s = 0.0
        self.max_speed = 0.0
        self.max_coolant = self.coolant_c
        self.harsh_count = 0
        self.overspeed_count = 0
        self.log: list[Event] = []

    # ── stops for the current leg ──
    def _arm_stops(self) -> None:
        leg = ROUTE[self.leg_i]
        self._stops = [{"frac": s.frac, "dwell": s.dwell_s,
                        "hard": s.hard, "done": False} for s in leg.stops]

    # ── target speed for this instant ──
    def _active_stop(self) -> tuple[dict | None, float]:
        """The next scripted stop on this leg, once we are inside its
        braking zone. The zone starts one stopping-distance ahead of the
        stop line (proportional control law below: cap-limited phase to
        v1 = cap/0.55, then an exponential tail), so the car halts right
        AT the line instead of overshooting into the next leg."""
        leg = ROUTE[self.leg_i]
        v = self.speed / 3.6
        for st in self._stops:
            if st["done"]:
                continue
            cap = 3.4 if st["hard"] else 2.2
            v1 = cap / 0.55
            d = ((v * v - v1 * v1) / (2 * cap) + v1 / 0.55) if v > v1 \
                else v / 0.55
            if self.leg_s >= st["frac"] * leg.length_m - (d + 2.0):
                return st, cap
        return None, 0.0

    def _target_speed(self, t: float) -> tuple[float, float]:
        """Return (target km/h, accel cap m/s²) for the current situation."""
        leg = ROUTE[self.leg_i]
        if self.dwell_left > 0:
            return 0.0, 3.6
        st, cap = self._active_stop()
        if st is not None:
            return 0.0, cap
        if leg.kind == "jam":
            # stop-and-go: creep 12 s, stand 14 s
            cyc = (t - self.leg_entry_t) % 26.0
            return (9.0 if cyc < 12.0 else 0.0), 2.2
        if self._merge_boost_left > 0:
            return leg.vmax + 8.0, 3.3     # kickdown merge → harsh accel
        return leg.vmax, 2.2

    # ── one virtual step of dt seconds ──
    def step(self, t: float, dt: float) -> Frame:
        rng = self.rng
        leg = ROUTE[self.leg_i]
        events: list[Event] = []
        wall = KEY_ON_WALL + int(t)

        # ── ignition / arrival state machine ──
        if self.arrived:
            self.after_arrival_t += dt
            if self.after_arrival_t > 15.0:
                self.ignition = False
                self.parked_ticks += dt

        # ── speed chase (proportional, capped at `cap` m/s²) ──
        target, cap = self._target_speed(t)
        if self.arrived:
            target, cap = 0.0, 3.6
        prev_speed = self.speed
        a = (target - self.speed) / 3.6 * 0.55          # m/s² demand
        a = max(-cap, min(cap, a))
        self.speed = max(0.0, self.speed + a * 3.6 * dt)
        if abs(self.speed - target) < 0.6:
            self.speed = target
        if self.dwell_left > 0:
            self.speed = 0.0
            self.dwell_left -= dt
        else:
            st, _ = self._active_stop()
            if st is not None and self.speed < 0.3:
                st["done"] = True                       # held at the line…
                self.dwell_left = st["dwell"]           # …for the dwell time
        if not self.ignition:
            self.speed = 0.0
        speed_ms = self.speed / 3.6
        accel = (self.speed - prev_speed) / 3.6 / dt if dt else 0.0

        # ── advance along the leg ──
        step_m = speed_ms * dt
        self.leg_s += step_m
        self.driven_m += step_m
        self.odometer_m += step_m
        self.service_km = max(0.0, self.service_km - step_m / 1000.0)
        if self.ignition:
            self.engine_min += dt / 60.0
        if self.speed > 3.0:
            self.moving_s += dt
        self.max_speed = max(self.max_speed, self.speed)

        if self.leg_s >= leg.length_m and not self.arrived:
            if self.leg_i < len(ROUTE) - 1:
                self.leg_i += 1
                self.leg_s = 0.0
                self.leg_entry_t = t
                leg = ROUTE[self.leg_i]
                self._arm_stops()
                if "merge" in leg.note:
                    self._merge_boost_left = 5.0
            else:
                self.arrived = True
                events.append(Event(wall, "info", f"arrived — {DESTINATION}"))

        self._merge_boost_left = max(0.0, self._merge_boost_left - dt)
        frac = min(1.0, self.leg_s / max(1.0, leg.length_m))

        # ── position, heading, altitude, satellites ──
        jitter = 1.2e-5 if self.speed > 5 else 3.0e-5     # ~1.3 m / ~3.3 m
        lat = leg.lat0 + (leg.lat1 - leg.lat0) * frac + rng.uniform(-jitter, jitter)
        lon = leg.lon0 + (leg.lon1 - leg.lon0) * frac + rng.uniform(-jitter, jitter)
        target_hdg = bearing_deg(leg.lat0, leg.lon0, leg.lat1, leg.lon1)
        turn = (target_hdg - self.heading + 540.0) % 360.0 - 180.0
        self.heading = (self.heading
                        + max(-28.0 * dt, min(28.0 * dt, turn))
                        + (rng.uniform(-1.5, 1.5) if self.speed > 3 else 0)) % 360.0
        alt = leg.alt0 + (leg.alt1 - leg.alt0) * frac
        sats = (7 if leg.canyon else 13 if leg.kind == "highway" else 11) \
            + rng.choice((-1, 0, 0, 1))

        # ── thermal model ──
        rain = leg.rain
        ambient_target = 26.5 if rain else 33.4
        self.ambient_c += (ambient_target - self.ambient_c) * (dt / 180.0)
        if self.ignition:
            cool_target = 89.0 + (6.0 if self.speed > 80 else 0.0) \
                - (2.0 if rain else 0.0)
            self.coolant_c += (cool_target - self.coolant_c) * (dt / 110.0)
            self.oil_c += (self.coolant_c + 13.0 - self.oil_c) * (dt / 300.0)
        else:
            self.coolant_c += (self.ambient_c - self.coolant_c) * (dt / 600.0)
            self.oil_c += (self.ambient_c - self.oil_c) * (dt / 900.0)
        self.max_coolant = max(self.max_coolant, self.coolant_c)

        # ── engine: gears, RPM, throttle, oil pressure ──
        moving = self.speed > 3.0
        if not self.ignition:
            gear, rpm, throttle, oil_press = 0, 0, 0, 0
        elif not moving:
            gear, rpm, throttle, oil_press = 0, 780, 0, 145
        else:
            bounds = (16.0, 36.0, 56.0, 76.0)
            gear = 1 + sum(self.speed > b for b in bounds)
            lo = (0.0,) + bounds
            hi = bounds + (140.0,)
            g_lo, g_hi = lo[gear - 1], hi[gear - 1]
            rpm = int(820 + (self.speed - g_lo) / max(1.0, g_hi - g_lo) * 2100
                      + max(0.0, accel) * 160)
            rpm = min(4600, max(820, rpm))
            throttle = int(min(90, max(6, 12 + accel * 20 + self.speed * 0.16)))
            oil_press = int(300 + rpm * 0.055)

        # ── electrical ──
        if not self.ignition:
            ext_v = 12.45 + rng.uniform(-0.04, 0.04)
        elif t < 1.6:
            ext_v = 10.9 + rng.uniform(-0.2, 0.2)       # crank dip
        elif not moving:
            ext_v = 13.9 + rng.uniform(-0.08, 0.08)
        else:
            ext_v = 14.15 + rng.uniform(-0.12, 0.12)
        can_batt = round(ext_v)
        gsm = 3 if rain else (5 if leg.kind == "highway" else 4)

        # ── fuel ──
        burn = (self.speed * 7.9 / 360_000.0
                + (0.00026 if self.ignition and not moving else 0.0)
                + max(0.0, accel) * self.speed * 9e-7) * dt
        self.fuel_l = max(2.0, self.fuel_l - burn)
        self.fuel_used_l += burn
        self.trip_fuel_l += burn
        range_km = int(self.fuel_l * 13.2)

        if self.hv_pct is not None:
            regen = 0.20 if accel < -1.5 else 0.0
            self.hv_pct = min(95.0, max(15.0,
                                        self.hv_pct - (0.02 if moving else 0.0) + regen))

        # ── device-native events (fall out of the actual acceleration) ──
        if accel >= HARSH_MS2 and self._ha_armed:
            self._ha_armed = False
            self.harsh_count += 1
            events.append(Event(wall, "harsh_accel",
                                f"harsh acceleration {accel:.2f} m/s²"))
        elif accel < 1.0:
            self._ha_armed = True
        if accel <= -HARSH_MS2 and self._hb_armed:
            self._hb_armed = False
            self.harsh_count += 1
            events.append(Event(wall, "harsh_brake",
                                f"harsh braking {-accel:.2f} m/s²"))
        elif accel > -1.0:
            self._hb_armed = True

        green: tuple[int, int] | None = None
        for ev in events:
            if ev.kind == "harsh_accel":
                green = (1, int(accel * 100))
            elif ev.kind == "harsh_brake":
                green = (2, int(-accel * 100))
        if leg.curve_r and self.leg_i not in self._corner_done_legs:
            lat_a = speed_ms * speed_ms / leg.curve_r
            if lat_a >= HARSH_MS2:
                self._corner_done_legs.add(self.leg_i)
                self.harsh_count += 1
                green = (3, int(lat_a * 100))
                events.append(Event(wall, "harsh_corner",
                                    f"harsh cornering {lat_a:.2f} m/s²"))

        overspeed = None
        if self.speed > OVERSPEED_KMH and self._overspeed_armed:
            self._overspeed_armed = False
            self.overspeed_count += 1
            overspeed = int(self.speed)
            events.append(Event(wall, "overspeed",
                                f"overspeed {int(self.speed)} km/h (limit 70)"))
        elif self.speed < OVERSPEED_KMH - 5:
            self._overspeed_armed = True

        # ── check-engine ──
        dtc_codes = None
        if (self.dtc_wanted and not self.dtcs
                and self.driven_m > 0.60 * self.total_m):
            self.dtcs = ["P0420"]
            dtc_codes = "P0420"
            events.append(Event(wall, "dtc",
                                "check engine — P0420 catalyst efficiency"))
        if self.dtcs and not self._dtc_reported:
            dtc_codes = ",".join(self.dtcs)
            self._dtc_reported = True

        # ── assemble the normalized sensor dict (keys = avl/fmc150.json) ──
        sensors: dict[str, float] = {
            "gsm_signal": gsm,
            "battery_voltage": round(ext_v, 3),
            "tracker_battery_voltage": 4.1,
            "vehicle_speed": round(self.speed, 1),
        }
        if self.ignition:
            sensors.update({
                "engine_rpm": rpm,
                "coolant_temperature": round(self.coolant_c, 1),
                "vehicle_speed_obd": round(self.speed, 1),
                "throttle_position": throttle,
                "fuel_level": int(self.fuel_l / TANK_L * 100),
                "fuel_level_liters": round(self.fuel_l, 1),
                "vehicle_battery_voltage": float(can_batt),
                "ambient_air_temperature": round(self.ambient_c, 1),
                "engine_oil_temperature": round(self.oil_c, 1),
                "engine_oil_pressure": oil_press,
                "engine_oil_level": 78,
                "remaining_distance": range_km,
            })
            if self.hv_pct is not None:
                sensors["hv_battery_charge"] = int(self.hv_pct)
        sensors.update({                          # counters report even parked
            "fuel_consumed": round(self.fuel_used_l, 1),
            "odometer": round(self.odometer_m / 1000.0, 3),
            "engine_hours": int(self.engine_min),
            "distance_until_service": int(self.service_km),
        })
        if self.dtcs:
            sensors["dtc_count"] = len(self.dtcs)

        # ── raw AVL IO view (exact wire encodings) ──
        avl: dict[int, int | str] = {
            AVL_IGNITION: int(self.ignition),
            AVL_MOVEMENT: int(self.speed > 3.0),
            AVL_GSM: gsm,
            AVL_EXT_VOLTAGE: int(ext_v * 1000),
            AVL_TRACKER_BAT: 4100,
            AVL_SPEED: int(self.speed),
            AVL_CAN_ODOMETER: int(self.odometer_m),
            AVL_CAN_FUEL_USED: int(self.fuel_used_l * 10),
            AVL_CAN_ENGINE_MIN: int(self.engine_min),
            AVL_SERVICE_KM: int(self.service_km),
        }
        if self.ignition:
            avl.update({
                AVL_CAN_THROTTLE: throttle,
                AVL_CAN_FUEL_PCT: int(self.fuel_l / TANK_L * 100),
                AVL_CAN_OIL_LEVEL: 78,
                AVL_CAN_RPM: rpm,
                AVL_CAN_SPEED: int(self.speed),
                AVL_CAN_COOLANT: int(self.coolant_c * 10),
                AVL_CAN_FUEL_L: int(self.fuel_l * 10),
                AVL_CAN_BATTERY: can_batt,
                AVL_CAN_OIL_TEMP: int(self.oil_c),
                AVL_CAN_OIL_PRESS: oil_press,
                AVL_CAN_AMBIENT: int(self.ambient_c),
                AVL_RANGE_KM: range_km,
            })
            if self.hv_pct is not None:
                avl[AVL_CAN_HV_PCT] = int(self.hv_pct)
        if self.dtcs:
            avl[AVL_DTC_COUNT] = len(self.dtcs)
        if dtc_codes:
            avl[AVL_DTC_CODES] = dtc_codes
        if t == 0:
            avl[AVL_VIN] = VIN
        if green:
            avl[AVL_GREEN_TYPE] = green[0]
            avl[AVL_GREEN_VALUE] = green[1]
        if overspeed:
            avl[AVL_OVERSPEED] = overspeed

        self.log.extend(events)
        trip = {
            "dist_km": self.driven_m / 1000.0,
            "moving_avg": (self.driven_m / 1000.0) / (self.moving_s / 3600.0)
            if self.moving_s else 0.0,
            "fuel_l": self.trip_fuel_l,
            "harsh": self.harsh_count,
            "overspeed": self.overspeed_count,
            "max_speed": self.max_speed,
            "max_coolant": self.max_coolant,
        }
        done = self.arrived and self.parked_ticks >= 3 * dt
        return Frame(
            t=t, wall_s=wall, road=leg.name, note=leg.note,
            ignition=self.ignition, movement=self.speed > 3.0,
            lat=lat, lon=lon, alt=alt, heading=int(self.heading),
            satellites=sats, speed_gnss=max(0.0, self.speed + rng.uniform(-1.2, 1.2)),
            sensors=sensors, avl=avl, events=events,
            progress=min(1.0, self.driven_m / self.total_m),
            dist_to_dest_km=max(0.0, (self.total_m - self.driven_m) / 1000.0),
            trip=trip, done=done,
        )


# ── Rendering helpers ─────────────────────────────────────────────────────────
def fmt_wall(wall_s: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(wall_s))


def fmt_t(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def bar(v: float, lo: float, hi: float, w: int = 14) -> str:
    n = int(round(w * max(0.0, min(1.0, (v - lo) / (hi - lo)))))
    return "█" * n + "░" * (w - n)


def _v(sensors: dict, key: str, fmt: str = "{}", dash: str = " --") -> str:
    return fmt.format(sensors[key]) if key in sensors else dash


def render_dashboard(f: Frame, car: KLCar, rate: float, show_avl: bool) -> str:
    s = f.sensors
    L: list[str] = []
    L.append("=" * 78)
    L.append(f" PREDICT · FMC150 real-world drive — Kuala Lumpur"
             f"{'':>3}{fmt_wall(f.wall_s)}  t+{fmt_t(f.t)}  ({rate:g}x)")
    L.append("-" * 78)
    L.append(f" {PLATE} · {CAR_DESC_HYBRID if car.hybrid else CAR_DESC}"
             f" · FMC150 IMEI {IMEI}")
    L.append(f" VIN {VIN} {'(reported)' if f.t > 0 else '(sending now)'}")
    L.append("-" * 78)
    L.append(" LOCATION")
    L.append(f"   {f.road}")
    if f.note:
        L.append(f"   >> {f.note}")
    L.append(f"   lat {f.lat:.5f}  lon {f.lon:.5f}  alt {f.alt:4.0f} m"
             f"   hdg {f.heading:3d}°   sats {f.satellites}")
    prog = bar(f.progress, 0.0, 1.0, 30)
    L.append(f"   route [{prog}] {f.progress * 100:3.0f}%"
             f"   to KLCC {f.dist_to_dest_km:4.1f} km")
    L.append("-" * 78)
    L.append(" POWERTRAIN")
    spd = s.get("vehicle_speed_obd", 0.0)
    L.append(f"   speed (CAN) {_v(s, 'vehicle_speed_obd', '{:5.1f}', '  -- ')} km/h"
             f" [{bar(spd, 0, 120)}] GNSS {f.speed_gnss:5.1f} km/h")
    gear = "P" if not f.ignition else ("N" if spd <= 3 else
           f"D{1 + sum(spd > b for b in (16, 36, 56, 76))}")
    rpm = s.get("engine_rpm", 0)
    L.append(f"   rpm {_v(s, 'engine_rpm', '{:5d}', '   --')} [{bar(rpm, 0, 5000)}]"
             f"  gear {gear:>2}  throttle {_v(s, 'throttle_position', '{:2d}', '--')}%")
    L.append(f"   coolant {_v(s, 'coolant_temperature', '{:5.1f}', '  -- ')} °C"
             f"   oil {_v(s, 'engine_oil_temperature', '{:5.1f}', '  -- ')} °C"
             f"   oil press {_v(s, 'engine_oil_pressure', '{:3d}', ' --')} kPa"
             f"   oil level {_v(s, 'engine_oil_level', '{:2d}', '--')}%")
    amb = s.get("ambient_air_temperature")
    raining = "  (rain)" if amb is not None and amb < 29.5 else ""
    L.append(f"   ambient {_v(s, 'ambient_air_temperature', '{:4.1f}', ' -- ')} °C{raining}")
    L.append("-" * 78)
    L.append(" FUEL & RANGE")
    L.append(f"   fuel {_v(s, 'fuel_level', '{:2d}', '--')}%"
             f" [{bar(s.get('fuel_level', 0), 0, 100)}]"
             f"  {_v(s, 'fuel_level_liters', '{:4.1f}', ' -- ')} L"
             f"   range {_v(s, 'remaining_distance', '{:3d}', ' --')} km")
    L.append(f"   consumed (lifetime) {_v(s, 'fuel_consumed', '{:,.1f}')} L")
    L.append("-" * 78)
    L.append(" ELECTRICAL & COMMS")
    L.append(f"   ext supply {_v(s, 'battery_voltage', '{:5.2f}')} V"
             f"   CAN battery {_v(s, 'vehicle_battery_voltage', '{:2.0f}', '--')} V"
             f"   tracker backup {_v(s, 'tracker_battery_voltage', '{:.2f}')} V")
    gsm = s.get("gsm_signal", 0)
    L.append(f"   GSM {gsm}/5 [{bar(gsm, 0, 5, 5)}]"
             f"   HV battery {_v(s, 'hv_battery_charge', '{:2d}', '--')}%")
    L.append("-" * 78)
    L.append(" COUNTERS & META")
    L.append(f"   odometer {_v(s, 'odometer', '{:,.1f}')} km"
             f"   engine {_v(s, 'engine_hours', '{:,}')} min"
             f"   service in {_v(s, 'distance_until_service', '{:,}')} km")
    L.append(f"   ignition {'ON ' if f.ignition else 'off'}"
             f"   movement {int(f.movement)}"
             f"   DTC {_v(s, 'dtc_count', '{:d}', '0')}"
             f"   codes {','.join(car.dtcs) if car.dtcs else 'none'}")
    L.append("-" * 78)
    L.append(" EVENTS (device-native: eco-driving / overspeed / DTC)")
    recent = car.log[-4:]
    if recent:
        for ev in recent:
            L.append(f"   {fmt_wall(ev.wall_s)}  {ev.text}")
    else:
        L.append("   (none yet)")
    L.append("-" * 78)
    tr = f.trip
    L.append(f" TRIP  {tr['dist_km']:.1f} km · moving avg {tr['moving_avg']:.0f} km/h"
             f" · fuel {tr['fuel_l']:.2f} L · max {tr['max_speed']:.0f} km/h"
             f" · harsh x{tr['harsh']}")
    if show_avl:
        L.append("-" * 78)
        L.append(" RAW AVL IO (this record, wire encoding)")
        items = "  ".join(f"{k}={v}" for k, v in sorted(f.avl.items()))
        # wrap at 74 cols
        line, out = "", []
        for tok in items.split("  "):
            if len(line) + len(tok) > 74:
                out.append(line)
                line = ""
            line += ("  " if line else "   ") + tok
        if line:
            out.append(line)
        L.extend(out)
    L.append("=" * 78)
    if f.done:
        L.append(f" ARRIVED — {DESTINATION}. Ignition off, CAN bus asleep.")
    else:
        L.append(" Ctrl-C to stop")
    return "\n".join(L)


def render_plain(f: Frame, car: KLCar) -> str:
    s = f.sensors
    extra = ""
    for ev in f.events:
        extra += f"  !! {ev.text}"
    return (f"[{fmt_wall(f.wall_s)} t+{fmt_t(f.t)}] {f.road[:44]:44s} "
            f"ign={'ON ' if f.ignition else 'off'} "
            f"{s.get('vehicle_speed_obd', 0):5.1f}km/h "
            f"rpm={s.get('engine_rpm', 0):4d} thr={s.get('throttle_position', 0):2d}% "
            f"cool={_v(s, 'coolant_temperature', '{:5.1f}', '  -- ')}C "
            f"oil={_v(s, 'engine_oil_temperature', '{:5.1f}', '  -- ')}C "
            f"amb={_v(s, 'ambient_air_temperature', '{:4.1f}', ' -- ')}C "
            f"fuel={_v(s, 'fuel_level_liters', '{:4.1f}', ' -- ')}L "
            f"ext={_v(s, 'battery_voltage', '{:5.2f}')}V "
            f"odo={s.get('odometer', 0):,.1f}km "
            f"({f.lat:.5f},{f.lon:.5f}){extra}")


def render_quiet_status(f: Frame, car: KLCar, rate: float, streaming: bool) -> str:
    """One compact heartbeat line for minimized terminal output."""
    s = f.sensors
    tr = f.trip
    dest = "streaming → dashboard" if streaming else "local only"
    return (f"\r  [{fmt_wall(f.wall_s)} t+{fmt_t(f.t)}] "
            f"{f.progress * 100:3.0f}%  "
            f"{s.get('vehicle_speed_obd', 0):5.1f} km/h  "
            f"{f.road[:36]:36s}  "
            f"harsh×{tr['harsh']} over×{tr['overspeed']}  "
            f"{rate:g}x · {dest}   ")


def render_quiet_event(ev: Event) -> str:
    return f"  !! [{fmt_wall(ev.wall_s)}] {ev.kind}: {ev.text}"


def frame_to_json(f: Frame) -> str:
    return json.dumps({
        "t_virtual_s": round(f.t, 1),
        "wall_clock": fmt_wall(f.wall_s),
        "road": f.road,
        "ignition": f.ignition,
        "movement": f.movement,
        "gps": {"lat": round(f.lat, 6), "lon": round(f.lon, 6),
                "alt_m": round(f.alt, 1), "heading": f.heading,
                "satellites": f.satellites,
                "speed_kmh": round(f.speed_gnss, 1)},
        "sensors": f.sensors,
        "avl_raw": {str(k): v for k, v in sorted(f.avl.items())},
        "events": [{"kind": e.kind, "text": e.text} for e in f.events],
        "progress": round(f.progress, 4),
    })


def list_route() -> None:
    total = 0.0
    print(f"Kuala Lumpur route — {len(ROUTE)} legs, "
          f"{PLATE} {CAR_DESC}, destination {DESTINATION}\n")
    for i, leg in enumerate(ROUTE, 1):
        d = leg.length_m
        total += d
        stops = f"  stops: {len(leg.stops)}" if leg.stops else ""
        rain = "  RAIN" if leg.rain else ""
        print(f"  {i:2d}. {leg.name:48s} {d / 1000:5.2f} km  "
              f"{leg.kind:8s} ~{leg.vmax:2.0f} km/h{stops}{rain}")
    print(f"\n  total {total / 1000:.1f} km")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    # Windows pipes default to cp1252 — keep °/█/→ output alive.
    # Also force line buffering so --quiet status shows up when stdout
    # is redirected (Cursor / CI capture), not only on a real TTY.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass
    try:
        os.system("")          # enable VT100 processing on Windows 10+
    except OSError:
        pass

    ap = argparse.ArgumentParser(
        description="Standalone FMC150 Kuala Lumpur drive simulator — "
                    "renders every FMC150 sensor live. No server needed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Real-world flavour")[-1],
    )
    ap.add_argument("--rate", type=float, default=10.0,
                    help="virtual seconds per REAL second (1 = real-time, "
                         "default 10)")
    ap.add_argument("--dt", type=float, default=2.0,
                    help="virtual seconds per tick (device send period is 10 s; "
                         "2 s gives a smoother dashboard)")
    ap.add_argument("--seed", type=int, default=7, help="jitter RNG seed")
    ap.add_argument("--hybrid", action="store_true",
                    help="drive a Corolla Cross Hybrid (adds HV battery %%)")
    ap.add_argument("--dtc", action="store_true",
                    help="check-engine light (P0420) at 60%% of the route")
    ap.add_argument("--plain", action="store_true",
                    help="one log line per tick instead of the dashboard")
    ap.add_argument("--quiet", action="store_true",
                    help="minimized terminal: updating status line + event "
                         "lines only (default when --stream)")
    ap.add_argument("--dashboard", action="store_true",
                    help="force the full TTY dashboard (even with --stream)")
    ap.add_argument("--avl", action="store_true",
                    help="dashboard: also show the raw AVL IO values")
    ap.add_argument("--frames", type=int, default=0,
                    help="stop after N ticks (0 = drive until arrival)")
    ap.add_argument("--once", action="store_true",
                    help="render a single dashboard snapshot and exit")
    ap.add_argument("--at", type=float, default=600.0,
                    help="virtual second for --once (default 600)")
    ap.add_argument("--jsonl", metavar="PATH",
                    help="export every tick as JSON lines")
    ap.add_argument("--list-route", action="store_true",
                    help="print the itinerary and exit")
    ap.add_argument("--stream", action="store_true",
                    help="ALSO send every tick to the PREDICT server as real "
                         "Codec 8E over TCP (the production path) — the drive "
                         "shows up live in the dashboard")
    ap.add_argument("--register", action="store_true",
                    help="register the car via the REST API before streaming")
    ap.add_argument("--imei", default=IMEI)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5123)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--name", default="KL Drive — Corolla (FMC150)")
    args = ap.parse_args()

    if args.list_route:
        list_route()
        return 0

    # Display mode: dashboard (full) | plain (every tick) | quiet (minimized)
    # --stream defaults to quiet so the terminal stays small while the
    # web dashboard shows the live car.
    if args.once or args.dashboard:
        mode = "dashboard"
    elif args.plain:
        mode = "plain"
    elif args.quiet or args.stream:
        mode = "quiet"
    elif sys.stdout.isatty():
        mode = "dashboard"
    else:
        mode = "plain"
    use_dashboard = mode == "dashboard"
    use_quiet = mode == "quiet"
    dt = max(0.5, args.dt)
    car = KLCar(args.hybrid, args.dtc, args.seed)
    out = open(args.jsonl, "w", encoding="utf-8") if args.jsonl else None

    if args.once:
        t = 0.0
        f = car.step(0.0, dt)
        while f.t < args.at and not f.done:
            t += dt
            f = car.step(t, dt)
        print(render_dashboard(f, car, args.rate, args.avl or True))
        if out:
            out.write(frame_to_json(f) + "\n")
            out.close()
        return 0

    sock = None
    if args.stream:
        if args.register:
            register_car(args.api, args.imei, args.name)
        try:
            sock = socket.create_connection((args.host, args.port), timeout=15)
            imei_b = args.imei.encode("ascii")
            sock.sendall(len(imei_b).to_bytes(2, "big") + imei_b)
            if sock.recv(1) != b"\x01":
                print("  server REJECTED the IMEI handshake", file=sys.stderr)
                sock.close()
                return 1
        except OSError as e:
            print(f"  cannot stream to {args.host}:{args.port} ({e}) — "
                  f"continuing local-only", file=sys.stderr)
            sock = None

    mode_label = {"dashboard": "dashboard", "plain": "plain log",
                  "quiet": "quiet (minimized)"}[mode]
    print(f"FMC150 KL drive — {PLATE} {CAR_DESC_HYBRID if args.hybrid else CAR_DESC}")
    print(f"  route: Mid Valley → KL Sentral → Bukit Bintang → Jalan Tun Razak "
          f"→ AKLEH → Ampang → KLCC ({car.total_m / 1000:.1f} km)")
    print(f"  rate {args.rate:g}x · tick {dt:g}s virtual · {mode_label}"
          f"{' · jsonl → ' + args.jsonl if out else ''}"
          f"{f' · streaming → {args.host}:{args.port} (Codec 8E)' if sock else ''}")
    if sock:
        print(f"  → watch it live on http://localhost:8000 — Home / Car / Driving")
        print(f"  → terminal stays minimized (events + status only); "
              f"use --dashboard for the full board")
    if use_quiet:
        print("  status line updates in-place; !! lines are trigger flags")
    if use_dashboard:
        time.sleep(1.6)

    t, n = 0.0, 0
    try:
        while True:
            f = car.step(t, dt)
            n += 1
            if out:
                out.write(frame_to_json(f) + "\n")
            if use_dashboard:
                sys.stdout.write("\033[2J\033[H" + render_dashboard(
                    f, car, args.rate, args.avl) + "\n")
                sys.stdout.flush()
            elif use_quiet:
                if f.events:
                    if sys.stdout.isatty():
                        sys.stdout.write("\n")
                    for ev in f.events:
                        print(render_quiet_event(ev))
                # TTY: in-place status. Piped/log: heartbeat every ~10 s virtual.
                if sys.stdout.isatty():
                    sys.stdout.write(render_quiet_status(
                        f, car, args.rate, sock is not None))
                    sys.stdout.flush()
                elif int(f.t) % 10 < dt or f.done or f.events:
                    print(render_quiet_status(
                        f, car, args.rate, sock is not None).lstrip("\r"))
            else:
                print(render_plain(f, car))
            if sock:
                # Stream with REAL timestamps (now + 5 s skew, like the
                # physical device) — virtual time may run 10x fast, and the
                # server's freshness window must accept every record.
                try:
                    rec = build_record(
                        int(time.time() * 1000) + 5000,
                        lon=int(round(f.lon * 1e7)),
                        lat=int(round(f.lat * 1e7)),
                        alt=int(f.alt), angle=f.heading, sats=f.satellites,
                        speed=int(f.speed_gnss), **frame_wire_groups(f))
                    sock.sendall(build_packet([rec]))
                    ack = sock.recv(4)
                    if len(ack) != 4 or int.from_bytes(ack, "big",
                                                       signed=True) != 1:
                        raise OSError(f"bad ACK {ack!r}")
                except OSError as e:
                    print(f"  !! stream lost ({e}) — continuing local-only",
                          file=sys.stderr)
                    sock.close()
                    sock = None
            if f.done or (args.frames and n >= args.frames):
                break
            t += dt
            time.sleep(dt / max(0.1, args.rate))
    except KeyboardInterrupt:
        print("\nStopped by driver (Ctrl-C).")
    finally:
        if out:
            out.close()
        if sock:
            sock.close()

    tr = f.trip
    print("\n── Trip summary ─────────────────────────────────────────────")
    print(f"  {PLATE} — Mid Valley Megamall → {DESTINATION}")
    print(f"  distance {tr.get('dist_km', 0):.1f} km"
          f" · drive time {fmt_t(f.t)} (virtual)"
          f" · moving avg {tr.get('moving_avg', 0):.0f} km/h")
    print(f"  fuel used {tr.get('fuel_l', 0):.2f} L"
          f" · top speed {tr.get('max_speed', 0):.0f} km/h"
          f" · peak coolant {tr.get('max_coolant', 0):.1f} °C")
    print(f"  eco-driving events: {tr.get('harsh', 0)} harsh"
          f" · {tr.get('overspeed', 0)} overspeed"
          f" · DTCs: {', '.join(car.dtcs) if car.dtcs else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
