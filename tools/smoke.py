#!/usr/bin/env python3
"""PREDICT smoke test helper: drive the REST API without shell quoting pain.

Usage:
    python tools/smoke.py patch-overheat 5     # set overheat sustain window (s)
    python tools/smoke.py status               # alerts, work orders, health snapshot
"""
import json
import sys
import urllib.request

BASE = "http://localhost:8000"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "patch-overheat":
        seconds = int(sys.argv[2])
        rules = call("GET", "/api/v1/rules")
        rule = next(r for r in rules if r["key"] == "overheat")
        print(call("PATCH", f"/api/v1/rules/{rule['id']}",
                   {"duration_seconds": seconds}))
        return 0

    if cmd == "status":
        alerts = call("GET", "/api/v1/alerts?status=active")
        print("active alerts:", [
            (a["vehicle_name"], a["title"], a["severity"],
             f"x{a['occurrence_count']}", a["message"][:70]) for a in alerts
        ])
        wos = call("GET", "/api/v1/workorders?status=suggested")
        print("suggested work orders:", [
            (w["vehicle_name"], w["title"], w["priority"],
             (w["description"] or "")[:70]) for w in wos
        ])
        fleet = call("GET", "/api/v1/live/fleet")
        for car in fleet:
            print("car:", car["name"], "| health:", car["health"],
                  "| live:", car["live"]["live"])
        return 0

    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
