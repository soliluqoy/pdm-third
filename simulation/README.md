# PREDICT standalone end-to-end simulator

This folder is **completely optional**. Delete it and the main app keeps working —
nothing under `app/`, `web/`, or `tools/` imports from here.

It registers **one** car, opens a real **Teltonika Codec 8E** TCP session to
`:5123`, and drives a continuous physics-based trip that exercises:

| Area | What the scenario triggers |
|------|----------------------------|
| Threshold alerts | Overheat, coolant hot, oil temp high, battery low, ECU voltage low, service due soon |
| DTC | `P0300`, `P0128` |
| Behavior | Harsh brake (≥8/day), harsh accel/corner, speeding, high RPM, idling |
| Scheduled | Odometer past 10 000 km service interval |
| Predictive (PME) | Brake energy wear, weak resting/crank battery, oil distance used |
| Work orders | Auto-drafted SUGGESTED WOs (shadow mode) for rules with `auto_work_order` |

## Prerequisites

```bash
docker compose up --build
```

Dashboard: http://localhost:8000 · Teltonika TCP: `127.0.0.1:5123`

## Run

Python **3.10+**, **stdlib only** (no pip install).

```bash
# from repo root
python simulation/run.py

# connectivity only (~30 s)
python simulation/run.py --smoke

# shorter idle (1 min); alert duration holds stay real (overheat 130s, oil 310s, …)
python simulation/run.py --quick

# custom targets
python simulation/run.py --host 127.0.0.1 --port 5123 --api http://localhost:8000
```

Full scenario wall time is roughly **20–25 minutes** (idle 5 min + several
multi-minute alert holds). `--quick` drops idle to 1 minute (~17–20 min).

## Layout

| File | Role |
|------|------|
| `run.py` | CLI entry |
| `scenario.py` | Phased trip script |
| `physics.py` | Longitudinal + thermal + battery model |
| `codec8e.py` | Codec 8E packet encoder |
| `tcp_client.py` | Device-side TCP (IMEI handshake → AVL → ACK) |
| `avl_map.py` | `VehicleState` → FMC001 AVL IDs |
| `api.py` | REST register / summary helpers |

## Sim car

IMEI `359633090000001` — reused if already registered. Safe to clear afterward:

```bash
python tools/clear_history.py --imei 359633090000001 --yes
```

## Notes

- Timestamps are **wall clock** so rule freshness (`RULE_MAX_RECORD_AGE_SECONDS`) accepts them.
- Alert phases hold conditions for at least each rule’s `duration_seconds`.
- PME scores update on trip close and on the hourly predictor job; the short-trip
  + weak-rest phases bias battery/brake/oil into alert range. If scores look
  unchanged immediately, wait for the next predictor pass or close another trip.
