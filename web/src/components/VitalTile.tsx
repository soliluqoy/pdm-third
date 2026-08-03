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
        "card text-left p-3 w-full",
        onClick && "card-hover cursor-pointer",
        status === "warn" && "border-warn/40",
        status === "bad" && "border-bad/40",
      )}
    >
      <div className="text-[11px] text-muted truncate">{name}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span
          className={clsx(
            "text-xl font-semibold tabular-nums",
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
