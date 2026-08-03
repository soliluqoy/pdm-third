// PREDICT — app shell: console-style collapsible sidebar + slim header
// (mobile keeps a top bar + bottom tabs)
import {
  Activity, Bell, CarFront, Gauge, LogOut, PanelLeftClose, PanelLeft,
  Settings as SettingsIcon, Wrench,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api, qk } from "../api";
import { useAuth } from "../auth";
import { useWs } from "../ws";

type NavEntry = {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
};

const FLEET_NAV: NavEntry[] = [
  { to: "/", label: "Home", icon: CarFront, end: true },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/maintenance", label: "Maintenance", icon: Wrench },
  { to: "/driving", label: "Driving", icon: Gauge },
];
const SYSTEM_NAV: NavEntry[] = [{ to: "/settings", label: "Settings", icon: SettingsIcon }];
const MOBILE_NAV: NavEntry[] = [...FLEET_NAV, ...SYSTEM_NAV];

function pageTitle(pathname: string): string {
  if (pathname.startsWith("/cars/")) return "Car details";
  if (pathname.startsWith("/alerts")) return "Alerts";
  if (pathname.startsWith("/maintenance")) return "Maintenance";
  if (pathname.startsWith("/driving")) return "Driving";
  if (pathname.startsWith("/settings")) return "Settings";
  return "Dashboard";
}

function BrandMark({ size = 36 }: { size?: number }) {
  return (
    <div
      className="rounded-xl bg-gradient-to-br from-accent via-teal-400 to-brand flex items-center justify-center text-white shadow-sm shrink-0"
      style={{ width: size, height: size }}
    >
      <Activity size={size * 0.52} strokeWidth={2.6} />
    </div>
  );
}

