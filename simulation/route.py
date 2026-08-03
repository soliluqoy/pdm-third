"""
Kuala Lumpur waypoint polylines for the KL Grind taxi shift.

Positions are approximate public landmarks — good enough for a continuous
great-circle path on the dashboard map.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


_R_KM = 6371.0


@dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float
    alt_m: float = 40.0
    label: str = ""


# Narrative legs (order matters)
PJ_START = Waypoint(3.1073, 101.6067, 45, "Petaling Jaya")
FEDERAL_HWY = Waypoint(3.1125, 101.6420, 40, "Federal Highway")
KLCC = Waypoint(3.1579, 101.7116, 50, "KLCC")
BANGSAR_MAMAK = Waypoint(3.1308, 101.6700, 40, "Bangsar mamak")
MRR2_NORTH = Waypoint(3.1980, 101.7200, 55, "MRR2 north")
MRR2_EAST = Waypoint(3.1750, 101.7550, 50, "MRR2 east")
PETRONAS_MIDVALLEY = Waypoint(3.1178, 101.6770, 40, "Petronas Mid Valley")
NSE_RAWANG = Waypoint(3.3200, 101.5760, 60, "NSE toward Rawang")
GENTING_SEMPAH = Waypoint(3.3660, 101.7900, 480, "Genting Sempah")
AMPANG_STRAND = Waypoint(3.1590, 101.7620, 55, "Ampang strand")


LEGS: dict[str, list[Waypoint]] = {
    "morning": [PJ_START, FEDERAL_HWY, KLCC],
    "urban": [KLCC, BANGSAR_MAMAK, KLCC],
    "mamak": [BANGSAR_MAMAK],
    "mrr2": [KLCC, MRR2_NORTH, MRR2_EAST, KLCC],
    "refuel": [KLCC, PETRONAS_MIDVALLEY],
    "evening": [PETRONAS_MIDVALLEY, FEDERAL_HWY, KLCC, BANGSAR_MAMAK],
    "night": [KLCC, NSE_RAWANG, NSE_RAWANG],
    "fuel_spike": [NSE_RAWANG, FEDERAL_HWY, KLCC],
    "hill": [KLCC, GENTING_SEMPAH],
    "limp": [GENTING_SEMPAH, AMPANG_STRAND],
    "dead": [AMPANG_STRAND],
    "smoke": [PJ_START, FEDERAL_HWY, KLCC],
}


def haversine_km(a: Waypoint, b: Waypoint) -> float:
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _R_KM * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: Waypoint, b: Waypoint) -> float:
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


class RouteFollower:
    """Advance along a waypoint list; call set_leg() when the phase changes."""

    def __init__(self, leg: str = "morning") -> None:
        self._points: list[Waypoint] = []
        self._idx = 0
        self.lat = PJ_START.lat
        self.lon = PJ_START.lon
        self.alt_m = PJ_START.alt_m
        self.heading = 90.0
        self.set_leg(leg)

    def set_leg(self, leg: str) -> None:
        pts = LEGS.get(leg) or [PJ_START]
        self._points = list(pts)
        self._idx = 0
        if self._points:
            # Snap toward first waypoint of the new leg (keep continuity if close)
            first = self._points[0]
            here = Waypoint(self.lat, self.lon)
            if haversine_km(here, first) > 15.0:
                self.lat, self.lon, self.alt_m = first.lat, first.lon, first.alt_m
            self.heading = bearing_deg(
                Waypoint(self.lat, self.lon),
                self._points[min(1, len(self._points) - 1)],
            )

    def advance(self, dist_m: float) -> None:
        if dist_m <= 0 or len(self._points) < 2:
            return
        remaining = dist_m
        while remaining > 0 and self._idx < len(self._points) - 1:
            target = self._points[self._idx + 1]
            here = Waypoint(self.lat, self.lon, self.alt_m)
            seg_m = haversine_km(here, target) * 1000.0
            self.heading = bearing_deg(here, target)
            if seg_m < 1.0:
                self._idx += 1
                self.lat, self.lon, self.alt_m = target.lat, target.lon, target.alt_m
                continue
            step = min(remaining, seg_m)
            frac = step / seg_m
            self.lat += (target.lat - self.lat) * frac
            self.lon += (target.lon - self.lon) * frac
            self.alt_m += (target.alt_m - self.alt_m) * frac
            remaining -= step
            if step >= seg_m - 0.5:
                self._idx += 1
                self.lat, self.lon, self.alt_m = target.lat, target.lon, target.alt_m
        # Loop last segment for long legs
        if self._idx >= len(self._points) - 1 and len(self._points) >= 2:
            self._idx = max(0, len(self._points) - 2)


def kl_ambient_c(narrative_hour: float) -> float:
    """Simple diurnal ambient for KL (~26–34 °C). narrative_hour in [0, 24)."""
    h = narrative_hour % 24.0
    # Peak ~14:00, low ~05:00
    return 30.0 + 4.0 * math.sin((h - 8.0) / 24.0 * 2 * math.pi)


def narrative_hour(elapsed_s: float, day_start_hour: float = 7.0) -> float:
    return (day_start_hour + elapsed_s / 3600.0) % 24.0
