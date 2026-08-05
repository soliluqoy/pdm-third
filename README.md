# PREDICT v3 — car health, maintenance & predictive insights

Reads your **Teltonika FMC001** (OBD-II plug-in) or **FMC150** (wired CAN
tracker) and turns Codec 8 Extended telemetry into a full owner-facing stack:

- **Live dashboard** — fleet health (RAG), vitals, alerts, and open to-dos
- **Component prognostics (PME)** — physics / heuristic health for battery,
  brakes, and oil (0–100, higher = healthier) with fuzzy remaining life
- **ML anomaly scores** — Isolation Forest vs fleet daily features
  (0–100, higher = more anomalous) on the Predictive page
- **Alerts & work orders** — rules catch overheating, weak batteries, DTCs,
  service intervals, unauthorized movement, PME thresholds, and baseline /
  ML anomalies; shadow mode drafts suggested repairs first
- **Driving** — trips, harsh events, daily 0–100 score
- **History** — every sensor charted from hours to a full year (Timescale
  continuous aggregates)

```
Car OBD-II → FMC001 ┐
                     ├─ 4G LTE → <HOST>:5123 ─→ app (Codec 8E listener)
Car CAN ───→ FMC150 ┘                            │
                                                 ├─ ingest → trips → rules → WS
                                                 ├─ PME / baselines / ML jobs
                                                 └─ FastAPI + dashboard :8000
                                                        ↕
                                              db (TimescaleDB)
```

**Two containers.** No MQTT, Redis, or separate bridge — the tracker listener,
rule engine, background jobs, REST API, WebSocket hub, and SPA all run in one
Python process.

| Service | Port | Role |
|---------|------|------|
| `app` | 8000 | Dashboard (built SPA) + REST `/api/v1` + WebSocket `/ws` |
| `app` | 5123 | Teltonika AVL TCP listener (point trackers here) |
| `db` | 5432 | TimescaleDB 2.17 (PostgreSQL 16) |

Deep PME formulas: [`docs/PME_TECHNICAL_REVIEW.md`](docs/PME_TECHNICAL_REVIEW.md).

---

## Run it

```bash
copy .env.example .env     # Windows (use: cp .env.example .env  on Linux/macOS)
docker compose up --build -d
```

Open **http://localhost:8000**.

For trackers on the road, port **5123** must be publicly reachable. Set
`TRACKER_PUBLIC_HOST` in `.env` so the SMS setup helper pre-fills it.

### Add a car

1. **Settings → Add car** — name, IMEI, model (FMC001 / FMC150).
2. SMS the shown template to the tracker SIM (or Configurator: server =
   `<HOST>:5123`, TCP, **Codec 8 Extended**, ~10 s period).
3. Within a minute the Home card turns green and vitals flow.

Unregistered IMEIs are logged and dropped — check `docker logs predict_app`.

---

## The app

| Page | What it answers |
|------|-----------------|
| **Home** | Cars at a glance — health ring, key vitals, alerts, open to-dos, compact PME chips |
| **Car** | Live vitals by system, **component health + fuzzy RUL**, history charts, timeline |
| **Alerts** | Active alerts (resolve/dismiss) + full alert history |
| **Maintenance** | Work-order board + maintenance history + CSV export |
| **Predictive** | ML Isolation Forest status, per-car anomaly scores, precision/recall |
| **Driving** | Daily score, calendar, trips, notable moments |
| **Settings** | Cars + SMS setup, rule thresholds, preferences (shadow mode, behavior) |

**Shadow mode** (`ask_me_first`, default on): detections draft *suggested*
work orders — approve before they hit your to-do list.

### Alerts

- One open alert per `rule_id:vehicle_id`; repeats increment **occurrence count**
- Condition clear for the rule’s duration → **auto-resolve**
- Alerts are never deleted — full history on the Alerts page
- Buffered store-and-forward data is stored/charted but never fires rules
  (`RULE_MAX_RECORD_AGE_SECONDS`)

### Work orders → maintenance history

```
SUGGESTED ─approve→ OPEN ─start→ IN_PROGRESS ─complete→ DONE
    │                                                 (writes history,
    └── dismiss/cancel → CANCELLED                     resolves the alert,
                                                       may reset PME / label ML)
```

Completing captures notes, **cost**, and **odometer**. Completing a `predict_*`
WO resets that component’s PME state (service anchors). Completing a reactive
WO can label a `FailureEvent` for ML evaluation.

---

## Predictive layers (three systems)

Do **not** conflate these:

