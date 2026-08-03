// Home / Dashboard: fleet stat cards + anything needing attention + every car
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell, CarFront, CheckCircle2, ClipboardList, Gauge, HeartPulse,
  Route, Siren, Sparkles, WifiOff, Wrench, type LucideIcon,
} from "lucide-react";
import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api, qk } from "../api";
import AlertCard from "../components/AlertCard";
import EmptyState from "../components/EmptyState";
import HealthRing from "../components/HealthRing";
import VitalTile from "../components/VitalTile";
import { HEALTH_META, timeAgo } from "../format";
import type { LiveState, OverviewCar } from "../types";
import { useWs } from "../ws";

// Which sensors make the card's glance row (first found wins per slot)
const GLANCE: { keys: string[]; name: string; decimals: number }[] = [
  { keys: ["vehicle_speed_obd", "vehicle_speed"], name: "Speed", decimals: 0 },
  { keys: ["engine_rpm"], name: "RPM", decimals: 0 },
  { keys: ["coolant_temperature"], name: "Coolant", decimals: 0 },
  { keys: ["fuel_level"], name: "Fuel", decimals: 0 },
  { keys: ["control_module_voltage", "vehicle_battery_voltage", "battery_voltage"], name: "Battery", decimals: 1 },
];

export default function OverviewPage() {
  const queryClient = useQueryClient();
  const { subscribe } = useWs();
  const { data: overview } = useQuery({
    queryKey: qk.overview,
    queryFn: api.overview,
    refetchInterval: 30_000,
  });
  const { data: alerts } = useQuery({
    queryKey: qk.alerts("active"),
    queryFn: () => api.alerts("active"),
    refetchInterval: 60_000,
  });
  const { data: summary } = useQuery({
    queryKey: qk.summary,
    queryFn: api.summary,
    refetchInterval: 60_000,
  });
  const { data: driving } = useQuery({
    queryKey: qk.driving,
    queryFn: api.drivingSummary,
    refetchInterval: 60_000,
  });

  // Live patches land straight in the overview cache — no refetch per message.
  useEffect(() => {
    return subscribe("telemetry", (data: LiveState & { vehicle_id: number }) => {
      queryClient.setQueryData<OverviewCar[]>(qk.overview, (old) =>
        old?.map((c) =>
          c.id === data.vehicle_id
            ? { ...c, live: { ...data }, last_seen: data.last_seen }
            : c,
        ),
      );
    });
  }, [subscribe, queryClient]);

  useEffect(() => {
    return subscribe("health", (data: { vehicle_id: number; health: OverviewCar["health"] }) => {
      queryClient.setQueryData<OverviewCar[]>(qk.overview, (old) =>
        old?.map((c) => (c.id === data.vehicle_id ? { ...c, health: data.health } : c)),
      );
    });
  }, [subscribe, queryClient]);

  if (overview && overview.length === 0) {
    return (
      <EmptyState
        icon={CarFront}
        title="No cars yet"
        body="Add your first car to start seeing its health, live sensors, and driving insights."
        action={
          <Link to="/settings" className="btn-primary mt-2">
            + Add a car
          </Link>
        }
      />
    );
  }

  // ── Stat card numbers ──────────────────────────────────────────────────
  const cars = overview ?? [];
  const healthy = cars.filter((c) => c.health === "green").length;
  const attentionCars = cars.filter((c) => c.health === "yellow" || c.health === "red").length;
  const offline = cars.filter((c) => c.health === "grey").length;
  const openWorkOrders = cars.reduce((sum, c) => sum + (c.open_work_orders ?? 0), 0);

  const scores = (driving ?? [])
    .map((d) => d.today_score ?? d.avg_score_7d)
    .filter((v): v is number => v !== null && v !== undefined);
  const avgScore = scores.length
    ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length)
    : null;
  const trips14d = (driving ?? []).reduce((sum, d) => sum + d.trips_14d, 0);

  return (
    <div className="space-y-6">
      {/* ── Stat cards ─────────────────────────────────────────────────── */}
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={HeartPulse} title="Fleet Health">
          <StatRow icon={CheckCircle2} tint="bg-emerald-100 text-emerald-600" label="All good" value={healthy} />
          <StatRow icon={Bell} tint="bg-amber-100 text-amber-600" label="Need attention" value={attentionCars} />
          <StatRow icon={WifiOff} tint="bg-slate-200 text-slate-500" label="Offline" value={offline} />
        </StatCard>

        <StatCard icon={Bell} title="Alerts">
          <StatRow icon={Bell} tint="bg-blue-100 text-blue-600" label="Active" value={summary?.alerts_total ?? 0} />
          <StatRow icon={Siren} tint="bg-red-100 text-red-600" label="Urgent" value={summary?.urgent ?? 0} />
        </StatCard>

        <StatCard icon={Wrench} title="Maintenance">
          <StatRow icon={Sparkles} tint="bg-violet-100 text-violet-600" label="Suggested" value={summary?.suggested ?? 0} />
          <StatRow icon={ClipboardList} tint="bg-pink-100 text-pink-600" label="Open to-dos" value={openWorkOrders} />
        </StatCard>

        <StatCard icon={Gauge} title="Driving">
          <StatRow icon={Gauge} tint="bg-amber-100 text-amber-600" label="Avg score" value={avgScore ?? "—"} />
          <StatRow icon={Route} tint="bg-indigo-100 text-indigo-600" label="Trips · 14 days" value={trips14d} />
        </StatCard>
      </section>

      {/* ── Alerts needing action ──────────────────────────────────────── */}
      {alerts && alerts.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-3">
            Needs attention
          </h2>
          <div className="space-y-2.5">
            {alerts.map((a) => (
              <AlertCard key={a.id} alert={a} />
            ))}
          </div>
        </section>
      )}

      {/* ── Cars ───────────────────────────────────────────────────────── */}
      <section>
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-3">
          Your cars
        </h2>
        <div className="grid gap-4 md:grid-cols-2">
          {overview?.map((car) => (
            <CarCard key={car.id} car={car} />
          ))}
        </div>
      </section>
    </div>
  );
}

