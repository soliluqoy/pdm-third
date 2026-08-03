// Car detail: live vitals by system, history charts, and the full timeline
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, MapPin, Power } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, qk } from "../api";
import HealthRing from "../components/HealthRing";
import SensorChart from "../components/SensorChart";
import VitalTile from "../components/VitalTile";
import { HEALTH_META, fmtDateTime, timeAgo } from "../format";
import type { LiveState, TimelineEvent, Vitals } from "../types";
import { useWs } from "../ws";

const RANGES = [
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 24 * 7 },
  { label: "30d", hours: 24 * 30 },
  { label: "1y", hours: 24 * 365 },
];

const KIND_META: Record<TimelineEvent["kind"], { label: string; classes: string }> = {
  alert: { label: "Alert", classes: "bg-bad/15 text-bad" },
  work_order: { label: "Work order", classes: "bg-accent/15 text-accent" },
  maintenance: { label: "Fixed", classes: "bg-ok/15 text-ok" },
  dtc: { label: "Fault code", classes: "bg-warn/15 text-warn" },
  health: { label: "Status", classes: "bg-ink-800 text-muted" },
  trip: { label: "Trip", classes: "bg-ink-800 text-slate-300" },
};

export default function CarPage() {
  const { id } = useParams();
  const carId = Number(id);
  const queryClient = useQueryClient();
  const { subscribe } = useWs();

  const { data: car } = useQuery({ queryKey: qk.car(carId), queryFn: () => api.car(carId) });
  const { data: vitals } = useQuery({
    queryKey: qk.vitals(carId),
    queryFn: () => api.vitals(carId),
    refetchInterval: 30_000,
  });
  const { data: timeline } = useQuery({
    queryKey: qk.timeline(carId),
    queryFn: () => api.timeline(carId),
    refetchInterval: 60_000,
  });

  const [sensor, setSensor] = useState<string | null>(null);
  const [hours, setHours] = useState(24);

  // Patch live sensors into the vitals cache as WS telemetry arrives.
  useEffect(() => {
    return subscribe("telemetry", (data: LiveState & { vehicle_id: number }) => {
      if (data.vehicle_id !== carId) return;
      queryClient.setQueryData<Vitals>(qk.vitals(carId), (old) => {
        if (!old) return old;
        const groups = old.groups.map((g) => ({
          ...g,
          sensors: g.sensors.map((s) => {
            const hit = data.sensors?.[s.sensor_type];
            return hit ? { ...s, value: hit.value, ts: hit.ts } : s;
          }),
        }));
        return {
          ...old,
          groups,
          live: data.live,
          last_seen: data.last_seen,
          ignition: data.ignition,
          gps: data.gps,
        };
      });
    });
  }, [subscribe, queryClient, carId]);

  const chartSensors = useMemo(
    () => vitals?.groups.flatMap((g) => g.sensors) ?? [],
    [vitals],
  );
  const activeSensor = sensor ?? chartSensors[0]?.sensor_type ?? null;
  const { data: history } = useQuery({
    queryKey: ["sensor-history", carId, activeSensor, hours],
    queryFn: () => api.history(carId, activeSensor!, hours),
    enabled: activeSensor !== null,
  });

  if (!car) return null;
  const meta = HEALTH_META[car.health];

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-start gap-4">
        <Link to="/" className="btn-ghost !p-2 mt-1" aria-label="Back">
          <ArrowLeft size={18} />
        </Link>
        <HealthRing health={car.health} size={64} />
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl font-bold text-slate-100 truncate">{car.name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
            <span style={{ color: meta.color }}>{meta.label}</span>
            <span>seen {timeAgo(car.last_seen)}</span>
            {vitals?.ignition !== null && vitals?.ignition !== undefined && (
              <span className="inline-flex items-center gap-1">
                <Power size={12} className={vitals.ignition ? "text-ok" : "text-muted"} />
                {vitals.ignition ? "Engine on" : "Engine off"}
              </span>
            )}
            {vitals?.gps?.latitude && (
              <span className="inline-flex items-center gap-1">
                <MapPin size={12} />
                {vitals.gps.latitude.toFixed(4)}, {vitals.gps.longitude?.toFixed(4)}
              </span>
            )}
            {car.today_score !== null && (
              <Link to="/driving" className="chip bg-accent/15 text-accent">
                Driving score {car.today_score}
              </Link>
            )}
          </div>
        </div>
      </div>

      {/* Vitals by system */}
      {vitals?.groups.length ? (
        vitals.groups.map((g) => (
          <section key={g.group}>
            <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-2.5">
              {g.label}
            </h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
              {g.sensors.map((s) => (
                <VitalTile
                  key={s.sensor_type}
                  name={s.name}
                  value={s.value}
                  unit={s.unit}
                  decimals={s.decimals}
                  onClick={() => setSensor(s.sensor_type)}
                />
              ))}
            </div>
          </section>
        ))
      ) : (
        <div className="card p-6 text-sm text-muted">
          No live data yet — vitals appear here as soon as the tracker reports.
        </div>
      )}

      {/* History */}
      {activeSensor && (
        <section className="card p-4">
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <select
              className="input !w-auto"
              value={activeSensor}
              onChange={(e) => setSensor(e.target.value)}
            >
              {chartSensors.map((s) => (
                <option key={s.sensor_type} value={s.sensor_type}>
                  {s.name}
                </option>
              ))}
            </select>
            <div className="flex gap-1 ml-auto">
              {RANGES.map((r) => (
                <button
                  key={r.label}
                  onClick={() => setHours(r.hours)}
                  className={
                    hours === r.hours
                      ? "btn-primary !px-3 !py-1 !text-xs"
                      : "btn-ghost !px-3 !py-1 !text-xs"
                  }
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>
          {history && (
            <SensorChart
              points={history.points}
              unit={history.unit}
              decimals={history.decimals}
            />
          )}
        </section>
      )}

      {/* Timeline */}
      <section>
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-2.5">
          Everything that happened
        </h2>
        <div className="space-y-1.5">
          {timeline?.events.length ? (
            timeline.events.map((e, idx) => {
              const k = KIND_META[e.kind];
              return (
                <div key={`${e.kind}-${e.ref_id}-${idx}`} className="card px-4 py-2.5 flex items-center gap-3">
                  <span className={`chip ${k.classes} shrink-0 w-20 justify-center`}>{k.label}</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-slate-200 truncate">{e.title}</div>
                    {e.detail && (
                      <div className="text-xs text-muted truncate">{e.detail}</div>
                    )}
                  </div>
                  <span className="text-xs text-muted shrink-0">{fmtDateTime(e.ts)}</span>
                </div>
              );
            })
          ) : (
            <div className="card p-6 text-sm text-muted">Nothing recorded yet.</div>
          )}
        </div>
      </section>
    </div>
  );
}
