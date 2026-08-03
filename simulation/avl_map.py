"""
Map VehicleState → Codec 8E SimRecord using FMC150 AVL IDs / scales.

Mirrors app/server/teltonika/avl/fmc150.json (copied as constants — this folder
never imports the main project).
"""
from __future__ import annotations

from codec8e import SimRecord
from physics import VehicleState

# FMC150 AVL IDs
AVL_IGNITION = 239
AVL_MOVEMENT = 240
AVL_GSM = 21
AVL_BATTERY_MV = 66
AVL_TRACKER_BATTERY_MV = 67
AVL_SPEED_GPS_IO = 24
AVL_ODOMETER_M = 87          # m → km * 0.001
AVL_VIN = 325
AVL_DTC_COUNT = 160
AVL_ENGINE_RPM = 85
AVL_COOLANT = 115            # raw * 0.1 → °C
AVL_SPEED_OBD = 81
AVL_THROTTLE = 82
AVL_FUEL_LEVEL = 89
AVL_FUEL_LEVEL_LITERS = 84   # raw * 0.1 → L
AVL_FUEL_CONSUMED = 83       # raw * 0.1 → L
AVL_ENGINE_HOURS = 102       # minutes
AVL_VEHICLE_BATTERY = 168    # V
AVL_HV_BATTERY = 152
AVL_AMBIENT = 1396
AVL_OIL_TEMP = 1270
AVL_OIL_PRESSURE = 1158
AVL_OIL_LEVEL = 1159
AVL_DTC = 282
AVL_DIST_SERVICE = 400
AVL_REMAINING_DIST = 866
AVL_GREEN_TYPE = 253
AVL_GREEN_VALUE = 254
AVL_OVERSPEED = 255


def state_to_record(state: VehicleState, *, event_id: int = 0) -> SimRecord:
    io: dict[int, int | bytes] = {
        AVL_IGNITION: 1 if state.ignition else 0,
        AVL_MOVEMENT: 1 if state.movement else 0,
        AVL_GSM: 4,
        AVL_BATTERY_MV: max(0, int(round(state.battery_v * 1000))),
        AVL_TRACKER_BATTERY_MV: max(0, int(round(state.tracker_battery_v * 1000))),
        AVL_SPEED_GPS_IO: max(0, min(255, int(round(state.speed_kmh)))),
        AVL_ODOMETER_M: max(0, int(round(state.odometer_km * 1000))),
        AVL_ENGINE_RPM: max(0, int(round(state.rpm))),
        AVL_COOLANT: max(0, int(round(state.coolant_c * 10))),
        AVL_SPEED_OBD: max(0, min(255, int(round(state.speed_kmh)))),
        AVL_THROTTLE: max(0, min(100, int(round(state.throttle)))),
        AVL_FUEL_LEVEL: max(0, min(100, int(round(state.fuel_level_pct)))),
        AVL_FUEL_LEVEL_LITERS: max(0, int(round(state.fuel_level_liters * 10))),
        AVL_FUEL_CONSUMED: max(0, int(round(state.fuel_consumed_l * 10))),
        AVL_ENGINE_HOURS: max(0, int(round(state.engine_hours_min))),
        AVL_VEHICLE_BATTERY: max(0, int(round(state.vehicle_battery_v))),
        AVL_HV_BATTERY: max(0, min(100, int(round(state.hv_battery_charge_pct)))),
        AVL_AMBIENT: int(round(state.ambient_c)),
        AVL_OIL_TEMP: max(0, int(round(state.oil_temp_c))),
        AVL_OIL_PRESSURE: max(0, int(round(state.engine_oil_pressure_kpa))),
        AVL_OIL_LEVEL: max(0, min(100, int(round(state.engine_oil_level_pct)))),
        AVL_DIST_SERVICE: max(0, int(round(state.distance_until_service_km))),
        AVL_REMAINING_DIST: max(0, int(round(state.remaining_distance_km))),
        AVL_DTC_COUNT: len(state.dtc_codes),
    }

    if state.vin:
        io[AVL_VIN] = state.vin.encode("ascii")[:17]

    if state.dtc_codes:
        io[AVL_DTC] = ",".join(state.dtc_codes).encode("ascii")

    if state.green_driving_type is not None:
        io[AVL_GREEN_TYPE] = int(state.green_driving_type)
        io[AVL_GREEN_VALUE] = int(state.green_driving_value or 0)
        event_id = event_id or AVL_GREEN_TYPE

    if state.overspeed_kmh is not None and state.overspeed_kmh > 0:
        io[AVL_OVERSPEED] = int(state.overspeed_kmh)
        event_id = event_id or AVL_OVERSPEED

    return SimRecord(
        timestamp=state.ts,
        latitude=state.lat,
        longitude=state.lon,
        altitude=state.altitude_m,
        angle=int(state.heading_deg) % 360,
        satellites=state.satellites,
        speed=max(0, int(round(state.speed_kmh))),
        event_id=event_id,
        io=io,
    )
