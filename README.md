# PREDICT v3 — car health & maintenance, at a glance

Reads your **Teltonika FMC001** (OBD-II plug-in) or **FMC150** (wired CAN) tracker
and turns raw telemetry into everything a car owner needs:

- **Live dashboard** — every car's health, vitals, and anything needing attention,
  visible with zero clicks.
- **Total history** — every sensor charted from 6 hours to a full year.
- **Alerts** — rules catch overheating, weak batteries, fault codes, service
  intervals. Active list + a permanent alert history.
- **Work orders** — detections draft maintenance to-dos (approve them first —
  shadow mode); completing one writes permanent maintenance history with
  cost and odometer.
- **Driving** — trips, hard braking/acceleration, speeding, idling, daily score.

```
Car OBD-II → FMC001 ┐
                     ├─ 4G LTE → <HOST>:5123 ─→ app (Codec 8E listener)
Car CAN ───→ FMC150 ┘                            ├─ ingest → rules → WS
                                                 └─ FastAPI + dashboard
                                                        ↕
                                              db (TimescaleDB)
```

**Two containers. That's it.** No MQTT broker, no Redis, no separate bridge —
the tracker listener, rule engine, API, and dashboard all run in one process.

| Service | Port | Role |
|---------|------|------|
| `app` | 8000 | Dashboard + REST API + WebSocket |
| `app` | 5123 | Teltonika AVL TCP (point trackers here) |
| `db` | 5432 | TimescaleDB (PostgreSQL 16) |

---

## Run it

```bash
copy .env.example .env     # Windows (cp on Linux/macOS)
docker compose up --build -d
```

Open **http://localhost:8000**. That's the whole stack.

For trackers to reach it from the road, port **5123** must be publicly reachable
(a small VPS with a static IP, or a router port-forward). Set
`TRACKER_PUBLIC_HOST` in `.env` so the SMS setup helper pre-fills it.

## Add your car

1. **Settings → Add car** — name, tracker IMEI, model (FMC001/FMC150).
2. The app shows a **text-message template** — SMS it to the tracker's SIM
   (or use Teltonika Configurator: server = `<HOST>:5123`, TCP,
   Codec 8 Extended, send every 10 s).
3. Within a minute the card on **Home** turns green and vitals start flowing.

Unregistered IMEIs are logged and dropped on purpose.

## The app

| Page | What it answers |
|------|-----------------|
| **Home** | Cars at a glance — health ring, key vitals, active alerts, open to-dos |
| **Alerts** | Active alerts (resolve/dismiss) + **full alert history** |
| **Maintenance** | Work-order board (Suggested → Open → In progress) + **maintenance history** + CSV export |
| **Car** (tap a card) | Live vitals by system, history charts (6 h → 1 y), full timeline |
| **Driving** | Daily score, trips, notable moments |
| **Settings** | Cars + SMS setup, warning limits (sliders, no rule builder), preferences |

**Shadow mode** (Settings, default on): when PREDICT spots something fixable it
drafts a *suggested* work order instead of putting it straight on your list —
approve or dismiss. Nothing touches your to-do list without consent.

### Alerts

- A firing rule opens one alert. If it keeps firing, the alert's **occurrence
  count** goes up — no spam rows.
