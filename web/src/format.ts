// PREDICT — formatting + display helpers (plain language, one place)
import type { Health, Severity, WorkOrderPriority } from "./types";

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fmtValue(v: number | null | undefined, decimals = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (!seconds && seconds !== 0) return "—";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export const HEALTH_META: Record<Health, { label: string; color: string; bg: string }> = {
  green: { label: "All good", color: "#34D399", bg: "rgba(52,211,153,0.12)" },
  yellow: { label: "Check soon", color: "#FBBF24", bg: "rgba(251,191,36,0.12)" },
  red: { label: "Urgent", color: "#F87171", bg: "rgba(248,113,113,0.12)" },
  grey: { label: "Offline", color: "#64748B", bg: "rgba(100,116,139,0.12)" },
};

export const SEVERITY_META: Record<Severity, { label: string; classes: string }> = {
  critical: { label: "Urgent", classes: "bg-bad/15 text-bad" },
  warning: { label: "Check soon", classes: "bg-warn/15 text-warn" },
  info: { label: "FYI", classes: "bg-accent/15 text-accent" },
};

export const PRIORITY_META: Record<WorkOrderPriority, { label: string; classes: string }> = {
  urgent: { label: "Urgent", classes: "bg-bad/15 text-bad" },
  high: { label: "High", classes: "bg-warn/15 text-warn" },
  medium: { label: "Medium", classes: "bg-accent/15 text-accent" },
  low: { label: "Low", classes: "bg-ink-800 text-muted" },
};

export const EVENT_LABELS: Record<string, string> = {
  harsh_accel: "Hard acceleration",
  harsh_brake: "Hard braking",
  harsh_corner: "Hard cornering",
  speeding: "Speeding",
  idling: "Long idle",
  high_rpm: "High RPM",
};

/** What fires each driving flag — shown next to every notable moment. */
export const EVENT_TRIGGERS: Record<string, string> = {
  harsh_accel:
    "Acceleration above the harsh threshold (device Eco-driving, or derived Δspeed/Δt)",
  harsh_brake:
    "Deceleration above the harsh threshold (device Eco-driving, or derived Δspeed/Δt)",
  harsh_corner:
    "Lateral acceleration above the harsh threshold (device Eco-driving corner detect)",
  speeding:
    "Speed above the configured limit (device overspeed AVL, or derived streak)",
  idling:
    "Engine on / in-trip with near-zero speed longer than the idle minutes setting",
  high_rpm:
    "Engine RPM above the high-RPM threshold while the vehicle is moving",
};

/** Device Eco-driving magnitude is wire-encoded as 0.01 m/s²; derived is already m/s². */
function harshMs2(
  value: number | null | undefined,
  source: "device" | "derived" | string | undefined,
): number | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  if (source === "device") return value / 100;
  return value;
}

/** Numeric value in display units (for peak / range across a group). */
export function eventComparableValue(
  type: string,
  value: number | null | undefined,
  source?: "device" | "derived" | string,
): number | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  if (type === "harsh_accel" || type === "harsh_brake" || type === "harsh_corner") {
    return harshMs2(value, source);
  }
  return value;
}

export const EVENT_TYPE_ORDER = [
  "harsh_accel", "harsh_brake", "harsh_corner", "speeding", "idling", "high_rpm",
];

/** Compact measured value for chips / scorecards. */
export function eventValueLabel(
  type: string,
  value: number | null | undefined,
  source?: "device" | "derived" | string,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  switch (type) {
    case "harsh_accel":
    case "harsh_brake":
    case "harsh_corner": {
      const ms2 = harshMs2(value, source);
      return ms2 === null ? "" : `${fmtValue(ms2, 2)} m/s²`;
    }
    case "speeding":
      return `${fmtValue(value)} km/h`;
    case "idling":
      return `${fmtValue(value, 1)} min`;
    case "high_rpm":
      return `${fmtValue(value)} rpm`;
    default:
      return fmtValue(value, 1);
  }
}

/** Explicit one-line explanation of why this flag fired. */
export function eventTriggerDetail(
  type: string,
  value: number | null | undefined,
  source?: "device" | "derived" | string,
): string {
  const via =
    source === "device"
      ? "Device Eco-driving / overspeed flag"
      : source === "derived"
        ? "Server estimate from telemetry samples"
        : "Trigger";

  switch (type) {
    case "harsh_accel": {
      const ms2 = harshMs2(value, source);
      return ms2 === null
        ? `${via}: hard acceleration detected`
        : `${via}: accel reached ${fmtValue(ms2, 2)} m/s²`;
    }
    case "harsh_brake": {
      const ms2 = harshMs2(value, source);
      return ms2 === null
        ? `${via}: hard braking detected`
        : `${via}: deceleration reached ${fmtValue(ms2, 2)} m/s²`;
    }
    case "harsh_corner": {
      const ms2 = harshMs2(value, source);
      return ms2 === null
        ? `${via}: hard cornering detected`
        : `${via}: lateral accel reached ${fmtValue(ms2, 2)} m/s²`;
    }
    case "speeding":
      return value == null
        ? `${via}: overspeed detected`
        : `${via}: speed hit ${fmtValue(value)} km/h`;
    case "idling":
      return value == null
        ? `${via}: long idle detected`
        : `${via}: idled ${fmtValue(value, 1)} minutes with near-zero speed`;
    case "high_rpm":
      return value == null
        ? `${via}: high RPM while moving`
        : `${via}: ${fmtValue(value)} rpm while moving`;
    default:
      return EVENT_TRIGGERS[type] ?? via;
  }
}

export function scoreColor(score: number | null | undefined): string {
  if (score === null || score === undefined) return "#64748B";
  if (score >= 85) return "#34D399";
  if (score >= 70) return "#FBBF24";
  return "#F87171";
}
