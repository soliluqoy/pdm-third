// One live sensor tile
import { memo } from "react";
import clsx from "clsx";
import { fmtValue } from "../format";

function VitalTile({
  name,
  value,
  unit,
  decimals = 0,
  status = "ok",
  onClick,
}: {
  name: string;
  value: number | null | undefined;
  unit?: string;
  decimals?: number;
  status?: "ok" | "warn" | "bad";
  onClick?: () => void;
}) {
  return (
    <div
      onClick={onClick}
      role={onClick ? "button" : undefined}
      className={clsx(
        "rounded-xl border border-line bg-ink-850 text-left p-3 w-full",
        onClick && "transition-all hover:bg-ink-800 hover:shadow-sm cursor-pointer",
        status === "warn" && "!border-warn/40 bg-warn/5",
        status === "bad" && "!border-bad/40 bg-bad/5",
      )}
    >
      <div className="text-[11px] text-muted truncate">{name}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span
          className={clsx(
            "text-xl font-semibold tabular-nums text-slate-800",
            status === "warn" && "text-warn",
            status === "bad" && "text-bad",
          )}
        >
          {fmtValue(value, decimals)}
        </span>
        {unit && <span className="text-[11px] text-muted">{unit}</span>}
      </div>
    </div>
  );
}

export default memo(VitalTile);
