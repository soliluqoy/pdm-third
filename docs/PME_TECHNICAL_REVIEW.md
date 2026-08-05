# Predictive Maintenance Engine (PME) — Technical Review

**System:** PREDICT v3  
**Approach:** Physics / heuristic algorithms — **no ML training, no GPU**  
**Primary implementation:** [`app/server/services/predictor.py`](../app/server/services/predictor.py)  
**Scope:** Battery, brakes, engine oil — health score (0–100) + fuzzy remaining useful life (RUL)

---

## 1. Design verdict

PME treats component health as a **depleting resource**, not a black-box failure classifier.

| Principle | Implication |
|-----------|-------------|
| Deterministic | Same inputs → same scores; fully explainable via `drivers` JSON |
| Physics-first where possible | Brakes use kinetic energy (MJ); battery uses voltage heuristics; oil uses schedule + stress |
| Relative where absolute thresholds fail | 30-day per-car baselines accelerate wear when *this* car runs hotter than *its* norm |
| Fuzzy RUL | UI shows ranges (pessimistic–optimistic), not a false-precision single km |
| Separate from ML | `/predictive` Isolation Forest is optional and independent |

**Do not confuse:**

| Layer | UI | Score meaning |
|-------|----|---------------|
| **PME** (this doc) | Car page “Component health”, Home chips | Higher = healthier |
| **ML anomaly** | `/predictive` | Higher = more anomalous vs fleet |

---

## 2. Architecture

```
Teltonika FMC001/FMC150
        │ Codec 8 / 8E TCP :5123
        ▼
  ingest.py ──► sensor_readings (TimescaleDB)
        │
        ├──► trips + driving_events
        │
        ▼
  predictor.py  (trip-close + hourly job)
        │
        ├── score_battery / score_brakes / score_oil
        ├── baseline wear multiplier (reads sensor_baselines)
        ├── cross-component coupling
        │
        ▼
  component_health  (+ component_wear_events ledger)
        │
        ├── predict_* rules → Alert → suggested WorkOrder
        ├── recompute fleet RAG health
        └── GET /api/v1/cars/{id}/prognostics → Car UI
```

### Triggers

| Trigger | When | Work done |
|---------|------|-----------|
| Trip close | `trips.py` closes a trip | Accumulate brake energy for that trip + full vehicle PME refresh |
| Hourly job | `PREDICTOR_INTERVAL_SECONDS` (default 3600) | Refresh battery/oil/brakes for all vehicles |

PME never runs on the per-packet TCP hot path.

### Key modules

| File | Role |
|------|------|
| `app/server/services/predictor.py` | Scorers, fuzzy RUL, baseline wear, cross-component, rules |
| `app/server/services/baselines.py` | Writes 30-day `sensor_baselines` (PME consumes them) |
| `app/server/services/health.py` | Folds low PME scores into fleet RAG color |
| `app/server/models.py` | `ComponentHealth`, `ComponentWearEvent` |
| `app/server/api/cars.py` | `GET /cars/{id}/prognostics` |
| `web/src/pages/CarPage.tsx` | Fuzzy range display |

---

## 3. Data model

### `component_health` (one row per vehicle)

| Field | Meaning |
|-------|---------|
| `battery_score`, `brake_score`, `oil_score` | 0–100, higher = healthier |
| `battery_rul_days` | Expected advisory days (score buckets — **not** electrochemical RUL) |
| `battery_rul_days_lo` / `_hi` | Pessimistic / optimistic advisory days |
| `brake_remaining_km` | Expected remaining pad life (km) |
| `brake_remaining_km_lo` / `_hi` | Fuzzy bounds |
| `oil_remaining_km` | Expected remaining service distance (km) |
| `oil_remaining_km_lo` / `_hi` | Fuzzy bounds |
| `brake_energy_mj_total` | Cumulative pad friction energy since last brake service |
| `drivers` | JSONB explainability payload per component |

### `component_wear_events` (append-only ledger)

| `event_kind` | Use |
|--------------|-----|
| `wear` | Brake energy delta per trip (`energy_mj`, `distance_km`, …) |
| `score` | Material score change snapshot |
| `reset` | Service completed (work order) — clears component state |

### Vehicle service anchors