// ── Stat card (console style: header + pastel-icon stat rows) ────────────────
function StatCard({
  icon: Icon,
  title,
  children,
}: {
  icon: LucideIcon;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="card overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-line/70">
        <Icon size={15} className="text-muted" />
        <span className="text-sm font-semibold text-slate-800">{title}</span>
      </div>
      <div className="p-4 space-y-3.5">{children}</div>
    </div>
  );
}

function StatRow({
  icon: Icon,
  tint,
  label,
  value,
}: {
  icon: LucideIcon;
  tint: string;
  label: string;
  value: number | string;
}) {
  return (
    <div className="flex items-center gap-3">
      <div className={`stat-icon ${tint}`}>
        <Icon size={17} />
      </div>
      <div className="min-w-0">
        <div className="text-xs text-muted truncate">{label}</div>
        <div className="text-xl font-bold text-slate-900 tabular-nums leading-tight">{value}</div>
      </div>
    </div>
  );
}

function CarCard({ car }: { car: OverviewCar }) {
  const meta = HEALTH_META[car.health];
  const sensors = car.live?.sensors ?? {};
  const glance = GLANCE.map((g) => {
    const hit = g.keys.map((k) => sensors[k]).find(Boolean);
    return { ...g, value: hit?.value ?? null, unit: hit?.unit ?? "" };
  });
  const activeAlerts = car.alerts?.total ?? 0;

  return (
    <Link to={`/cars/${car.id}`} className="card card-hover p-5 block">
      <div className="flex items-start gap-4">
        <HealthRing health={car.health} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-lg font-semibold text-slate-900 truncate">{car.name}</h3>
            {car.license_plate && (
              <span className="chip bg-ink-800 text-muted">{car.license_plate}</span>
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-xs">
            <span className="font-medium" style={{ color: meta.color }}>{meta.label}</span>
            <span className="text-muted">· {timeAgo(car.last_seen)}</span>
            {car.live?.ignition && <span className="chip bg-ok/10 text-ok">Driving</span>}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          {(car.alerts?.critical ?? 0) > 0 && (
            <span className="chip bg-bad/10 text-bad">{car.alerts.critical} urgent</span>
          )}
          {(car.alerts?.warning ?? 0) > 0 && (
            <span className="chip bg-warn/10 text-warn">{car.alerts.warning} check</span>
          )}
          {activeAlerts === 0 && car.open_work_orders > 0 && (
            <span className="chip bg-brand/10 text-brand">
              {car.open_work_orders} to-do{car.open_work_orders > 1 ? "s" : ""}
            </span>
          )}
        </div>
      </div>

      <div className="mt-4 grid grid-cols-3 sm:grid-cols-5 gap-2">
        {glance.map((g) => (
          <VitalTile
            key={g.name}
            name={g.name}
            value={g.value}
            unit={g.unit}
            decimals={g.decimals}
            onClick={() => {}}
          />
        ))}
      </div>
    </Link>
  );
}
