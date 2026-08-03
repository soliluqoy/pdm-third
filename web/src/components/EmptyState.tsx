// Friendly empty state with optional CTA
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

export default function EmptyState({
  icon: Icon,
  title,
  body,
  action,
}: {
  icon: LucideIcon;
  title: string;
  body?: string;
  action?: ReactNode;
}) {
  return (
    <div className="card p-10 flex flex-col items-center text-center gap-3">
      <div className="rounded-2xl bg-ink-850 p-4 text-muted">
        <Icon size={28} />
      </div>
      <div className="font-medium text-slate-800">{title}</div>
      {body && <p className="text-sm text-muted max-w-sm">{body}</p>}
      {action}
    </div>
  );
}
