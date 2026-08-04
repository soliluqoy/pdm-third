# PREDICT KL Grind simulator (FMC150)

Optional standalone folder. Delete it and the main app keeps working.

Simulates a **Toyota Corolla Hybrid taxi** in Kuala Lumpur with a wired
**FMC150** CAN tracker. Default mode is a **real 24-hour wall-clock** shift
(timestamps = wall UTC, sample every 5 s).

| Area | What the scenario triggers |
|------|----------------------------|
| Threshold | Overheat, oil very hot, battery / car battery low, service due soon |
| DTC | `P0217` engine overtemp |
| Behavior | Harsh brakes (≥8/day), accel/corner, speeding, high RPM, idling (mamak) |
| Scheduled | Odometer 10 000 km + engine-hours interval |
| Fuel | Monotonic `fuel_consumed` (L) → trip L/100 km; mid-shift Petronas **refuel** |
| Predictive (PME) | Brake energy (tiny 2.5 MJ pad budget + 0.4 regen for demo), weak battery / short trips, oil stress |
| Work orders | SUGGESTED drafts (`ask_me_first=true`) |

## Prerequisites

```bash
docker compose up --build -d
```

Dashboard: http://localhost:8000 · Teltonika TCP: `127.0.0.1:5123`

Python **3.10+**, **stdlib only**.

## Run

```bash
# from repo root — clear prior sim history first
python tools/clear_history.py --imei 359633090000001 --yes --reset-anchors

# connectivity (~30 s)
python simulation/run.py --smoke

# condensed feature path (~32 min) for iteration
python simulation/run.py --dev

# REAL 24-hour wall-clock shift + verify at the end
python simulation/run.py
python simulation/run.py --log-file simulation/kl_grind.log

# resume after crash / Ctrl+C (uses simulation/.kl_grind_checkpoint.json)
python simulation/run.py --resume
```

**Host sleep freezes the sim** — run on a machine that stays awake, or use
`caffeinate` / Windows “prevent sleep” while `run.py` is active.

## Layout

| File | Role |
|------|------|
| `run.py` | CLI |
| `scenario.py` | 24h / dev / smoke runner + checkpoint |
| `schedule.py` | Phase timetable |
| `route.py` | KL waypoints |
| `physics.py` | CAN hybrid physics, refuel, degradation |
| `avl_map.py` | FMC150 AVL IDs / scales |
| `codec8e.py` | Codec 8E encoder |
| `tcp_client.py` | Device TCP + reconnect backoff |
| `api.py` | REST helpers |
| `verify.py` | Post-run checklist |

## Sim car

IMEI `359633090000001`, `device_type: fmc150`. Starts with a **full tank**.

```bash
python tools/clear_history.py --imei 359633090000001 --yes --reset-anchors
```

## Notes

- Timestamps are **wall clock** so rule freshness accepts every packet.
- Alert phases hold conditions for each rule’s `duration_seconds` in real time.
- `--dev` is for coding loops only — acceptance is a successful **full 24h** verify.
- Checkpoint every 10 minutes during the full run; deleted on clean completion.
