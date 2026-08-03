# PREDICT v3 — car health & maintenance, at a glance

Reads your **Teltonika FMC001** (OBD-II plug-in dongle) or **FMC150** (wired CAN
tracker) and turns raw telemetry into everything a car owner needs:

- **Live dashboard** — every car's health, vitals, and anything needing
  attention, visible with zero clicks.
- **Total history** — every sensor charted from 6 hours to a full year.
- **Alerts** — rules catch overheating, weak batteries, fault codes, and service
  intervals. Active list + a permanent alert history.
- **Work orders** — detections draft maintenance to-dos (approve them first —
  shadow mode); completing one writes permanent maintenance history with cost
  and odometer.
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
the tracker listener, rule engine, REST API, WebSocket hub, and dashboard all
run in one Python process.

| Service | Port | Role |
|---------|------|------|
| `app` | 8000 | Dashboard (built SPA) + REST API `/api/v1` + WebSocket `/ws` |
| `app` | 5123 | Teltonika AVL TCP listener (point trackers here) |
| `db` | 5432 | TimescaleDB 2.17 (PostgreSQL 16) |

---

## Run it

```bash
copy .env.example .env     # Windows (use: cp .env.example .env  on Linux/macOS)
docker compose up --build -d
```

Open **http://localhost:8000**. That's the whole stack.

For trackers to reach it from the road, port **5123** must be publicly reachable
(a small VPS with a static IP, or a router port-forward). Set
`TRACKER_PUBLIC_HOST` in `.env` so the SMS setup helper pre-fills it.

## Add your car

1. **Settings → Add car** — name, tracker IMEI, model (FMC001 / FMC150).
2. The app shows a **text-message template** — SMS it to the tracker's SIM
   (or use Teltonika Configurator: server = `<HOST>:5123`, TCP,
   **Codec 8 Extended**, send every 10 s).
3. Within a minute the card on **Home** turns green and vitals start flowing.

Unregistered IMEIs are logged and dropped on purpose — check
`docker logs predict_app` if a car never appears.

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

### Detection presets

Ten preset rules ship out of the box (seeded at startup, editable in Settings —
threshold + on/off, no rule builder):

| Key | Type | What it catches |
|-----|------|-----------------|
| `overheat` | threshold | Coolant critically hot (critical) |
| `coolant_hot` | threshold | Coolant above normal for a sustained period |
| `battery_low` | threshold | Electrical system voltage low |
| `ecu_voltage_low` | threshold | ECU supply voltage low (FMC001) |
| `car_battery_low` | threshold | Vehicle-reported CAN battery voltage low (FMC150) |
| `oil_temp_high` | threshold | Engine oil above safe sustained range |
| `service_due_soon` | threshold | Car-reported distance-to-service < 500 km |
| `service_interval` | scheduled | Regular maintenance every 10,000 km |
| `dtc_any` | dtc | Any diagnostic trouble code from the car |
| `harsh_braking_day` | behavior | More than 8 hard-braking events in one day |

