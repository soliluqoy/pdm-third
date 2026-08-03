// Alerts: active alerts + full history (alerts are never deleted)
import { useQuery } from "@tanstack/react-query";
import { Bell, History } from "lucide-react";
import { useState } from "react";
import { api, qk } from "../api";
import AlertCard from "../components/AlertCard";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, SEVERITY_META } from "../format";

type Tab = "active" | "history";

export default function AlertsPage() {
  const [tab, setTab] = useState<Tab>("active");
  const { data: active } = useQuery({
    queryKey: qk.alerts("active"),
    queryFn: () => api.alerts("active"),
    refetchInterval: 30_000,
  });
  const { data: history } = useQuery({
    queryKey: qk.alerts("all"),
    queryFn: () => api.alerts("all"),
    enabled: tab === "history",
  });

  const closed = history?.filter((a) => a.status !== "active") ?? [];

  return (
    <div className="space-y-5">
      <div className="card inline-flex items-center gap-1 p-1">
        <button
          className={tab === "active" ? "tab-pill tab-pill-active" : "tab-pill"}
          onClick={() => setTab("active")}
        >
          <Bell size={15} /> Active
          {active?.length ? (
            <span className="chip bg-brand/10 text-brand !px-2 tabular-nums">{active.length}</span>
          ) : null}
        </button>
        <button
          className={tab === "history" ? "tab-pill tab-pill-active" : "tab-pill"}
          onClick={() => setTab("history")}
        >
          <History size={15} /> History
        </button>
      </div>

      {tab === "active" && (
        <div className="space-y-2.5">
          {active?.length ? (
            active.map((a) => <AlertCard key={a.id} alert={a} />)
          ) : (
            <EmptyState
              icon={Bell}
              title="No active alerts"
              body="When a rule fires — overheating, low battery, a fault code — it shows up here."
            />
          )}
        </div>
      )}

      {tab === "history" && (
        <div className="space-y-1.5">
          {closed.length ? (
            closed.map((a) => {
              const sev = SEVERITY_META[a.severity];
              return (
                <div key={a.id} className="card px-4 py-3 flex items-center gap-3">
                  <span className={`chip ${sev.classes} shrink-0`}>{sev.label}</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-slate-700 truncate">
                      {a.title}
                      {a.occurrence_count > 1 && (
                        <span className="text-muted"> ×{a.occurrence_count}</span>
                      )}
                    </div>
                    <div className="text-xs text-muted truncate">
                      {a.vehicle_name} · {a.status}
                    </div>
                  </div>
                  <div className="text-xs text-muted shrink-0 text-right">
                    <div>{fmtDateTime(a.created_at)}</div>
                    {a.resolved_at && <div>→ {fmtDateTime(a.resolved_at)}</div>}
                  </div>
                </div>
              );
            })
          ) : (
            <EmptyState
              icon={History}
              title="No alert history yet"
              body="Resolved and dismissed alerts are kept here forever — the car's full alert record."
            />
          )}
        </div>
      )}
    </div>
  );
}