- `last_oil_change_at` / `last_oil_change_odo`
- `last_brake_service_at` / `last_brake_service_odo`
- `brake_pad_capacity_mj`, `mass_kg`, `regen_fraction`

---

## 4. Shared helpers

### Health clamp

\[
\text{score} = \mathrm{clamp}(v,\, 0,\, 100)
\]

### Fuzzy remaining from wear rates

Given remaining budget \(B\) and positive wear rates \(\{r_i\}\):

\[
\begin{aligned}
r_{\mathrm{mean}} &= \overline{r_i} \\
r_{\mathrm{opt}} &= P_{10}(r_i) \quad\text{(gentle)} \\
r_{\mathrm{pes}} &= P_{90}(r_i) \quad\text{(harsh)} \\
\text{expected} &= B / r_{\mathrm{mean}} \\
\text{optimistic (hi)} &= B / r_{\mathrm{opt}} \\
\text{pessimistic (lo)} &= B / r_{\mathrm{pes}}
\end{aligned}
\]

Convention: **lo = fails sooner**, **hi = lasts longer**.

---

## 5. Battery model

### Inputs

- Resting voltage (ignition OFF, 7-day median)
- Crank voltage drop at trip starts (pre vs min in first 5 s)
- Post-trip recovery time to near-resting
- Short-trip ratio (trips &lt; 10 min over 7 days)

Sensor preference: `vehicle_battery_voltage` → `control_module_voltage` → `battery_voltage`.

### Scoring (heuristic penalties from 100)

| Condition | Penalty |
|-----------|---------|
| Resting &lt; 12.0 V | −40 |
| Resting &lt; 12.2 V | −25 |
| Resting &lt; 12.4 V | −10 |
| Avg crank drop &gt; 1.2 V | −25 (cap with trend bump) |
| Avg crank drop &gt; 0.8 V | −15 |
| Crank worsening ≥20% vs prior window | +10 on crank pen (cap 35) |
| Recovery &gt; 1200 s | −15 |
| Recovery &gt; 600 s | −10 |
| Short-trip ratio &gt; 0.8 | −10 |
| Short-trip ratio &gt; 0.6 | −5 |

If no usable voltage signals → score is **null** (do not invent “healthy”).

### Advisory RUL (buckets — not chemistry)

| Score | Expected advisory days |
|-------|------------------------|
| ≥ 80 | 90 |
| ≥ 50 | 30 |
| ≥ 30 | 14 |
| ≥ 15 | 7 |
| else | 3 |

Fuzzy band: evaluate buckets at `score ± 12` → `(lo, expected, hi)`.

### Alert

Fire `predict_battery` when score &lt; 40 **or** advisory days &lt; configured warn threshold (default 30).

---

## 6. Brake model

### Cumulative damage (physics)

On each closed trip, integrate decelerations from the speed series:

\[
\Delta KE = \tfrac{1}{2} m (v_{\mathrm{prev}}^2 - v^2)
\]

| Deceleration | Pad energy counted |
|--------------|-------------------|
| ≥ `brake_decel_g` (default 0.25 g) | Full \(\Delta KE\) |
| ≥ `light_brake_g` (default 0.10 g) | `light_brake_fraction` × \(\Delta KE\) (default 0.25) |

Hybrid/regen: only pad share accumulates:

\[
E_{\mathrm{pad}} = E_{\mathrm{total}} \times (1 - f_{\mathrm{regen}})
\]

Fallback if no speed series: each device `harsh_brake` event ≈ KE of a 50→20 km/h stop.

Accumulate into `brake_energy_mj_total`. Ledger row: `event_kind=wear`.

### Health score

\[
\text{brake\_score} = 100 \times \left(1 - \frac{E_{\mathrm{total}}}{E_{\mathrm{pad\_capacity}}}\right)
\]

Default pad capacity: 800 MJ (settings / per-vehicle override).

### Fuzzy remaining km

1. Preferred: per-trip rates \(E_{\mathrm{mj}} / d_{\mathrm{km}}\) over **30 days** → `_fuzzy_remaining_from_rates(remaining_MJ, rates)`.
2. Fallback: 7-day mean MJ/km with ±30% intensity band.

### Alert

Fire `predict_brakes` when score &lt; 25.