Rule types: **threshold** (sensor vs limit), **DTC** (fault-code match),
**scheduled** (km / engine-hours / days interval), **behavior** (daily driving
event counts), **anomaly** (deviation from the car's own 30-day baseline).

## Configuration

Everything is optional — the defaults in `.env.example` work out of the box.

| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `predict` / `predict_dev_password` / `predict` | Database credentials (compose auto-wires `DATABASE_URL` from these) |
| `DATABASE_URL` | local dev URL | Full SQLAlchemy URL — only set it for local dev outside compose |
| `TELTONIKA_HOST` / `TELTONIKA_PORT` | `0.0.0.0` / `5123` | Tracker listener bind address |
| `TELTONIKA_IDLE_TIMEOUT` | `300` | Close a silent tracker connection after N seconds |
| `TRACKER_PUBLIC_HOST` | `<YOUR_SERVER_IP>` | Pre-fills the SMS setup templates in Settings (listener doesn't use it) |
| `RULE_MAX_RECORD_AGE_SECONDS` | `300` | Rules only fire for records fresher than this (buffered data is stored, never alerts) |
| `OFFLINE_AFTER_SECONDS` | `300` | A car shows offline (grey) after N seconds without data |
| `READINGS_RETENTION_DAYS` | `365` | Raw readings retention; 1m/1h/1d aggregates are kept beyond this |
| `COMPRESS_AFTER_DAYS` | `7` | TimescaleDB compresses chunks older than this |
| `WATCHDOG_INTERVAL_SECONDS` | `60` | Offline-watchdog cadence |
| `BASELINES_INTERVAL_SECONDS` | `21600` (6 h) | Baseline/anomaly job cadence |
| `CORS_ORIGINS` | `http://localhost:5173,…` | Allowed origins for the Vite dev server |

## Develop

```bash
# backend (db from compose)
docker compose up -d db
pip install -r app/requirements.txt
cd app
uvicorn server.main:app --reload     # API on :8000, trackers on :5123
```

```bash
# frontend (proxies API + WS to :8000) — separate terminal
cd web
npm install
npm run dev                          # http://localhost:5173
```

```bash
# type gate
cd web
npm run build                        # tsc type check + production bundle
```

## API overview

REST under `/api/v1`, WebSocket at `/ws`, liveness at `/health`. Interactive
docs at `http://localhost:8000/docs` while developing.

| Group | Endpoints |
|-------|-----------|
| Live | `GET /overview` · `GET /live/fleet` · `GET /live/summary` |
| Cars | `POST/GET /cars` · `GET/PATCH/DELETE /cars/{id}` · `GET /cars/{id}/vitals` · `GET /cars/{id}/history` (auto resolution: raw ≤ 6 h, 1m ≤ 7 d, 1h ≤ 30 d, 1d all-time) · `GET /cars/{id}/timeline` · `GET /cars/{id}/driving-events` |
| Alerts | `GET /alerts` · `POST /alerts/{id}/resolve` · `POST /alerts/{id}/dismiss` |
| Work orders | `GET/POST /workorders` · `POST /workorders/{id}/approve` · `/start` · `/complete` · `/cancel` |
| Maintenance | `GET /maintenance` · `GET /maintenance/export.csv` |
| Driving | `GET /driving/summary` · `GET /driving/cars/{id}/calendar` · `/trips` · `/scores` |
| Rules | `GET /rules` · `PATCH /rules/{id}` |
| Settings | `GET/PATCH /settings` · `GET /settings/catalog/{model}` (what each tracker model can report) |

## Adding sensors / a new device model

AVL maps live in `app/server/teltonika/avl/<model>.json` (AVL ID → normalized
`sensor_type`; both models normalize to the same types). Unmapped IDs are
logged once per process — that is the discovery tool. Add the entry, restart
`app`. To add a whole model: drop in a new map file, extend the `device_type`
pattern in `app/server/schemas.py` and the model picker in Settings.

## Architecture notes

- **Ingest**: one asyncio process — TCP listener → Codec 8E decode → AVL map →
  batched INSERT → merged in-process live state → trips → rules → WS broadcast.
- **Data**: `sensor_readings` hypertable, compression after 7 days, retention
  365 days. 1-minute / 1-hour / **1-day** continuous aggregates feed history
  charts (the daily aggregate is what makes the 1-year view instant).
- **Rules**: preset detections (threshold / DTC / scheduled / behavior /
  anomaly) with plain-language recommendations. A firing rule opens an *alert*
  and, when advice exists, drafts a *work order*.
- **Rule cache**: active rules are cached in-process and invalidated on any
  change; sustained-duration timers live in-process with self-expiry.
- **Health**: green/yellow/red from active alerts, grey when offline;
  transitions are recorded as health events for the car timeline.

## Project structure

```
pdm-third/
├── docker-compose.yml          # db (TimescaleDB) + app (all-in-one)
├── .env.example                # every knob, with sane defaults
├── app/
│   ├── Dockerfile              # stage 1 builds the SPA, stage 2 runs it all
│   ├── requirements.txt        # lean: FastAPI, SQLAlchemy async, asyncpg
│   ├── server/
│   │   ├── main.py             # lifespan: init_db → listener → jobs; SPA host
│   │   ├── models.py           # 13 tables (alerts, work_orders, …)
│   │   ├── ingest.py           # decode → persist → live → trips → rules
│   │   ├── rules.py            # rule engine + 10 presets
│   │   ├── alerts.py           # alert create/dedup/resolve/history
│   │   ├── workorders.py       # WO lifecycle + maintenance_log
│   │   ├── teltonika/          # Codec 8E listener + per-model AVL maps
│   │   ├── api/                # live, cars, alerts, workorders+maintenance,
│   │   │                       # driving, rules, settings
│   │   └── services/           # watchdog, health, trips, baselines
├── tools/                      # clear_history (ops utility)
└── web/                        # React 18 + Vite + Tailwind dashboard
```

## Tech stack

- **Backend** — Python 3.12, FastAPI, SQLAlchemy 2 (async), asyncpg,
  pydantic v2 / pydantic-settings, uvicorn
- **Database** — TimescaleDB 2.17 on PostgreSQL 16 (hypertables, compression,
  retention, continuous aggregates)
- **Frontend** — React 18, TypeScript, Vite 6, Tailwind CSS 3, TanStack Query,
  React Router 6, Recharts, lucide-react
- **Device protocol** — Teltonika Codec 8 Extended over raw TCP
- **Packaging** — two-service docker-compose; multi-stage app image
  (`node:22-alpine` builds the SPA → `python:3.12-slim` runtime)

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tracker never connects | Wrong APN/SIM PIN, firewall on 5123, or stack down (`docker compose ps`) |
| Connected but no car appears | IMEI not registered — add it in Settings (check `docker logs predict_app`) |
| No VIN / fault codes / service distance | Device still on plain Codec 8 — switch to **Codec 8 Extended** |
| Wrong/missing RPM, fuel | Wrong model at registration, or car doesn't expose that PID/CAN param — check unmapped-AVL logs |
| FMC001 primary platform stalls | Duplicate mode requires BOTH servers to ACK — fix the endpoint or disable Duplicate |
| Alert keeps re-firing | It won't spam — one alert, occurrence count increments. It auto-resolves when the condition clears |

delete simulation log :

docker compose run --rm -v ${PWD}/tools:/tools app python /tools/clear_history.py --yes