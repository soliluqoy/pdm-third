// History chart (recharts wrapper): line for raw, band (min–max + avg) for buckets
import {
  Area, AreaChart, CartesianGrid, ComposedChart, Line, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { fmtValue } from "../format";
import type { HistoryPoint } from "../types";

const axisStyle = { fontSize: 11, fill: "#98A2B3" };

export default function SensorChart({
  points,
  unit,
  decimals,
}: {
  points: HistoryPoint[];
  unit?: string;
  decimals?: number;
}) {
  const hasBand = points.some((p) => p.min !== undefined && p.max !== undefined);
  const data = points.map((p) => ({
    ...p,
    band: hasBand && p.min !== undefined && p.max !== undefined
      ? [p.min, p.max]
      : undefined,
    label: new Date(p.ts).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    }),
  }));

  if (data.length === 0) {
    return (
      <div className="h-48 flex items-center justify-center text-sm text-muted">
        No data in this range yet
      </div>
    );
  }

  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        {hasBand ? (
          <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
            <CartesianGrid stroke="rgba(15,23,42,0.06)" vertical={false} />
            <XAxis dataKey="label" tick={axisStyle} tickLine={false} axisLine={false}
                   minTickGap={48} />
            <YAxis tick={axisStyle} tickLine={false} axisLine={false}
                   tickFormatter={(v: number) => fmtValue(v, decimals)} unit={unit ? ` ${unit}` : ""} />
            <Tooltip content={<ChartTip unit={unit} decimals={decimals} />} />
            <Area type="monotone" dataKey="band" stroke="none"
                  fill="rgba(59,130,246,0.12)" isAnimationActive={false} />
            <Line type="monotone" dataKey="value" stroke="#3B82F6" strokeWidth={2}
                  dot={false} isAnimationActive={false} />
          </ComposedChart>
        ) : (
          <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
            <defs>
              <linearGradient id="val" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3B82F6" stopOpacity={0.22} />
                <stop offset="100%" stopColor="#3B82F6" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(15,23,42,0.06)" vertical={false} />
            <XAxis dataKey="label" tick={axisStyle} tickLine={false} axisLine={false}
                   minTickGap={48} />
            <YAxis tick={axisStyle} tickLine={false} axisLine={false}
                   tickFormatter={(v: number) => fmtValue(v, decimals)} unit={unit ? ` ${unit}` : ""} />
            <Tooltip content={<ChartTip unit={unit} decimals={decimals} />} />
            <Area type="monotone" dataKey="value" stroke="#3B82F6" strokeWidth={2}
                  fill="url(#val)" dot={false} isAnimationActive={false} />
          </AreaChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}

function ChartTip({
  active, payload, unit, decimals,
}: {
  active?: boolean;
  payload?: any[];
  unit?: string;
  decimals?: number;
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="card px-3 py-2 text-xs !shadow-md">
      <div className="text-muted">{p.label}</div>
      <div className="font-semibold text-slate-900 mt-0.5">
        {fmtValue(p.value, decimals)}{unit ? ` ${unit}` : ""}
      </div>
      {p.min !== undefined && p.max !== undefined && (
        <div className="text-muted">
          {fmtValue(p.min, decimals)} – {fmtValue(p.max, decimals)}
        </div>
      )}
    </div>
  );
}