### Service reset

Completing a predictive brake work order zeros `brake_energy_mj_total` and clears RUL fields; sets `last_brake_service_*`.

---

## 7. Oil model

**Not** a viscosity / TAN / oil-chemistry model. It is **schedule + stress**.

### Base schedule

\[
\begin{aligned}
d &= \text{km since last oil change (or trip-sum proxy)} \\
p &= \min(1,\, d / D_{\mathrm{interval}}) \quad (D_{\mathrm{interval}} \approx 10{,}000\,\mathrm{km}) \\
\text{score}_0 &= 100 - 60p
\end{aligned}
\]

### Stress penalties (7-day window)

| Stress | Rule | Cap |
|--------|------|-----|
| Thermal | Minutes with oil/coolant &gt; 110 °C × 0.5, then × baseline multiplier | 15 (before mult) |
| Cold short trips | Trips &lt; 10 min that never reach 80 °C coolant: +2 each | 15 |
| Idle | Idle ratio &gt; 0.35 → −5; &gt; 0.2 → −3 | — |
| Load / RPM | Minutes RPM &gt; 4000 or load &gt; 80% | 5 |

### Remaining km

Target: project until score ≈ 20 at current wear-per-km:

\[
\text{wear/km} = (100 - \text{score}) / d
\]

\[
\text{remaining} = (\text{score} - 20) / (\text{wear/km})
\]

Fuzzy:

- **Optimistic (hi):** distance-only wear (stress stripped)
- **Pessimistic (lo):** wear × baseline mult × stress factor (1.05–1.15)
- Fresh oil (score ≥ 90): interval leftover × {0.85, 1.0, 1.1}

### Alert

Fire `predict_oil` when score &lt; 30.

---

## 8. Thirty-day baseline wear acceleration

**Problem:** Hardcoded “coolant bad at 100 °C” fails across vehicle types.  
**Solution:** Use each car’s own 30-day baseline from `baselines.py`.

### Z-score

For sensors `coolant_temperature`, `engine_oil_temperature`:

\[
z = \frac{\mu_{\mathrm{recent\,3h}} - \mu_{30d}}{\sigma_{30d}}
\]

Recent mean: prefer `sensor_readings_1h`; fall back to raw `sensor_readings` if the continuous aggregate is empty.

### Multiplier (bounded 1.0–1.5)

If \(\max z \ge 2\):

\[
m = \min\!\bigl(1.5,\; 1.0 + 0.1 + 0.2\,(z - 2)\bigr)
\]

| z | Multiplier (approx) |
|---|---------------------|
| &lt; 2 | 1.0 |
| 2 | 1.1 |
| 4 | 1.5 (cap) |

Applied to:

1. Oil thermal penalty  
2. Oil remaining-km band (expected ÷ \(m\); optimistic softer; pessimistic harder)

Recorded in `drivers.oil.baseline_wear` / `baseline_multiplier`.

---

## 9. Cross-component contamination

Applied in `update_vehicle` **after** individual scorers, **before** persist:

| Condition | Effect |
|-----------|--------|
| Oil `baseline_multiplier` &gt; 1.05 | Set oil `cross_component` / `top_reason`: “Oil wear accelerated by overheating trend” |
| Same + mult &gt; 1.2 | Slightly tighten brake RUL (fade / more pad use under heat); note in brake `drivers` |
| Battery score &lt; 40 | Set `phantom_dtc_risk` + note to verify voltage before chasing electrical DTCs — **does not suppress real DTCs** |

Explainability is intentional: the Car page “top reason” surfaces these links.

---

## 10. Fuzzy RUL product contract

| Component | Expected field | Lo / Hi | Physical meaning |
|-----------|----------------|---------|------------------|
| Battery | `battery_rul_days` | `_lo` / `_hi` | Advisory calendar days from score buckets |
| Brakes | `brake_remaining_km` | `_lo` / `_hi` | Pad life from MJ budget ÷ wear rate |
| Oil | `oil_remaining_km` | `_lo` / `_hi` | Distance until score ~20 at current pace |

**Rules and fleet RAG use the mid/expected score only** so alerts stay stable while the UI shows the trapezoid range.

UI formatting (`web/src/format.ts`):

