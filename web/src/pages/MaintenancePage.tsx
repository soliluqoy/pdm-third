// Maintenance: work-order board (Suggested | Open | In progress) + History + CSV
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check, ClipboardList, Download, Play, Plus, Sparkles, ThumbsDown,
  ThumbsUp, Wrench, X,
} from "lucide-react";
import { useState } from "react";
import { api, qk } from "../api";
import EmptyState from "../components/EmptyState";
import Modal from "../components/Modal";
import { PRIORITY_META, fmtDateTime, fmtValue, timeAgo } from "../format";
import type { Car, WorkOrder } from "../types";

type Tab = "suggested" | "open" | "in_progress" | "history";

export default function MaintenancePage() {
  const [tab, setTab] = useState<Tab>("suggested");
  const [newOpen, setNewOpen] = useState(false);

  const { data: suggested } = useQuery({
    queryKey: qk.workorders("suggested"),
    queryFn: () => api.workorders("suggested"),
  });
  const { data: board } = useQuery({
    queryKey: qk.workorders(tab),
    queryFn: () => api.workorders(tab),
    enabled: tab !== "history",
  });
  const { data: history } = useQuery({
    queryKey: qk.history,
    queryFn: () => api.maintenanceHistory(),
    enabled: tab === "history",
  });

  const tabs: { key: Tab; label: string; count?: number }[] = [
    { key: "suggested", label: "Suggested", count: suggested?.length },
    { key: "open", label: "Open" },
    { key: "in_progress", label: "In progress" },
    { key: "history", label: "History" },
  ];

  const monthSpend = (history ?? [])
    .filter((h) => {
      if (!h.event_date || h.cost == null) return false;
      const d = new Date(h.event_date);
      const now = new Date();
      return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear();
    })
    .reduce((sum, h) => sum + (h.cost ?? 0), 0);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={tab === t.key ? "btn-primary" : "btn-ghost"}
          >
            {t.key === "suggested" && <Sparkles size={15} />}
            {t.label}
            {t.count ? <span className="chip bg-black/20 text-inherit">{t.count}</span> : null}
          </button>
        ))}
        <div className="ml-auto flex gap-2">
          {tab === "history" && (
            <a className="btn-ghost" href={api.maintenanceCsvUrl()} download>
              <Download size={15} /> CSV
            </a>
          )}
          <button className="btn-ghost" onClick={() => setNewOpen(true)}>
            <Plus size={15} /> New work order
          </button>
        </div>
      </div>

      {tab !== "history" && (
        <div className="space-y-2.5">
          {board?.length ? (
            board.map((t) => <WorkOrderCard key={t.id} wo={t} />)
          ) : (
            <EmptyState
              icon={tab === "suggested" ? Sparkles : ClipboardList}
              title={
                tab === "suggested"
                  ? "No suggestions right now"
                  : tab === "open"
                    ? "Nothing open"
                    : "Nothing in progress"
              }
              body={
                tab === "suggested"
                  ? "When PREDICT spots something fixable, it drafts a suggestion here — nothing reaches your list without approval (shadow mode)."
                  : "Approve a suggestion or add a work order manually."
              }
            />
          )}
        </div>
      )}

      {tab === "history" && (
        <div className="space-y-3">
          {monthSpend > 0 && (
            <div className="card px-4 py-3 text-sm text-slate-300">
              This month's maintenance spend:{" "}
              <span className="font-semibold text-slate-100">
                ${monthSpend.toLocaleString(undefined, { maximumFractionDigits: 2 })}
              </span>
            </div>
          )}
          <div className="space-y-1.5">
            {history?.length ? (
              history.map((h) => (
                <div key={h.id} className="card px-4 py-3 flex items-center gap-3">
                  <div className="rounded-lg bg-ok/15 text-ok p-1.5">
                    <Wrench size={14} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-slate-200">{h.title}</div>
                    {h.notes && <div className="text-xs text-muted truncate">{h.notes}</div>}
                  </div>
                  <div className="text-xs text-muted shrink-0 text-right">
                    <div>{h.vehicle_name} · {fmtDateTime(h.event_date)}</div>
                    <div>
                      {h.cost != null && `$${fmtValue(h.cost, 2)}`}
                      {h.cost != null && h.odometer != null && " · "}
                      {h.odometer != null && `${fmtValue(h.odometer, 0)} km`}
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <EmptyState
                icon={Wrench}
                title="No history yet"
                body="Completed work orders become the car's permanent maintenance record."
              />
            )}
          </div>
        </div>
      )}

      <NewWorkOrderModal open={newOpen} onClose={() => setNewOpen(false)} />
    </div>
  );
}

