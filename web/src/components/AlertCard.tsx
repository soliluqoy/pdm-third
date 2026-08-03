// An alert ("something PREDICT noticed") with resolve/dismiss actions
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Info, X } from "lucide-react";
import { api } from "../api";
import { SEVERITY_META, timeAgo } from "../format";
import type { Alert } from "../types";

export default function AlertCard({ alert, compact }: { alert: Alert; compact?: boolean }) {
  const queryClient = useQueryClient();
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["alerts"] });
    queryClient.invalidateQueries({ queryKey: ["overview"] });
    queryClient.invalidateQueries({ queryKey: ["summary"] });
  };
  const resolve = useMutation({ mutationFn: () => api.resolveAlert(alert.id), onSuccess: invalidate });
  const dismiss = useMutation({ mutationFn: () => api.dismissAlert(alert.id), onSuccess: invalidate });
  const sev = SEVERITY_META[alert.severity];
  const Icon = alert.severity === "info" ? Info : AlertTriangle;

  return (
    <div className="card p-4">
      <div className="flex items-start gap-3">
        <div className={`mt-0.5 ${sev.classes} rounded-full p-2`}>
          <Icon size={16} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-slate-800">{alert.title}</span>
            <span className={`chip ${sev.classes}`}>{sev.label}</span>
            {alert.occurrence_count > 1 && (
              <span className="chip bg-ink-800 text-slate-500">×{alert.occurrence_count}</span>
            )}
            {alert.vehicle_name && (
              <span className="chip bg-ink-800 text-muted">{alert.vehicle_name}</span>
            )}
          </div>
          {!compact && (
            <p className="mt-1 text-sm text-slate-500 leading-snug">{alert.message}</p>
          )}
          <div className="mt-1 text-xs text-muted">
            {timeAgo(alert.created_at)}
            {alert.resolved_at && ` · closed ${timeAgo(alert.resolved_at)}`}
          </div>
        </div>
        {alert.status === "active" && (
          <div className="flex gap-1.5 shrink-0">
            <button
              className="btn-ghost !px-2.5 !py-1.5 text-xs"
              title="Mark as fixed"
              disabled={resolve.isPending}
              onClick={() => resolve.mutate()}
            >
              <Check size={14} /> Fixed
            </button>
            <button
              className="btn-ghost !px-2.5 !py-1.5 text-xs"
              title="Dismiss (not an issue)"
              disabled={dismiss.isPending}
              onClick={() => dismiss.mutate()}
            >
              <X size={14} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