- Distinct bounds → `between X–Y km` / `Advisory X–Yd`
- Equal bounds → `~N km` / `Advisory ~Nd`

---

## 11. API & UI surface

### `GET /api/v1/cars/{id}/prognostics`

Returns scores, expected RUL, lo/hi fields, `drivers`, `updated_at`, and `collecting` when no row exists yet.

### Compact prognostics (Home / fleet cards)

Mid scores only (`battery`, `brakes`, `oil`) — ranges reserved for the Car page.

### Work-order loop

1. PME score crosses threshold → `predict_*` rule fires  
2. Alert + suggested work order  
3. On complete → `reset_component` restores score / clears cumulative wear / sets service anchors  
4. Optional `FailureEvent` labeling feeds ML evaluation (separate path)

---

## 12. Configuration knobs

Stored via settings store (defaults in parentheses):

| Key | Default | Role |
|-----|---------|------|
| `predict.brake_pad_capacity_mj` | 800 | Pad energy budget |
| `predict.brake_decel_g` | 0.25 | Hard brake threshold |
| `predict.light_brake_g` | 0.10 | Light brake threshold |
| `predict.light_brake_fraction` | 0.25 | Fraction of ΔKE for light stops |
| `predict.regen_fraction` | 0.0 | Global hybrid regen share |
| `predict.battery_warn_rul_days` | 30 | Advisory days alert threshold |
| `predict.oil_interval_km` | 10000 | Oil distance schedule |
| `predict.mass_kg_default` | 1500 | KE mass if vehicle unset |

Env: `PREDICTOR_INTERVAL_SECONDS` (hourly batch).

---

## 13. What this is not

- Not Isolation Forest / `/predictive` ML anomaly scores  
- Not true electrochemical battery RUL or pad thickness measurement  
- Not oil lab chemistry (TAN, viscosity, soot)  
- Not a rewrite to naïve “1 harsh brake = 15 km” formulas — the KE model is preferred  
- Not a substitute for OEM service schedules — it **augments** them with usage and stress

---

## 14. Extending to new components

Reuse the same pattern:

1. Add scorer → `(score, rul_lo, rul_mid, rul_hi, drivers)`  
2. Persist columns / JSON on `component_health`  
3. Append `component_wear_events` for cumulative damage  
4. Seed `predict_<component>` rule with auto work order  
5. Wire reset on work-order complete  
6. Show mid score on Overview; fuzzy range on Car page  

Candidates: cooling system, tires, transmission — only when sensor coverage supports a physics or schedule+stress model.

---

## 15. Review checklist

| Item | Status |
|------|--------|
| Cumulative damage health (0–100) | Implemented |
| Brake KE + pad capacity | Implemented |
| Battery voltage / crank / recovery heuristics | Implemented |
| Oil schedule + stress | Implemented |
| Fuzzy RUL lo/expected/hi | Implemented |
| 30-day baseline → wear multiplier | Implemented |
| Cross-component drivers | Implemented |
| Explainable `drivers` + Car UI ranges | Implemented |
| Alert → WorkOrder → reset | Implemented |
| ML Isolation Forest coupling | Intentionally out of scope |

---

## 16. Reference formula card (quick)

**Brakes**

\[
E += \Delta KE_{\mathrm{hard}} + f_{\mathrm{light}}\Delta KE_{\mathrm{light}},\quad
\text{score} = 100\,(1 - E/E_{\mathrm{cap}}),\quad
\text{RUL}_{\mathrm{km}} = (E_{\mathrm{cap}}-E)\,/\,r_{\mathrm{MJ/km}}
\]

**Battery**

\[
\text{score} = 100 - \sum \text{penalties}(V_{\mathrm{rest}}, \Delta V_{\mathrm{crank}}, t_{\mathrm{recover}}, r_{\mathrm{short}})
\]

**Oil**

\[
\text{score} = 100 - 60\frac{d}{D} - P_{\mathrm{thermal}}\,m_z - P_{\mathrm{cold}} - P_{\mathrm{idle}} - P_{\mathrm{load}}
\]

**Baseline multiplier**

\[
m_z = f\!\left(\max z_{\mathrm{coolant,\,oil\,temp}}\right) \in [1.0,\, 1.5]
\]