export default function Layout() {
  const { connected } = useWs();
  const { authenticated, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("pdm.nav-collapsed") === "1",
  );
  const toggleCollapsed = () =>
    setCollapsed((c) => {
      localStorage.setItem("pdm.nav-collapsed", c ? "0" : "1");
      return !c;
    });

  const { data: summary } = useQuery({
    queryKey: qk.summary,
    queryFn: api.summary,
    refetchInterval: 60_000,
  });
  const alertCount = summary?.alerts_total ?? 0;
  const suggested = summary?.suggested ?? 0;
  const attention = alertCount + suggested;

  const badgeFor = (label: string) =>
    label === "Alerts" ? alertCount : label === "Maintenance" ? suggested : 0;

  const navLink = ({ to, label, icon: Icon, end }: NavEntry) => (
    <NavLink
      key={to}
      to={to}
      end={end}
      title={label}
      className={({ isActive }) =>
        clsx("nav-item", isActive && "nav-item-active", collapsed && "justify-center !px-0")
      }
    >
      <span className="relative shrink-0">
        <Icon size={19} />
        {collapsed && badgeFor(label) > 0 && (
          <span
            className={clsx(
              "absolute -top-1 -right-1 w-2 h-2 rounded-full",
              label === "Alerts" ? "bg-bad" : "bg-warn",
            )}
          />
        )}
      </span>
      {!collapsed && <span className="flex-1 truncate">{label}</span>}
      {!collapsed && badgeFor(label) > 0 && (
        <span
          className={clsx(
            "chip !px-2 tabular-nums",
            label === "Alerts" ? "bg-bad/10 text-bad" : "bg-warn/10 text-warn",
          )}
        >
          {badgeFor(label)}
        </span>
      )}
    </NavLink>
  );

  return (
    <div className="min-h-dvh">
      {/* ── Sidebar (desktop) ─────────────────────────────────────────── */}
      <aside
        className={clsx(
          "hidden sm:flex fixed inset-y-0 left-0 z-30 flex-col bg-ink-900 border-r border-line transition-[width] duration-200",
          collapsed ? "w-[68px]" : "w-60",
        )}
      >
        <Link
          to="/"
          className={clsx(
            "flex items-center gap-2.5 h-16 px-4 border-b border-line/70 shrink-0",
            collapsed && "justify-center px-0",
          )}
        >
          <BrandMark />
          {!collapsed && (
            <span className="font-bold text-slate-900 tracking-wide">PREDICT</span>
          )}
        </Link>

        <nav className="flex-1 overflow-y-auto px-3 pb-3">
          {!collapsed && <div className="nav-group">Fleet</div>}
          {collapsed && <div className="pt-4" />}
          <div className="space-y-0.5">{FLEET_NAV.map(navLink)}</div>

          {!collapsed && <div className="nav-group">System</div>}
          {collapsed && <div className="pt-4" />}
          <div className="space-y-0.5">{SYSTEM_NAV.map(navLink)}</div>
        </nav>

        <div className="border-t border-line/70 p-3 space-y-1 shrink-0">
          <div
            className={clsx(
              "flex items-center gap-2 px-3 py-1.5 text-xs text-muted",
              collapsed && "justify-center px-0",
            )}
            title={connected ? "Live" : "Reconnecting…"}
          >
            <span
              className={clsx(
                "inline-block w-2 h-2 rounded-full shrink-0",
                connected ? "bg-ok" : "bg-bad animate-pulse",
              )}
            />
            {!collapsed && (connected ? "Live" : "Reconnecting…")}
          </div>
          <button
            type="button"
            onClick={toggleCollapsed}
            className={clsx("nav-item w-full", collapsed && "justify-center !px-0")}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? <PanelLeft size={19} /> : <PanelLeftClose size={19} />}
            {!collapsed && "Collapse sidebar"}
          </button>
        </div>
      </aside>

      {/* ── Main column ───────────────────────────────────────────────── */}
      <div
        className={clsx(
          "min-h-dvh flex flex-col transition-[padding] duration-200",
          collapsed ? "sm:pl-[68px]" : "sm:pl-60",
        )}
      >
        <header className="sticky top-0 z-20 border-b border-line bg-ink-900/85 backdrop-blur">
          <div className="flex items-center gap-3 h-16 px-4 sm:px-6">
            <Link to="/" className="flex sm:hidden items-center gap-2">
              <BrandMark size={30} />
              <span className="font-bold text-slate-900 tracking-wide">PREDICT</span>
            </Link>
            <h1 className="hidden sm:block text-lg font-bold text-slate-900">
              {pageTitle(location.pathname)}
            </h1>

            <div className="ml-auto flex items-center gap-1.5">
              <Link
                to="/alerts"
                className="relative p-2.5 rounded-xl text-slate-500 hover:bg-ink-850 hover:text-slate-800 transition-colors"
                title="Alerts"
              >
                <Bell size={19} />
                {attention > 0 && (
                  <span className="absolute top-1 right-1 min-w-[18px] h-[18px] px-1 rounded-full bg-bad text-white text-[10px] font-bold flex items-center justify-center">
                    {attention}
                  </span>
                )}
              </Link>
              <div className="hidden sm:flex items-center gap-2 pl-2 text-xs text-muted">
                <span
                  className={clsx(
                    "inline-block w-2 h-2 rounded-full",
                    connected ? "bg-ok" : "bg-bad animate-pulse",
                  )}
                />
                {connected ? "Live" : "Reconnecting…"}
              </div>
              {authenticated && (
                <button
                  type="button"
                  onClick={async () => {
                    setLoggingOut(true);
                    await logout();
                    navigate("/login", { replace: true });
                  }}
                  disabled={loggingOut}
                  className="btn-ghost !py-1.5 !px-2.5 text-xs"
                  title="Sign out"
                >
                  <LogOut size={15} />
                  <span className="hidden sm:inline">Sign out</span>
                </button>
              )}
            </div>
          </div>
        </header>

        <main className="flex-1 w-full max-w-6xl mx-auto px-4 sm:px-6 py-6 pb-24 sm:pb-10">
          <Outlet />
        </main>

        {/* ── Mobile bottom tabs ──────────────────────────────────────── */}
        <nav className="sm:hidden fixed bottom-0 inset-x-0 z-20 border-t border-line bg-ink-900/95 backdrop-blur">
          <div className="grid grid-cols-5">
            {MOBILE_NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  clsx(
                    "relative flex flex-col items-center gap-0.5 py-2.5 text-[11px] transition-colors",
                    isActive ? "text-accent font-medium" : "text-muted",
                  )
                }
              >
                <Icon size={20} />
                {label}
                {label === "Alerts" && attention > 0 && (
                  <span className="absolute top-1 right-[22%] min-w-[16px] h-4 px-1 rounded-full bg-bad text-white text-[10px] font-bold flex items-center justify-center">
                    {attention}
                  </span>
                )}
              </NavLink>
            ))}
          </div>
        </nav>
      </div>
    </div>
  );
}
