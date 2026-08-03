"""
Map VehicleState → Codec 8E SimRecord using FMC001 AVL IDs.

Scales match app/server/teltonika/avl/fmc001.json (copied as constants here so
this folder never imports the main project).
"""
from __future__ import annotations

from codec8e import SimRecord
from physics import VehicleState

# FMC001 AVL IDs
AVL_IGNITION = 239
AVL_MOVEMENT = 240
AVL_GSM = 21
AVL_BATTERY_MV = 66          # mV → V * 0.001
AVL_TRACKER_BATTERY_MV = 67
AVL_SPEED_GPS_IO = 24
AVL_ODOMETER_M = 16          # m → km * 0.001
AVL_VIN = 256                # ASCII X-group
AVL_DTC_COUNT = 30
AVL_ENGINE_LOAD = 31
AVL_COOLANT = 32
AVL_RPM = 36
AVL_SPEED_OBD = 37
AVL_INTAKE_AIR = 39
AVL_THROTTLE = 41
AVL_RUNTIME = 42
AVL_FUEL_LEVEL = 48
AVL_ECU_MV = 51              # mV → V * 0.001
AVL_AMBIENT = 53
AVL_OIL_TEMP = 58
AVL_FUEL_RATE = 60           # * 0.01 → L/h
AVL_DTC = 281                # ASCII X-group
AVL_DIST_SERVICE = 402
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
        AVL_ENGINE_LOAD: max(0, min(100, int(round(state.engine_load)))),
        AVL_COOLANT: max(0, min(215, int(round(state.coolant_c)))),
        AVL_RPM: max(0, int(round(state.rpm))),
        AVL_SPEED_OBD: max(0, min(255, int(round(state.speed_kmh)))),
        AVL_INTAKE_AIR: max(0, min(100, int(round(state.intake_air_c)))),
        AVL_THROTTLE: max(0, min(100, int(round(state.throttle)))),
        AVL_RUNTIME: max(0, int(round(state.runtime_s))),
        AVL_FUEL_LEVEL: max(0, min(100, int(round(state.fuel_level_pct)))),
        AVL_ECU_MV: max(0, int(round(state.ecu_v * 1000))),
        AVL_AMBIENT: max(0, min(100, int(round(state.ambient_c)))),
        AVL_OIL_TEMP: max(0, min(200, int(round(state.oil_temp_c)))),
        AVL_FUEL_RATE: max(0, int(round(state.fuel_rate_lph / 0.01))),
        AVL_DIST_SERVICE: max(0, int(round(state.distance_until_service_km))),
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
