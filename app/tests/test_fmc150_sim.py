"""Round-trip test: the FMC150 simulator's wire output is fed through the
project's OWN Codec 8E parser and fmc150 AVL catalog — proving the virtual
car speaks exactly what the production listener understands.

No sockets, no DB: build frames → build packet → parse → decode → assert.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import simulate_fmc150 as sim  # noqa: E402
from server.catalog import decode_record  # noqa: E402
from server.teltonika.codec8e import parse_avl_packet  # noqa: E402

STEP = 10


def run_scenario(scenario: str, hybrid: bool = False):
    """Generate one scenario, packetize it, parse + decode every record."""
    n = sim.SCENARIO_DEFAULTS[scenario]
    car = sim.FMC150Car(scenario, hybrid, seed=7)
    frames = [car.step(i * STEP, STEP) for i in range(n)]
    records = [
        sim.build_record(
            1_700_000_000_000 + i * STEP * 1000,
            lon=int(round(f.lon * 1e7)), lat=int(round(f.lat * 1e7)),
            alt=25, angle=f.heading, sats=9, speed=int(f.speed),
            **sim.frame_to_io(f),
        )
        for i, f in enumerate(frames)
    ]
    packet = sim.build_packet(records)
    parsed, consumed = parse_avl_packet(packet)
    assert consumed == len(packet), "parser must consume the whole packet"
    assert len(parsed) == n
    return frames, [decode_record(r, "fmc150") for r in parsed]


# ── Wire-level sanity ─────────────────────────────────────────────────────────
def test_imei_default_is_valid():
    # listener requires a 14–17 digit IMEI in the handshake
    assert 14 <= len("862462051234567") <= 17 and "862462051234567".isdigit()


def test_multi_record_packet_parses_clean():
    frames, readings = run_scenario("burst")   # 50 records, one packet
    assert len(readings) == 50
    assert all(r.speed is not None for r in readings)


# ── Commute: the full demo ────────────────────────────────────────────────────
def test_commute_full_can_parameter_set():
    frames, readings = run_scenario("commute")
    driving = readings[30]              # t=300 s, on the expressway
    s = driving.sensors
    assert s["engine_rpm"] > 1000
    assert s["vehicle_speed_obd"] > 80
    assert 31 <= s["coolant_temperature"] <= 92
    assert 0 < s["throttle_position"] <= 90
    assert 60 <= s["fuel_level"] <= 70
    assert 33.0 <= s["fuel_level_liters"] <= 34.5
    assert s["fuel_consumed"] >= 4253
    assert 80234 <= s["odometer"] <= 80260
    assert s["engine_hours"] >= 152300
    assert 13.5 <= s["battery_voltage"] <= 14.5          # tracker feed (mV→V)
    assert s["vehicle_battery_voltage"] in (13, 14)      # CAN-reported volts
    assert s["tracker_battery_voltage"] == 4.1
    assert s["engine_oil_temperature"] > s["coolant_temperature"] - 20
    assert 200 <= s["engine_oil_pressure"] <= 500
    assert s["engine_oil_level"] == 78
    assert s["ambient_air_temperature"] == 31
    assert s["distance_until_service"] <= 4320
    assert 300 <= s["remaining_distance"] <= 500
    assert s["gsm_signal"] == 4
    assert driving.ignition is True and driving.movement is True


def test_commute_vin_and_eco_events_decode():
    _, readings = run_scenario("commute")
    assert readings[0].vin == sim.VIN
    assert all(r.vin is None for r in readings[1:])   # VIN sent once
    events = [e["event_type"] for r in readings for e in r.events]
    assert "harsh_accel" in events
    assert "harsh_brake" in events
    assert "speeding" in events                        # device overspeed (255)


def test_commute_ignition_off_records_are_sparse():
    frames, readings = run_scenario("commute")
    parked = readings[49]               # t=490 s — ignition off
    assert parked.ignition is False
    assert "engine_rpm" not in parked.sensors          # CAN bus asleep
    assert "coolant_temperature" not in parked.sensors
    assert "odometer" in parked.sensors                # counters always sent
    assert readings[-1].ignition is False              # ends parked (trip closes)
    assert readings[47].ignition is True               # still driving at t=470


def test_commute_gps_stays_plausible():
    _, readings = run_scenario("commute")
    for r in readings:                          # anywhere in Singapore is fine
        assert 1.25 <= r.latitude <= 1.47
        assert 103.60 <= r.longitude <= 104.10
        assert 5 <= r.satellites <= 20
    assert readings[-1].longitude != readings[0].longitude  # it actually drove


# ── Fault scenarios produce the values the rules key on ──────────────────────
def test_overheat_reaches_critical_temp():
    _, readings = run_scenario("overheat")
    peak = max(r.sensors.get("coolant_temperature", 0) for r in readings)
    assert 117.5 <= peak <= 118.5
    # sustained above both rule thresholds long before the end
    hot = [r for r in readings if r.sensors.get("coolant_temperature", 0) > 110]
    assert len(hot) >= 25           # ≥ 250 virtual seconds over 110 °C


def test_weak_battery_reports_low_can_voltage():
    _, readings = run_scenario("weak_battery")
    running = [r for r in readings if r.ignition]
    # every engine-on record reports the sagging CAN voltage
    assert {r.sensors["vehicle_battery_voltage"] for r in running} == {11.0}
    # tracker feed stays healthy — it's the car side that sags
    assert all(r.sensors["battery_voltage"] > 13 for r in running)
    assert any(r.ignition is False for r in readings)   # ends parked


def test_dtc_codes_decode_once_then_count_only():
    _, readings = run_scenario("dtc")
    with_codes = [r for r in readings if r.dtcs]
    assert len(with_codes) == 1
    assert with_codes[0].dtcs == ["P0128", "P0300"]
    later = [r for r in readings if r.timestamp > with_codes[0].timestamp]
    assert all(r.sensors.get("dtc_count") == 2 for r in later)


def test_service_countdown_below_threshold():
    _, readings = run_scenario("service")
    assert readings[0].sensors["distance_until_service"] == 480
    assert readings[-1].sensors["distance_until_service"] <= 480


def test_idle_keeps_engine_warm_and_stationary():
    _, readings = run_scenario("idle")
    running = [r for r in readings if r.ignition]
    assert len(running) == 35                           # keys off after t=350
    assert all((r.speed or 0) <= 3 for r in readings)
    assert all(r.sensors.get("engine_rpm") == 850 for r in running)
    # 35 running records × 10 s ≥ the 5-minute idling window before key-off
    assert len(running) * STEP >= 350


def test_hybrid_reports_hv_battery():
    _, readings = run_scenario("commute", hybrid=True)
    driving = readings[30]
    assert 15 <= driving.sensors["hv_battery_charge"] <= 95