| Layer | Where | Polarity | What it is |
|-------|-------|----------|------------|
| **PME** | Car page, Home chips | **Higher = healthier** (0–100) | Physics / heuristics for battery, brakes, oil + fuzzy RUL |
| **Baseline anomalies** | Alerts (jobs) | Event-based | Per-car 30-day z-score / battery / cooling / fuel detectors |
| **ML Isolation Forest** | Predictive page | **Higher = more anomalous** (0–100) | Unsupervised drift vs fleet daily features |

PME also folds low scores into fleet RAG color. ML is independent of PME.

### A. Predictive Maintenance Engine (PME)

**Module:** `app/server/services/predictor.py`  
**Triggers:** trip close + hourly job (`PREDICTOR_INTERVAL_SECONDS`, default 3600).  
**Never** on the TCP hot path.

#### Battery (heuristic)

Inputs (prefer `vehicle_battery_voltage` → `control_module_voltage` →
`battery_voltage`):

- Resting voltage (ignition OFF, 7-day median)
- Crank voltage drop at trip starts
- Post-trip recovery time
- Short-trip ratio (trips &lt; 10 min)

Penalties from 100 (e.g. resting &lt; 12.0 V → −40; large crank drop; slow
recovery; many short trips). No usable voltage → score is **null**.

**Advisory “RUL”** is score buckets (not chemistry): ≥80 → 90 d, ≥50 → 30 d,
≥30 → 14 d, ≥15 → 7 d, else 3 d. Fuzzy band = buckets at `score ± 12`.

Alert `predict_battery` when score &lt; 40 or advisory days &lt; warn threshold
(default 30).

#### Brakes (physics)

Per closed trip, integrate decelerations from the speed series:

\[
\Delta KE = \tfrac{1}{2} m (v_{\mathrm{prev}}^2 - v^2)
\]

Hard brake (≥ 0.25 g) counts full ΔKE; light (≥ 0.10 g) counts a fraction
(default 0.25). Hybrid regen subtracts pad share. Fallback: device
`harsh_brake` events ≈ 50→20 km/h KE.

\[
\text{brake\_score} = 100 \times \bigl(1 - E_{\mathrm{total}} / E_{\mathrm{pad\_capacity}}\bigr)
\]

Default pad capacity 800 MJ. Fuzzy remaining km from per-trip MJ/km rates
(P10 / mean / P90). Alert `predict_brakes` when score &lt; 25.

#### Oil (schedule + stress)

Not viscosity/TAN chemistry. Base schedule over ~10,000 km interval:

\[
\text{score}_0 = 100 - 60 \times \min(1,\, d / D)
\]

Stress (7-day): thermal (&gt;110 °C), cold short trips, idle ratio, high RPM/load.
Remaining km projects until score ≈ 20. Alert `predict_oil` when score &lt; 30.

#### Baseline wear multiplier

Recent coolant / oil-temp vs each car’s 30-day baseline → z-score. If max
z ≥ 2, multiplier \(m \in [1.1, 1.5]\) accelerates oil wear (and can tighten
brake RUL). Cross-component notes (overheating → oil/brakes; weak battery →
phantom DTC advisory) land in explainable `drivers` JSON.

#### Fuzzy RUL product contract

| Component | Expected | Lo / Hi |
|-----------|----------|---------|
| Battery | `battery_rul_days` | advisory calendar days |
| Brakes | `brake_remaining_km` | pad life from MJ budget |
| Oil | `oil_remaining_km` | distance until score ~20 |

Rules and RAG use the mid/expected score; the Car page shows the range
(`between X–Y km` / `Advisory X–Yd`).

### B. Per-car baseline anomalies

**Module:** `app/server/services/baselines.py`  
**Cadence:** every `BASELINES_INTERVAL_SECONDS` (default 6 h).

1. Recompute `sensor_baselines` (30-day mean/std/p95 from hourly aggregates).
2. Detectors (24 h dedup):
   - **z-score** ≥ 3σ sustained ~3 h → `anomaly_zscore`
   - **Battery** z ≤ −1.5 → `anomaly_battery`
   - **Coolant creep** z ≥ 1.5 → `anomaly_cooling`
   - **Fuel** trip L/100 km ≥ 3σ vs recent trips → `anomaly_fuel`

### C. ML — Isolation Forest

**Modules:** `features.py` (daily vectors) · `models.py` (train/score)  
**UI:** `/predictive`

