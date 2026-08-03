"""
HTTP helpers for the PREDICT REST API (stdlib only).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


class PredictApi:
    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self.base = base_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> Any:
        url = f"{self.base}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"{method} {path} failed: {e.reason}") from e

    def health(self) -> Any:
        return self._request("GET", "/health")

    def list_cars(self) -> list:
        return self._request("GET", "/api/v1/cars") or []

    def find_car_by_imei(self, imei: str) -> Optional[dict]:
        for car in self.list_cars():
            if str(car.get("imei")) == imei:
                return car
        return None

    def register_car(self, payload: dict) -> dict:
        existing = self.find_car_by_imei(payload["imei"])
        if existing:
            # Re-apply physics / service anchors so re-runs stay deterministic.
            patch = {
                k: payload[k]
                for k in (
                    "name", "license_plate", "make", "model", "year", "vin",
                    "mass_kg", "oil_capacity_l", "brake_pad_capacity_mj",
                    "last_oil_change_odo", "last_brake_service_odo",
                )
                if k in payload and payload[k] is not None
            }
            if patch:
                try:
                    return self._request("PATCH", f"/api/v1/cars/{existing['id']}", patch)
                except RuntimeError:
                    return existing
            return existing
        return self._request("POST", "/api/v1/cars", payload)

    def patch_settings(self, values: dict[str, str]) -> Any:
        return self._request("PATCH", "/api/v1/settings", {"values": values})

    def overview(self) -> Any:
        return self._request("GET", "/api/v1/overview")

    def car_vitals(self, vehicle_id: int) -> Any:
        return self._request("GET", f"/api/v1/cars/{vehicle_id}/vitals")

    def car_prognostics(self, vehicle_id: int) -> Any:
        return self._request("GET", f"/api/v1/cars/{vehicle_id}/prognostics")

    def alerts(self, vehicle_id: Optional[int] = None, status: str = "all") -> list:
        q = [f"status={status}", "limit=500"]
        if vehicle_id is not None:
            q.append(f"vehicle_id={vehicle_id}")
        return self._request("GET", f"/api/v1/alerts?{'&'.join(q)}") or []

    def workorders(self, vehicle_id: Optional[int] = None) -> list:
        q = ["status=all", "limit=500"]
        if vehicle_id is not None:
            q.append(f"vehicle_id={vehicle_id}")
        return self._request("GET", f"/api/v1/workorders?{'&'.join(q)}") or []

    def trips(self, vehicle_id: int) -> list:
        try:
            return self._request("GET", f"/api/v1/driving/cars/{vehicle_id}/trips") or []
        except RuntimeError:
            return []

    def driving_events(self, vehicle_id: int) -> list:
        try:
            return self._request("GET", f"/api/v1/cars/{vehicle_id}/driving-events") or []
        except RuntimeError:
            return []
