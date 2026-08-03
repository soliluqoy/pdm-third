// PREDICT v3 — app shell: slim header + bottom-tab nav (mobile) / top nav (desktop)
import { Activity, Bell, Car, Gauge, Settings as SettingsIcon, Wrench } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api, qk } from "../api";
import { useWs } from "../ws";

const NAV = [
  { to: "/", label: "Home", icon: Car, end: true },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/maintenance", label: "Maintenance", icon: Wrench },
  { to: "/driving", label: "Driving", icon: Gauge },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

export default function Layout() {
  const { connected } = useWs();
  const { data: summary } = useQuery({
    queryKey: qk.summary,
    queryFn: api.summary,
    refetchInterval: 60_000,
  });
  const alertCount = summary?.alerts_total ?? 0;
  const attention = alertCount + (summary?.suggested ?? 0);

  return (
    <div className="min-h-dvh flex flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-ink-950/85 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 h-14 flex items-center gap-4">
          <NavLink to="/" className="flex items-center gap-2 font-bold tracking-wide">
            <Activity size={20} className="text-accent" strokeWidth={2.6} />
            <span className="text-slate-100">PREDICT</span>
          </NavLink>

          <nav className="hidden sm:flex items-center gap-1 ml-6">
            {NAV.map(({ to, label, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  clsx(
                    "px-3 py-1.5 rounded-lg text-sm transition-colors",
                    isActive
                      ? "bg-accent-soft text-accent font-semibold"
                      : "text-muted hover:text-slate-200",
                  )
                }
              >
                {label}
                {label === "Alerts" && alertCount > 0 && (
                  <span className="ml-1.5 chip bg-bad/15 text-bad">{alertCount}</span>
                )}
                {label === "Maintenance" && (summary?.suggested ?? 0) > 0 && (
                  <span className="ml-1.5 chip bg-warn/15 text-warn">{summary!.suggested}</span>
                )}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2 text-xs text-muted">
            <span
              className={clsx(
                "inline-block w-2 h-2 rounded-full",
                connected ? "bg-ok" : "bg-bad animate-pulse",
              )}
            />
            {connected ? "Live" : "Reconnecting…"}
          </div>
        </div>
      </header>

      <main className="flex-1 mx-auto w-full max-w-6xl px-4 py-5 pb-24 sm:pb-8">
        <Outlet />
      </main>

      {/* Mobile bottom tabs */}
      <nav className="sm:hidden fixed bottom-0 inset-x-0 z-20 border-t border-line bg-ink-950/95 backdrop-blur">
        <div className="grid grid-cols-5">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(
                  "relative flex flex-col items-center gap-0.5 py-2.5 text-[11px]",
                  isActive ? "text-accent" : "text-muted",
                )
              }
            >
              <Icon size={20} />
              {label}
              {label === "Alerts" && attention > 0 && (
                <span className="absolute top-1 right-[22%] min-w-[16px] h-4 px-1 rounded-full bg-bad text-ink-950 text-[10px] font-bold flex items-center justify-center">
                  {attention}
                </span>
              )}
            </NavLink>
          ))}
        </div>
      </nav>
    </div>
  );
}