| Piece | Detail |
|-------|--------|
| Input | Daily `vehicle_features` (usage, battery, coolant, oil, RPM, harsh/idle, baseline z, trends) |
| Models | One IsolationForest per component: battery, cooling, oil, engine (`n_estimators=100`, `contamination=0.05`) |
| Train | ≥ 20 feature rows; persisted as `models/isolation_{component}.joblib` |
| Score | Invert `decision_function` → 0–100; fire if ≥ threshold (default 80); 24 h dedup |
| Rules | Auto-created `ml_anomaly_{component}` |
| Eval | Precision/recall vs `FailureEvent` labels from reactive WO complete |

---

## Detection presets

Seeded at startup (editable in Settings — threshold + on/off). Extra anomaly
rules are created lazily by baseline / PME / ML jobs.

| Key | Type | What it catches |
|-----|------|-----------------|
| `overheat` | threshold | Coolant &gt; 110 °C for 120 s (critical) |
| `coolant_hot` | threshold | Coolant &gt; 105 °C for 300 s |
| `battery_low` | threshold | `battery_voltage` &lt; 11.8 V |
| `ecu_voltage_low` | threshold | ECU V &lt; 12.0 (FMC001) |
| `car_battery_low` | threshold | CAN battery &lt; 12.0 (FMC150) |
| `oil_temp_high` | threshold | Oil temp &gt; 130 °C |
| `service_due_soon` | threshold | Distance-to-service &lt; 500 km |
| `service_interval` | scheduled | Every 10,000 km |
| `service_interval_hours` | scheduled | Engine-hours interval |
| `dtc_any` | dtc | Any diagnostic trouble code |
| `harsh_braking_day` | behavior | ≥ 8 hard brakes/day (FYI, no WO) |
| `unauthorized_movement` | threshold | Speed while ignition off |
| `predict_battery` / `predict_brakes` / `predict_oil` | anomaly | PME thresholds |
| `anomaly_*` / `ml_anomaly_*` | anomaly | Created by baseline / ML jobs |

**Rule types:** threshold (optional sustained duration + auto-resolve), DTC,
scheduled (km / engine-hours), behavior (daily event counts), anomaly.

---

## Driving score

**Module:** `app/server/services/trips.py`

Trips open/close from ignition (or movement/speed fallback). Events come from
device eco-driving (AVL 253) plus derived accel/brake/speed/idle/RPM.

```
score = clamp(100 − Σ(events_per_100km × weight) − idle_ratio × 20, 0, 100)
```

Default weights: harsh_brake 10, harsh_accel/corner 8, speeding 6, high_rpm 4,
idling 3. Tunable via `behavior.*` settings.

---

## Fleet health (RAG)

**Module:** `app/server/services/health.py`

| Color | Meaning |
|-------|---------|
| **RED** | Critical alert, or any PME score &lt; 15 |
| **YELLOW** | Warning alert, or any PME score &lt; 30 |
| **GREEN** | Online, no active issues |
| **GREY** | Offline (`OFFLINE_AFTER_SECONDS`) |

Transitions are recorded as `health_events` for the car timeline.

---

## End-to-end data flow

```
FMC001/FMC150 ──4G──► :5123 TeltonikaListener
                              │
                              ▼
                    ingest.handle_records
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  sensor_readings      live_store + WS        trips + events
        │                     │                     │
        │                     ▼                     ▼
        │              health RAG colors     on_trip_closed → PME
        │
        ├── fresh? → rules → Alert → (shadow) WorkOrder → WS
        │
        ▼
  Background jobs:
    watchdog   → mark offline
    baselines  → 30d stats + anomaly alerts
    features   → daily VehicleFeature rows
    models     → train/score Isolation Forest
    predictor  → hourly PME refresh
        │
        ▼
  FastAPI /api/v1 + /ws + static SPA (:8000)
```

---

## Configuration

Env defaults in `.env.example` work out of the box. Runtime knobs also live in
the DB settings store (Settings UI + `settings_store.py`).

### Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_*` | `predict` / `predict_dev_password` / `predict` | DB credentials (compose wires `DATABASE_URL`) |
| `DATABASE_URL` | local asyncpg URL | Only needed for uvicorn outside compose |
| `TELTONIKA_HOST` / `TELTONIKA_PORT` | `0.0.0.0` / `5123` | Tracker listener bind |
| `TELTONIKA_IDLE_TIMEOUT` | `300` | Close silent connections |
| `TRACKER_PUBLIC_HOST` | `<YOUR_SERVER_IP>` | SMS setup pre-fill only |
| `RULE_MAX_RECORD_AGE_SECONDS` | `300` | Freshness gate for rules |
| `OFFLINE_AFTER_SECONDS` | `300` | Grey / offline after silence |
| `READINGS_RETENTION_DAYS` | `365` | Raw readings retention |
| `COMPRESS_AFTER_DAYS` | `7` | Timescale chunk compression |
| `WATCHDOG_INTERVAL_SECONDS` | `60` | Offline watchdog |
| `BASELINES_INTERVAL_SECONDS` | `21600` | Baseline / anomaly job |
| `PREDICTOR_INTERVAL_SECONDS` | `3600` | PME refresh job |
| `TELEMETRY_SAMPLE_SECONDS` | `10` | Reading counts → minutes |
| `CORS_ORIGINS` | Vite origins | Dev CORS |

### Runtime settings (DB)

| Key | Default | Role |
|-----|---------|------|
| `ask_me_first` | `true` | Shadow mode for auto work orders |
| `behavior.speed_limit_kmh` | `120` | Speeding threshold |
| `behavior.idle_minutes` | `5` | Idle event threshold |
| `behavior.accel_threshold_ms2` | `3.0` | Harsh accel/brake sensitivity |
| `behavior.high_rpm_threshold` | `4000` | High-RPM threshold |
| `behavior.score_weights` | JSON | Driving-score penalties |
| `predict.brake_pad_capacity_mj` | `800` | Pad energy budget |
| `predict.brake_decel_g` | `0.25` | Hard-brake threshold |
| `predict.light_brake_g` | `0.10` | Light-brake threshold |
| `predict.light_brake_fraction` | `0.25` | Fraction of ΔKE for light stops |
| `predict.regen_fraction` | `0.0` | Global hybrid regen share |
| `predict.battery_warn_rul_days` | `30` | Battery advisory alert threshold |
| `predict.oil_interval_km` | `10000` | Oil distance schedule |
| `predict.mass_kg_default` | `1500` | KE mass if vehicle unset |
| `ml.anomaly_threshold_*` | `80` | ML fire thresholds per component |

---

## Develop

```bash
# backend (db from compose)
docker compose up -d db
pip install -r app/requirements.txt
cd app
uvicorn server.main:app --reload     # API :8000, trackers :5123
```

```bash
# frontend (proxies API + WS to :8000)
cd web
npm install
npm run dev                          # http://localhost:5173
```

```bash
cd web && npm run build              # tsc + production bundle
```

### Simulator (optional)

[`simulation/`](simulation/README.md) — FMC150 KL hybrid-taxi scenario
(stdlib-only). Smoke / condensed / full 24 h wall-clock modes exercise
thresholds, DTCs, behavior, PME, and shadow work orders.

```bash
python tools/clear_history.py --imei 359633090000001 --yes --reset-anchors
python simulation/run.py --smoke     # ~30 s connectivity
python simulation/run.py --dev       # condensed feature path
python simulation/run.py             # full 24 h + verify
```

### Ops utilities

```bash
# clear history for a car (also via compose)
docker compose run --rm -v ${PWD}/tools:/tools app python /tools/clear_history.py --yes
# or locally:
python tools/clear_history.py --imei <IMEI> --yes
```

`tools/seed_predictive_demo.py` seeds demo predictive data for UI walkthroughs.

---

## API overview

REST under `/api/v1`, WebSocket at `/ws`, liveness at `/health`. Interactive
docs: `http://localhost:8000/docs`.

| Group | Endpoints |
|-------|-----------|
| Live | `GET /overview` · `/live/fleet` · `/live/summary` |
| Cars | `POST/GET /cars` · `GET/PATCH/DELETE /cars/{id}` · `/vitals` · `/prognostics` · `/history` (raw ≤6 h, 1m ≤7 d, 1h ≤30 d, 1d all-time) · `/timeline` · `/driving-events` |
| Alerts | `GET /alerts` · `POST /alerts/{id}/resolve` · `/dismiss` |
| Work orders | `GET/POST /workorders` · `…/approve` · `/start` · `/complete` · `/cancel` |
| Maintenance | `GET /maintenance` · `/maintenance/export.csv` |
| Driving | `GET /driving/summary` · `/driving/cars/{id}/calendar` · `/trips` · `/scores` |
| Rules | `GET /rules` · `PATCH /rules/{id}` |
| Settings | `GET/PATCH /settings` · `GET /settings/catalog/{model}` |
| ML models | `GET /models/status` · `/models/evaluate` · `/models/vehicles/{id}` |