- When the condition clears (and stays clear for the rule's duration), the
  alert **auto-resolves**.
- Alerts are never deleted — the Alerts page shows the complete history.
- Buffered store-and-forward data is stored and charted but never fires rules
  (`RULE_MAX_RECORD_AGE_SECONDS`), so old trips cause no phantom alerts.

### Work orders → maintenance history

```
SUGGESTED ─approve→ OPEN ─start→ IN_PROGRESS ─complete→ DONE
    │                                                 (writes history,
    └── dismiss/cancel → CANCELLED                     resolves the alert)
```

Completing captures notes, **cost**, and **odometer**, and appends an immutable
row to the maintenance log. History is filterable per car and exportable to CSV
(`GET /api/v1/maintenance/export.csv`).

## Develop

```bash
# backend (db from compose)
docker compose up -d db
pip install -r app/requirements.txt pytest
cd app && uvicorn server.main:app --reload     # API on :8000, trackers on :5123

# frontend (proxies API+WS to :8000)
cd web && npm install && npm run dev           # http://localhost:5173

# tests & type gate
cd app && python -m pytest tests -q
cd web && npm run build
```

### Fake a tracker (real Codec 8E over TCP — the production path)

```bash
python tools/replay.py --imei 352999001234567 --scenario drive
python tools/replay.py --imei 352999001234567 --scenario overheat   # fires a rule
python tools/replay.py --imei 352999001234567 --scenario burst      # old buffered data (no phantom alerts)
python tools/smoke.py status                                        # snapshot
```

### Simulate an FMC150 car (wired CAN tracker — full CAN vitals)

`tools/simulate_fmc150.py` is a virtual car with realistic physics streaming the
FMC150 CAN parameter set (RPM, coolant/oil temps, oil pressure, throttle, fuel,
CAN odometer, engine hours, CAN battery voltage, service countdown, VIN, DTCs,
eco-driving events).

```bash
python tools/simulate_fmc150.py --register                  # add the car, then drive
python tools/simulate_fmc150.py --scenario overheat         # fires BOTH overheating rules
python tools/simulate_fmc150.py --scenario weak_battery     # "Car battery low"
python tools/simulate_fmc150.py --scenario dtc              # check-engine codes
python tools/simulate_fmc150.py --scenario service          # "Service due soon"
```

### Watch every FMC150 sensor on a real Kuala Lumpur drive (standalone)

`tools/kl_drive_sim.py` needs **no server, no DB — nothing from `app/`**. A
virtual Corolla with an FMC150 drives a real 16 km KL route (Mid Valley →
KL Sentral → Bukit Bintang → Jalan Tun Razak → the AKLEH elevated highway →
Ampang → back through the evening rain to the Petronas Towers at KLCC) and
renders **every sensor the module reports**, live, in your terminal —
powertrain, temps, fuel, electrical, counters, GPS, GSM, VIN/DTCs, plus the
device-native harsh accel/brake/corner and overspeed events as they fall out
of the actual driving. Great for demos and for seeing what the CAN parameter
set looks like before any server enters the picture.

```bash
python tools/kl_drive_sim.py                  # live dashboard, 10x speed
python tools/kl_drive_sim.py --rate 1         # true real-time rush hour
python tools/kl_drive_sim.py --avl            # + raw AVL IO wire values
python tools/kl_drive_sim.py --plain          # one log line per tick (pipes)
python tools/kl_drive_sim.py --hybrid --dtc   # HV battery + check-engine
python tools/kl_drive_sim.py --list-route     # the 20-leg itinerary
python tools/kl_drive_sim.py --jsonl drive.jsonl   # export every tick
```

Want the drive **in the web dashboard** too? With the stack up
(`docker compose up -d`), add `--stream` — every tick is also sent to the
server as a real Codec 8E packet (the production path), so the Home card
goes green and the Car / Driving pages fill in live:

```bash
python tools/kl_drive_sim.py --stream --register        # first time
python tools/kl_drive_sim.py --stream                   # already registered
```

## Adding sensors / a new device model

AVL maps live in `app/server/teltonika/avl/<model>.json` (AVL ID → normalized
`sensor_type`; both models normalize to the same types). Unmapped IDs are
logged once per process — that is the discovery tool. Add the entry, restart
`app`. To add a whole model: drop in a new map file, extend the `device_type`
pattern in `app/server/schemas.py` and the model picker in Settings.

## Architecture notes

- **Ingest**: one asyncio process — TCP listener → decode → batched INSERT →
  merged in-process live state → trips → rules → WS broadcast.
- **Data**: `sensor_readings` hypertable, compression after 7 days, retention
  365 days. 1-minute / 1-hour / **1-day** continuous aggregates feed history
  charts (the daily aggregate is what makes the 1-year view instant).
- **Rules**: preset detections (threshold / DTC / scheduled / behavior /
  anomaly) with plain-language recommendations. A firing rule opens an *alert*
  and, when advice exists, drafts a *work order*.
- **Rule cache**: active rules are cached in-process and invalidated on any
  change; sustained-duration timers live in-process with self-expiry.

## Project structure

```
pdm-third/
├── docker-compose.yml
├── comple-rebuilding-of-predict.md   # the v3 spec
├── app/
│   ├── Dockerfile
│   ├── server/
│   │   ├── main.py               # lifespan: init_db → listener → jobs
│   │   ├── models.py             # 14 tables (alerts, work_orders, …)
│   │   ├── ingest.py             # decode → persist → live → trips → rules
│   │   ├── rules.py              # rule engine + presets
│   │   ├── alerts.py             # alert create/dedup/resolve/history
│   │   ├── workorders.py         # WO lifecycle + maintenance_log
│   │   ├── teltonika/            # Codec 8E listener + AVL maps
│   │   ├── api/                  # overview, cars, alerts, workorders,
│   │   │                         # maintenance, driving, rules, settings
│   │   └── services/             # watchdog, health, trips, baselines
│   └── tests/
├── tools/                        # replay / simulate_fmc150 / kl_drive_sim / smoke
└── web/                          # React + Vite + Tailwind dashboard
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tracker never connects | Wrong APN/SIM PIN, firewall on 5123, or stack down (`docker compose ps`) |
| Connected but no car appears | IMEI not registered — add it in Settings (check `docker logs predict_app`) |
| No VIN / fault codes / service distance | Device still on plain Codec 8 — switch to **Codec 8 Extended** |
| Wrong/missing RPM, fuel | Wrong model at registration, or car doesn't expose that PID/CAN param — check unmapped-AVL logs |
| FMC001 primary platform stalls | Duplicate mode requires BOTH servers to ACK — fix the endpoint or disable Duplicate |
| Alert keeps re-firing | It won't spam — one alert, occurrence count increments. It auto-resolves when the condition clears |