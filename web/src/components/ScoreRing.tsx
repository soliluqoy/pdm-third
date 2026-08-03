// Numeric 0–100 component health ring (battery / brakes / oil)

function scoreColor(score: number | null | undefined): string {
  if (score === null || score === undefined) return "#94A3B8";
  if (score < 25) return "#DC2626";
  if (score < 40) return "#D97706";
  if (score < 70) return "#CA8A04";
  return "#16A34A";
}

export default function ScoreRing({
  label,
  score,
  detail,
  size = 56,
  stroke = 5,
}: {
  label: string;
  score: number | null | undefined;
  detail?: string | null;
  size?: number;
  stroke?: number;
}) {
  const color = scoreColor(score);
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const pct = score === null || score === undefined ? 0 : Math.max(0, Math.min(100, score)) / 100;
  const offset = c * (1 - pct);

  return (
    <div className="flex flex-col items-center gap-1 min-w-0">
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} className="-rotate-90">
          <circle
            cx={size / 2} cy={size / 2} r={r}
            fill="none" stroke="rgba(15,23,42,0.08)" strokeWidth={stroke}
          />
          <circle
            cx={size / 2} cy={size / 2} r={r}
            fill="none" stroke={color} strokeWidth={stroke}
            strokeDasharray={c} strokeDashoffset={offset}
            strokeLinecap="round"
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="text-xs font-bold tabular-nums" style={{ color }}>
            {score === null || score === undefined ? "—" : Math.round(score)}
          </span>
        </div>
      </div>
      <div className="text-xs font-medium text-slate-800 truncate max-w-full">{label}</div>
      {detail && (
        <div className="text-[10px] text-muted text-center leading-tight line-clamp-2 max-w-[7rem]">
          {detail}
        </div>
      )}
    </div>
  );
}
