#!/usr/bin/env python3
"""PREDICT smoke test helper: drive the REST API without shell quoting pain.

Usage:
    python tools/smoke.py patch-overheat 5     # set overheat sustain window (s)
    python tools/smoke.py status               # issues, tasks, health snapshot
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
        rules = call("GET", "/api/v1/settings/rules")
        rule = next(r for r in rules if r["key"] == "overheat")
        print(call("PATCH", f"/api/v1/settings/rules/{rule['id']}",
                   {"duration_seconds": seconds}))
        return 0

    if cmd == "status":
        issues = call("GET", "/api/v1/issues?status=active")
        print("active issues:", [
            (i["title"], i["severity"], i["message"][:70]) for i in issues
        ])
        tasks = call("GET", "/api/v1/tasks?status=suggested")
        print("suggested tasks:", [
            (t["title"], t["priority"], (t["description"] or "")[:70]) for t in tasks
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
