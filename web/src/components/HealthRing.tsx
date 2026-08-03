// Circular health indicator (SVG ring + center dot)
import { HEALTH_META } from "../format";
import type { Health } from "../types";

export default function HealthRing({
  health,
  size = 56,
  stroke = 5,
}: {
  health: Health;
  size?: number;
  stroke?: number;
}) {
  const meta = HEALTH_META[health];
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2} cy={size / 2} r={r}
          fill="none" stroke="rgba(15,23,42,0.08)" strokeWidth={stroke}
        />
        <circle
          cx={size / 2} cy={size / 2} r={r}
          fill="none" stroke={meta.color} strokeWidth={stroke}
          strokeDasharray={c} strokeDashoffset={health === "grey" ? c * 0.25 : 0}
          strokeLinecap="round"
        />
      </svg>
      <div
        className="absolute inset-0 m-auto rounded-full"
        style={{
          width: size * 0.28, height: size * 0.28,
          background: meta.color, boxShadow: `0 0 10px ${meta.color}44`,
        }}
      />
    </div>
  );
}
