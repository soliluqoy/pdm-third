"""Catalog decode tests: AVL map → normalized readings (scale, meta, DTC, events)."""
from datetime import datetime, timezone

from server.catalog import decode_record, sensors_for_model
from server.teltonika.codec8e import AvlRecord


def _rec(io: dict) -> AvlRecord:
    return AvlRecord(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        priority=1, longitude=103.85, latitude=1.29, altitude=25,
        angle=90, satellites=9, speed=45, io=io,
    )


def test_fmc001_scales_and_meta():
    rd = decode_record(_rec({
        239: 1,        # ignition
        240: 0,        # movement
        32: 92,        # coolant °C
        66: 13800,     # mV → 13.8 V
        16: 80234567,  # m → 80234.567 km
    }), "fmc001")
    assert rd.ignition is True
    assert rd.movement is False
    assert rd.sensors["coolant_temperature"] == 92
    assert abs(rd.sensors["battery_voltage"] - 13.8) < 1e-6
    assert abs(rd.sensors["odometer"] - 80234.567) < 1e-3
    assert rd.units["coolant_temperature"] == "°C"


def test_fmc150_can_values():
    rd = decode_record(_rec({
        85: 3200,     # rpm
        115: 910,     # coolant ×0.1 → 91.0 °C
        89: 63,       # fuel %
        87: 123456789,  # m → km
    }), "fmc150")
    assert rd.sensors["engine_rpm"] == 3200
    assert abs(rd.sensors["coolant_temperature"] - 91.0) < 1e-6
    assert rd.sensors["fuel_level"] == 63
    assert abs(rd.sensors["odometer"] - 123456.789) < 1e-3


def test_dtc_split():
    rd = decode_record(_rec({281: b"P0128,P0300\x00"}), "fmc001")
    assert rd.dtcs == ["P0128", "P0300"]
    rd2 = decode_record(_rec({282: b"U0101"}), "fmc150")
    assert rd2.dtcs == ["U0101"]


def test_eco_driving_events():
    rd = decode_record(_rec({253: 2, 254: 78, 255: 132}), "fmc001")
    types = [e["event_type"] for e in rd.events]
    assert "harsh_brake" in types
    assert "speeding" in types
    speeding = next(e for e in rd.events if e["event_type"] == "speeding")
    assert speeding["value"] == 132


def test_vin_ascii_meta():
    rd = decode_record(_rec({256: b"1HGBH41JXMN109186\x00"}), "fmc001")
    assert rd.vin == "1HGBH41JXMN109186"
    rd2 = decode_record(_rec({325: b"WVWZZZ1KZAW123456"}), "fmc150")
    assert rd2.vin == "WVWZZZ1KZAW123456"


def test_unmapped_ids_are_ignored():
    rd = decode_record(_rec({239: 1, 9999: 42}), "fmc001")
    assert "9999" not in rd.sensors
    assert rd.ignition is True


def test_model_catalogs_are_normalized():
    """Both models expose the same normalized sensor_type for key quantities."""
    for model in ("fmc001", "fmc150"):
        types = {s["sensor_type"] for s in sensors_for_model(model)}
        for expected in ("engine_rpm", "coolant_temperature", "fuel_level",
                         "odometer", "distance_until_service"):
            assert expected in types, f"{model} missing {expected}"
