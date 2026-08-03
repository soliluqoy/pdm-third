"""Rule engine unit tests: operators, sustained-duration timers, presets."""
from datetime import datetime, timedelta, timezone

from server import rules
from server.rules import OPERATORS, PRESETS, _duration_ok, _duration_reset


class TestOperators:
    def test_basic(self):
        assert OPERATORS[">"](5, 4)
        assert not OPERATORS[">"](4, 4)
        assert OPERATORS[">="](4, 4)
        assert OPERATORS["<"](3, 4)
        assert OPERATORS["<="](4, 4)

    def test_float_equality_uses_tolerance(self):
        assert OPERATORS["=="](13.8000001, 13.8)
        assert not OPERATORS["=="](13.9, 13.8)


class TestDurationTimers:
    def setup_method(self):
        rules._duration_since.clear()

    def test_zero_duration_fires_immediately(self):
        assert _duration_ok(1, 1, datetime.now(timezone.utc), 0)

    def test_sustained_window(self):
        t0 = datetime.now(timezone.utc)
        assert not _duration_ok(1, 1, t0, 120)          # window opens
        assert not _duration_ok(1, 1, t0 + timedelta(seconds=60), 120)
        assert _duration_ok(1, 1, t0 + timedelta(seconds=121), 120)

    def test_reset_clears_window(self):
        t0 = datetime.now(timezone.utc)
        assert not _duration_ok(2, 1, t0, 60)
        _duration_reset(2, 1)
        assert not _duration_ok(2, 1, t0 + timedelta(hours=1), 60)  # re-anchored


class TestPresets:
    def test_keys_unique(self):
        keys = [p["key"] for p in PRESETS]
        assert len(keys) == len(set(keys))

    def test_threshold_rules_have_required_fields(self):
        for p in PRESETS:
            if p["rule_type"].value == "threshold":
                assert p.get("sensor_type"), p["key"]
                assert p.get("operator") in OPERATORS, p["key"]
                assert p.get("threshold_value") is not None, p["key"]

    def test_preset_sensor_types_exist_in_maps(self):
        """No dormant presets: every threshold sensor_type is in at least one AVL map."""
        from server.catalog import IO_MAPS
        mapped = {
            e["sensor_type"]
            for io_map in IO_MAPS.values()
            for e in io_map.values()
        }
        for p in PRESETS:
            st = p.get("sensor_type")
            if st and p["rule_type"].value == "threshold":
                assert st in mapped, f"preset {p['key']} targets unmapped {st}"

    def test_urgent_rules_carry_recommendations(self):
        from server.models import Severity
        for p in PRESETS:
            if p.get("severity") in (Severity.CRITICAL, Severity.WARNING):
                assert p.get("recommendation"), f"{p['key']} has no plain-language advice"
