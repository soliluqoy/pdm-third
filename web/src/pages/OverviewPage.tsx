// Home / Overview: every car at a glance + anything needing attention
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CarFront, Plus } from "lucide-react";
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
            <Plus size={16} /> Add a car
          </Link>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
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

      <section className="grid gap-4 md:grid-cols-2">
        {overview?.map((car) => (
          <CarCard key={car.id} car={car} />
        ))}
      </section>
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
            <h3 className="text-lg font-semibold text-slate-100 truncate">{car.name}</h3>
            {car.license_plate && (
              <span className="chip bg-ink-800 text-muted">{car.license_plate}</span>
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-xs">
            <span style={{ color: meta.color }}>{meta.label}</span>
            <span className="text-muted">· {timeAgo(car.last_seen)}</span>
            {car.live?.ignition && <span className="chip bg-ok/15 text-ok">Driving</span>}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          {(car.alerts?.critical ?? 0) > 0 && (
            <span className="chip bg-bad/15 text-bad">{car.alerts.critical} urgent</span>
          )}
          {(car.alerts?.warning ?? 0) > 0 && (
            <span className="chip bg-warn/15 text-warn">{car.alerts.warning} check</span>
          )}
          {activeAlerts === 0 && car.open_work_orders > 0 && (
            <span className="chip bg-accent/15 text-accent">
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