function WorkOrderCard({ wo }: { wo: WorkOrder }) {
  const queryClient = useQueryClient();
  const [completeOpen, setCompleteOpen] = useState(false);
  const [notes, setNotes] = useState("");
  const [cost, setCost] = useState("");
  const [odometer, setOdometer] = useState("");

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["workorders"] });
    queryClient.invalidateQueries({ queryKey: qk.summary });
    queryClient.invalidateQueries({ queryKey: qk.history });
    queryClient.invalidateQueries({ queryKey: ["alerts"] });
    queryClient.invalidateQueries({ queryKey: ["overview"] });
  };
  const approve = useMutation({ mutationFn: () => api.approveWorkOrder(wo.id), onSuccess: invalidate });
  const start = useMutation({ mutationFn: () => api.startWorkOrder(wo.id), onSuccess: invalidate });
  const cancel = useMutation({ mutationFn: () => api.cancelWorkOrder(wo.id), onSuccess: invalidate });
  const complete = useMutation({
    mutationFn: () =>
      api.completeWorkOrder(wo.id, {
        notes: notes || undefined,
        cost: cost ? Number(cost) : undefined,
        odometer: odometer ? Number(odometer) : undefined,
      }),
    onSuccess: () => {
      invalidate();
      setCompleteOpen(false);
      setNotes(""); setCost(""); setOdometer("");
    },
  });

  const prio = PRIORITY_META[wo.priority];

  return (
    <div className="card p-4">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-slate-100">{wo.title}</span>
            <span className={`chip ${prio.classes}`}>{prio.label}</span>
            {wo.source === "auto" && (
              <span className="chip bg-accent/15 text-accent">auto</span>
            )}
            {wo.vehicle_name && (
              <span className="chip bg-ink-800 text-muted">{wo.vehicle_name}</span>
            )}
          </div>
          {wo.description && (
            <p className="mt-1.5 text-sm text-slate-300/90 leading-snug">{wo.description}</p>
          )}
          <div className="mt-1 text-xs text-muted">
            {timeAgo(wo.created_at)}
            {wo.due_date && ` · due ${fmtDateTime(wo.due_date)}`}
            {wo.started_at && ` · started ${timeAgo(wo.started_at)}`}
          </div>
        </div>
        <div className="flex gap-1.5 shrink-0">
          {wo.status === "suggested" && (
            <>
              <button
                className="btn-primary !px-3 !py-1.5 text-xs"
                disabled={approve.isPending}
                onClick={() => approve.mutate()}
              >
                <ThumbsUp size={13} /> Approve
              </button>
              <button
                className="btn-ghost !px-2.5 !py-1.5 text-xs"
                title="Not needed"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                <ThumbsDown size={13} />
              </button>
            </>
          )}
          {wo.status === "open" && (
            <>
              <button
                className="btn-primary !px-3 !py-1.5 text-xs"
                disabled={start.isPending}
                onClick={() => start.mutate()}
              >
                <Play size={13} /> Start
              </button>
              <button
                className="btn-ghost !px-2.5 !py-1.5 text-xs"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                <X size={13} />
              </button>
            </>
          )}
          {wo.status === "in_progress" && (
            <button
              className="btn-primary !px-3 !py-1.5 text-xs"
              onClick={() => setCompleteOpen(true)}
            >
              <Check size={13} /> Complete
            </button>
          )}
        </div>
      </div>

      <Modal open={completeOpen} onClose={() => setCompleteOpen(false)} title="Complete work order">
        <p className="text-sm text-muted mb-3">
          This moves <span className="text-slate-200">{wo.title}</span> into the car's
          permanent maintenance history.
        </p>
        <label className="label">Notes (what was done, parts)</label>
        <textarea
          className="input min-h-[80px]"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="e.g. Replaced battery, 65 Ah"
        />
        <div className="grid grid-cols-2 gap-3 mt-3">
          <div>
            <label className="label">Cost ($, optional)</label>
            <input
              className="input" type="number" min="0" step="0.01"
              value={cost} onChange={(e) => setCost(e.target.value)}
            />
          </div>
          <div>
            <label className="label">Odometer (km, optional)</label>
            <input
              className="input" type="number" min="0" step="1"
              value={odometer} onChange={(e) => setOdometer(e.target.value)}
            />
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost" onClick={() => setCompleteOpen(false)}>Cancel</button>
          <button
            className="btn-primary"
            disabled={complete.isPending}
            onClick={() => complete.mutate()}
          >
            <Check size={15} /> Complete
          </button>
        </div>
      </Modal>
    </div>
  );
}

function NewWorkOrderModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: cars } = useQuery<Car[]>({ queryKey: ["cars"], queryFn: api.cars });
  const [vehicleId, setVehicleId] = useState<number | "">("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api.createWorkOrder({
        vehicle_id: vehicleId,
        title,
        description: description || undefined,
        priority,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workorders"] });
      onClose();
      setTitle(""); setDescription(""); setError(null);
    },
    onError: (e: Error) => setError(e.message),
  });

  return (
    <Modal open={open} onClose={onClose} title="New work order">
      <div className="space-y-3">
        <div>
          <label className="label">Car</label>
          <select
            className="input"
            value={vehicleId}
            onChange={(e) => setVehicleId(Number(e.target.value) || "")}
          >
            <option value="">Choose…</option>
            {cars?.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="label">What needs doing?</label>
          <input
            className="input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Replace wiper blades"
          />
        </div>
        <div>
          <label className="label">Details (optional)</label>
          <textarea
            className="input min-h-[64px]"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div>
          <label className="label">Priority</label>
          <select className="input" value={priority} onChange={(e) => setPriority(e.target.value)}>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="urgent">Urgent</option>
          </select>
        </div>
        {error && <p className="text-sm text-bad">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className="btn-primary"
            disabled={!vehicleId || !title.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            <Plus size={15} /> Add work order
          </button>
        </div>
      </div>
    </Modal>
  );
}
