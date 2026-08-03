"""Guard tests for the standalone Kuala Lumpur FMC150 drive simulator
(tools/kl_drive_sim.py). No server, no DB, no sockets: the tool is meant
to run completely outside the main code, so these tests just drive the
virtual car and check that every FMC150 sensor stays plausible all the
way from Mid Valley to KLCC."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import kl_drive_sim as sim  # noqa: E402

DT = 2.0

# Every sensor_type the FMC150 map defines while the engine is running
# (avl/fmc150.json, minus vin/dtc/dtc_count which are conditional).
RUNNING_SENSORS = {
    "gsm_signal", "battery_voltage", "tracker_battery_voltage",
    "vehicle_speed", "engine_rpm", "coolant_temperature",
    "vehicle_speed_obd", "throttle_position", "fuel_level",
    "fuel_level_liters", "fuel_consumed", "odometer", "engine_hours",
    "vehicle_battery_voltage", "ambient_air_temperature",
    "engine_oil_temperature", "engine_oil_pressure", "engine_oil_level",
    "distance_until_service", "remaining_distance",
}


def drive(hybrid=False, dtc=False, max_ticks=4000):
    """Run the whole drive; return (car, frames)."""
    car = sim.KLCar(hybrid, dtc, seed=7)
    frames, t = [], 0.0
    for _ in range(max_ticks):
        f = car.step(t, DT)
        frames.append(f)
        if f.done:
            break
        t += DT
    return car, frames


# ── Route sanity ──────────────────────────────────────────────────────────────
def test_route_legs_chain_and_stay_in_kl():
    for a, b in zip(sim.ROUTE, sim.ROUTE[1:]):
        # legs connect (within ~50 m — coordinates are real KL roads)
        assert sim.hav_m(a.lat1, a.lon1, b.lat0, b.lon0) < 50
        for lat, lon in ((a.lat0, a.lon0), (a.lat1, a.lon1)):
            assert 3.10 <= lat <= 3.18          # central Kuala Lumpur box
            assert 101.66 <= lon <= 101.76
    total = sum(l.length_m for l in sim.ROUTE)
    assert 10_000 <= total <= 20_000            # a believable cross-town drive


def test_route_mentions_real_places():
    names = " | ".join(l.name for l in sim.ROUTE)
    for place in ("KL Sentral", "Bukit Bintang", "Tun Razak", "AKLEH",
                  "Ampang", "KLCC", "Petronas"):
        assert place in names


# ── Full-drive behaviour ──────────────────────────────────────────────────────
def test_drive_completes_and_ignition_cycles():
    car, frames = drive()
    assert frames[-1].done                      # arrived at KLCC, parked
    assert any(f.ignition for f in frames)
    assert frames[-1].ignition is False         # keyed off at the drop-off
    assert car.driven_m > 10_000


def test_all_fmc150_sensors_present_while_running():
    _, frames = drive()
    running = [f for f in frames if f.ignition and f.sensors.get("engine_rpm")]
    assert len(running) > 200
    for f in running[::37]:                     # sample across the whole drive
        missing = RUNNING_SENSORS - set(f.sensors)
        assert not missing, f"missing sensors at t={f.t}: {missing}"


def test_sensor_values_stay_plausible():
    _, frames = drive()
    for f in frames:
        assert 3.10 <= f.lat <= 3.18
        assert 101.66 <= f.lon <= 101.76
        assert 0 <= f.heading <= 359
        assert 5 <= f.satellites <= 17
        s = f.sensors
        assert 0 <= s["vehicle_speed"] <= 100
        assert 10.5 <= s["battery_voltage"] <= 15.0
        if "coolant_temperature" in s:      # live CAN values, engine running
            assert 25 <= s["coolant_temperature"] <= 100
            assert s["engine_oil_temperature"] >= s["coolant_temperature"] - 40
            assert 0 <= s["throttle_position"] <= 90
            assert 0 <= s["engine_rpm"] <= 5000
            assert 0 < s["fuel_level_liters"] <= sim.TANK_L
    # counters never go backwards
    odo = [f.sensors["odometer"] for f in frames]
    assert all(b >= a for a, b in zip(odo, odo[1:]))


def test_device_events_fire_from_real_driving():
    car, frames = drive()
    kinds = {ev.kind for f in frames for ev in f.events}
    assert "harsh_brake" in kinds               # Bukit Bintang scramble
    assert "harsh_accel" in kinds               # Jalan Tun Razak merge
    assert "harsh_corner" in kinds              # AKLEH ramp
    assert "overspeed" in kinds                 # 92 km/h on the span
    assert car.overspeed_count >= 1
    # events also land in the raw AVL view, like the real device
    eco = [f.avl for f in frames if sim.AVL_GREEN_TYPE in f.avl]
    assert eco and {a[sim.AVL_GREEN_TYPE] for a in eco} == {1, 2, 3}
    assert any(sim.AVL_OVERSPEED in f.avl for f in frames)


def test_rain_cools_ambient_sensor():
    _, frames = drive()
    ambs = [f.sensors.get("ambient_air_temperature") for f in frames]
    ambs = [a for a in ambs if a is not None]
    assert max(ambs) >= 33.0                    # hot KL afternoon at the start
    assert min(ambs) <= 28.0                    # monsoon shower on Jalan Ampang


def test_parked_frames_drop_live_can_but_keep_counters():
    _, frames = drive()
    parked = [f for f in frames if not f.ignition]
    assert parked
    for f in parked:
        assert "engine_rpm" not in f.sensors    # CAN bus asleep
        assert "odometer" in f.sensors          # counters still report
        assert f.avl[sim.AVL_IGNITION] == 0


def test_vin_reported_once():
    _, frames = drive()
    assert frames[0].avl[sim.AVL_VIN] == sim.VIN
    assert all(sim.AVL_VIN not in f.avl for f in frames[1:])


def test_dtc_option_reports_code():
    car, frames = drive(dtc=True)
    assert car.dtcs == ["P0420"]
    coded = [f for f in frames if sim.AVL_DTC_CODES in f.avl]
    assert len(coded) == 1                      # codes sent once, then count
    later = [f for f in frames if f.t > coded[0].t and f.ignition]
    assert all(f.sensors.get("dtc_count") == 1 for f in later)


def test_hybrid_reports_hv_battery():
    _, frames = drive(hybrid=True)
    running = [f for f in frames if f.ignition]
    assert all(15 <= f.sensors["hv_battery_charge"] <= 95 for f in running)


# ── --stream wire format: round-trip through the project's own parser ────────
def test_stream_packets_roundtrip_through_server_parser():
    """The --stream path builds real Codec 8E packets. Feed them through the
    project's OWN codec8e parser + fmc150 AVL catalog — proving the KL sim
    speaks exactly what the production listener understands."""
    from server.catalog import decode_record
    from server.teltonika.codec8e import parse_avl_packet

    car = sim.KLCar(hybrid=False, dtc=True, seed=7)
    frames, t = [], 0.0
    for _ in range(500):                        # ~17 virtual minutes
        f = car.step(t, DT)
        frames.append(f)
        if f.done:
            break
        t += DT
    records = [
        sim.build_record(
            1_700_000_000_000 + i * int(DT * 1000),
            lon=int(round(f.lon * 1e7)), lat=int(round(f.lat * 1e7)),
            alt=int(f.alt), angle=f.heading, sats=f.satellites,
            speed=int(f.speed_gnss), **sim.frame_wire_groups(f),
        )
        for i, f in enumerate(frames)
    ]
    # Codec 8E record counts are one byte — chunk like a real device would
    parsed = []
    for i in range(0, len(records), 200):
        packet = sim.build_packet(records[i:i + 200])
        chunk, consumed = parse_avl_packet(packet)
        assert consumed == len(packet), "parser must consume the whole packet"
        parsed.extend(chunk)
    assert len(parsed) == len(frames)
    readings = [decode_record(r, "fmc150") for r in parsed]

    assert readings[0].vin == sim.VIN           # VIN sent once, X group
    assert all(r.vin is None for r in readings[1:])
    driving = next(r for r in readings if r.sensors.get("vehicle_speed_obd", 0) > 60)
    assert driving.sensors["engine_rpm"] > 1500
    assert driving.sensors["coolant_temperature"] > 40
    assert driving.sensors["odometer"] >= 62480
    events = {e["event_type"] for r in readings for e in r.events}
    assert "harsh_brake" in events              # Bukit Bintang scramble
    assert "harsh_accel" in events              # Jalan Tun Razak merge