WebSocket events include `telemetry`, `health`, `alert`, `work_order`, `trip`,
`driving_event`.

---

## Data model (key tables)

| Table | Role |
|-------|------|
| `vehicles` | Car + IMEI + device type + PME anchors (mass, pad MJ, regen, service odo/dates) |
| `sensor_readings` | Timescale hypertable (compress 7 d, retain 365 d) |
| `sensor_readings_1m/_1h/_1d` | Continuous aggregates for charts |
| `rules` / `alerts` | Detection presets + deduped alert history |
| `work_orders` / `maintenance_log` | Lifecycle to-dos + immutable completed work |
| `failure_events` | ML ground truth from reactive WO complete |
| `trips` / `driving_events` / `driver_scores` | Driving insights |
| `sensor_baselines` | 30-day per-sensor stats |
| `component_health` / `component_wear_events` | Latest PME + wear/score/reset ledger |
| `vehicle_features` | Daily ML feature vectors |
| `settings` | Key/value runtime config |

---

## Adding sensors / a new device model

AVL maps live in `app/server/teltonika/avl/<model>.json` (AVL ID → normalized
`sensor_type`). Unmapped IDs are logged once per process. Add the entry,
restart `app`. For a whole model: new map file, extend `device_type` in
`schemas.py`, and the Settings model picker.

---

## Project structure

```
pdm-third/
├── docker-compose.yml          # db (TimescaleDB) + app
├── .env.example
├── docs/
│   └── PME_TECHNICAL_REVIEW.md # authoritative PME formulas
├── app/
│   ├── Dockerfile              # Vite build → Python runtime
│   ├── requirements.txt
│   └── server/
│       ├── main.py             # lifespan: init_db → listener → jobs; SPA host
│       ├── models.py           # SQLAlchemy schema
│       ├── ingest.py           # decode → persist → live → trips → rules
│       ├── rules.py            # rule engine + presets
│       ├── alerts.py / workorders.py
│       ├── catalog.py          # AVL → normalized sensors
│       ├── teltonika/          # Codec 8E TCP + avl/*.json
│       ├── api/                # REST routers
│       └── services/
│           ├── predictor.py    # PME (battery / brakes / oil)
│           ├── baselines.py    # 30d baselines + anomaly detectors
│           ├── features.py     # daily ML feature vectors
│           ├── models.py       # Isolation Forest train/score
│           ├── trips.py        # trips, events, driving score
│           ├── health.py       # fleet RAG
│           └── watchdog.py     # offline detection
├── web/                        # React 18 + Vite + Tailwind SPA
├── simulation/                 # optional FMC150 KL taxi simulator
└── tools/                      # clear_history, seed_predictive_demo
```

---

## Tech stack

- **Backend** — Python 3.12, FastAPI, SQLAlchemy 2 (async), asyncpg,
  pydantic v2 / pydantic-settings, uvicorn
- **ML** — scikit-learn IsolationForest, numpy, joblib
- **Database** — TimescaleDB 2.17 on PostgreSQL 16 (hypertables, compression,
  retention, continuous aggregates)
- **Frontend** — React 18, TypeScript, Vite 6, Tailwind CSS 3, TanStack Query 5,
  React Router 6, Recharts, lucide-react
- **Device protocol** — Teltonika Codec 8 Extended over raw TCP
- **Packaging** — two-service docker-compose; multi-stage image
  (`node:22-alpine` → `python:3.12-slim`)

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tracker never connects | Wrong APN/SIM PIN, firewall on 5123, or stack down (`docker compose ps`) |
| Connected but no car appears | IMEI not registered — add it in Settings (`docker logs predict_app`) |
| No VIN / fault codes / service distance | Device still on plain Codec 8 — switch to **Codec 8 Extended** |
| Wrong/missing RPM, fuel | Wrong model at registration, or car doesn't expose that PID/CAN param — check unmapped-AVL logs |
| FMC001 primary platform stalls | Duplicate mode requires BOTH servers to ACK — fix the endpoint or disable Duplicate |
| Alert keeps re-firing | It won't spam — one alert, occurrence count increments; auto-resolves when clear |
| PME scores empty / “collecting” | Needs trips + voltage/speed samples; wait for trip close or hourly job |
| Predictive page all zeros | Need ≥ ~20 daily feature rows before Isolation Forest trains |

Clear simulation / car history:

```bash
docker compose run --rm -v ${PWD}/tools:/tools app python /tools/clear_history.py --yes
```
