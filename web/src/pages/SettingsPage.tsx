// Settings: cars (+ add wizard with SMS setup), alerts tuning, preferences
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BellRing, Car as CarIcon, Check, Copy, MessageSquareText, Pencil, Plus, Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api, qk } from "../api";
import Modal from "../components/Modal";
import Toggle from "../components/Toggle";
import { fmtValue } from "../format";
import type { Car, Rule } from "../types";

export default function SettingsPage() {
  const { data: cars } = useQuery({ queryKey: ["cars"], queryFn: api.cars });
  const { data: settings } = useQuery({ queryKey: qk.settings, queryFn: api.settings });
  const { data: rules } = useQuery({ queryKey: qk.rules, queryFn: api.rules });
  const [addOpen, setAddOpen] = useState(false);
  const [smsFor, setSmsFor] = useState<Car | null>(null);

  return (
    <div className="space-y-8">
      {/* ── Cars ─────────────────────────────────────────────────────────── */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-muted uppercase tracking-wide">Your cars</h2>
          <button className="btn-primary !py-1.5 text-xs" onClick={() => setAddOpen(true)}>
            <Plus size={14} /> Add car
          </button>
        </div>
        <div className="space-y-2">
          {cars?.map((c) => (
            <CarRow key={c.id} car={c} onSms={() => setSmsFor(c)} />
          ))}
          {cars?.length === 0 && (
            <div className="card p-6 text-sm text-muted">
              No cars registered. Add one — it takes a minute.
            </div>
          )}
        </div>
      </section>

      {/* ── Alerts ───────────────────────────────────────────────────────── */}
      <section>
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-1">
          <BellRing size={14} className="inline -mt-0.5 mr-1.5" />
          Warnings & limits
        </h2>
        <p className="text-xs text-muted mb-3">
          PREDICT watches these for you. Tune the limits — nothing else to configure.
          Component-health rules fire below a health score; ML anomaly rules fire
          above an anomaly score (opposite polarity).
        </p>
        <div className="space-y-2">
          {rules
            ?.filter(
              (r) =>
                r.rule_type !== "anomaly"
                || r.key.startsWith("predict_")
                || r.key.startsWith("ml_anomaly_"),
            )
            .map((r) => (
              <RuleRow key={r.id} rule={r} />
            ))}
        </div>
      </section>

      {/* ── Preferences ──────────────────────────────────────────────────── */}
      {settings && <Preferences settings={settings} />}

      <AddCarModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onCreated={(car) => {
          setAddOpen(false);
          setSmsFor(car);
        }}
      />
      <SmsModal car={smsFor} onClose={() => setSmsFor(null)} />
    </div>
  );
}

// ── Car row ───────────────────────────────────────────────────────────────────
function CarRow({ car, onSms }: { car: Car; onSms: () => void }) {
  const queryClient = useQueryClient();
  const [editOpen, setEditOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const del = useMutation({
    mutationFn: () => api.deleteCar(car.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cars"] });
      queryClient.invalidateQueries({ queryKey: qk.overview });
    },
  });

  return (
    <div className="card px-4 py-3 flex items-center gap-3">
      <div className="rounded-xl bg-ink-800 p-2 text-muted">
        <CarIcon size={18} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-slate-800">
          {car.name}
          {car.license_plate && <span className="text-muted font-normal"> · {car.license_plate}</span>}
        </div>
        <div className="text-xs text-muted">
          {car.device_type.toUpperCase()} · IMEI {car.imei}
        </div>
      </div>
      <button className="btn-ghost !p-2" title="Tracker SMS setup" onClick={onSms}>
        <MessageSquareText size={15} />
      </button>
      <button className="btn-ghost !p-2" title="Edit" onClick={() => setEditOpen(true)}>
        <Pencil size={15} />
      </button>
      <button
        className="btn-danger !p-2"
        title="Remove car"
        onClick={() => setConfirmDelete(true)}
      >
        <Trash2 size={15} />
      </button>

      <EditCarModal car={car} open={editOpen} onClose={() => setEditOpen(false)} />
      <Modal open={confirmDelete} onClose={() => setConfirmDelete(false)} title="Remove car?">
        <p className="text-sm text-muted">
          This deletes <span className="text-slate-800 font-medium">{car.name}</span> and all its history
          (readings, alerts, work orders, trips). This cannot be undone.
        </p>
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost" onClick={() => setConfirmDelete(false)}>Keep it</button>
          <button
            className="btn-danger"
            disabled={del.isPending}
            onClick={() => del.mutate(undefined, { onSuccess: () => setConfirmDelete(false) })}
          >
            <Trash2 size={15} /> Delete everything
          </button>
        </div>
      </Modal>
    </div>
  );
}

// ── Add / edit car ────────────────────────────────────────────────────────────
function CarForm({
  initial, onSubmit, busy, error, submitLabel,
}: {
  initial?: Record<string, unknown>;
  onSubmit: (values: any) => void;
  busy: boolean;
  error: string | null;
  submitLabel: string;
}) {
  const [v, setV] = useState<any>({
    name: "", imei: "", device_type: "fmc001",
    license_plate: "", sim_phone: "",
    make: "", model: "", year: "", vin: "",
    mass_kg: "", last_oil_change_odo: "", last_brake_service_odo: "",
    ...initial,
  });
  const set = (k: string) => (e: any) => setV({ ...v, [k]: e.target.value });

  return (
    <div className="space-y-3">
      <div>
        <label className="label">Name</label>
        <input className="input" value={v.name} onChange={set("name")} placeholder="My Car" />
      </div>
      {v.imei !== undefined && (
        <div>
          <label className="label">Tracker IMEI (15 digits, on the device label)</label>
          <input
            className="input" value={v.imei} onChange={set("imei")}
            placeholder="3529XXXXXXXXXXX" inputMode="numeric"
          />
        </div>
      )}
      {v.device_type !== undefined && (
        <div>
          <label className="label">Tracker model</label>
          <div className="grid grid-cols-2 gap-2">
            {[
              { id: "fmc001", label: "FMC001", hint: "OBD-II plug-in" },
              { id: "fmc150", label: "FMC150", hint: "wired CAN" },
            ].map((m) => (
              <button
                key={m.id}
                type="button"
                onClick={() => setV({ ...v, device_type: m.id })}
                className={`card p-3 text-left transition-colors ${v.device_type === m.id ? "!border-accent/50 bg-accent-soft" : ""}`}
              >
                <div className="text-sm font-semibold">{m.label}</div>
                <div className="text-xs text-muted">{m.hint}</div>
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="label">License plate (optional)</label>
          <input className="input" value={v.license_plate ?? ""} onChange={set("license_plate")} />
        </div>
        <div>
          <label className="label">Tracker SIM phone (optional, for SMS setup)</label>
          <input className="input" value={v.sim_phone ?? ""} onChange={set("sim_phone")} placeholder="+60…" />
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <div>
          <label className="label">Make</label>
          <input className="input" value={v.make ?? ""} onChange={set("make")} placeholder="Toyota" />
        </div>
        <div>
          <label className="label">Model</label>
          <input className="input" value={v.model ?? ""} onChange={set("model")} placeholder="Hilux" />
        </div>
        <div>
          <label className="label">Year</label>
          <input className="input" value={v.year ?? ""} onChange={set("year")} inputMode="numeric" placeholder="2021" />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="label">Mass (kg, for brake wear)</label>
          <input
            className="input" value={v.mass_kg ?? ""} onChange={set("mass_kg")}
            inputMode="decimal" placeholder="1500"
          />
        </div>
        <div>
          <label className="label">VIN (optional)</label>
          <input className="input" value={v.vin ?? ""} onChange={set("vin")} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="label">Last oil change odo (km)</label>
          <input
            className="input" value={v.last_oil_change_odo ?? ""} onChange={set("last_oil_change_odo")}
            inputMode="decimal" placeholder="45000"
          />
        </div>
        <div>
          <label className="label">Last brake service odo (km)</label>
          <input
            className="input" value={v.last_brake_service_odo ?? ""} onChange={set("last_brake_service_odo")}
            inputMode="decimal" placeholder="40000"
          />
        </div>
      </div>
      {error && <p className="text-sm text-bad">{error}</p>}
      <div className="flex justify-end pt-1">
        <button
          className="btn-primary"
          disabled={busy || !v.name?.trim() || (v.imei !== undefined && v.imei.length < 14)}
          onClick={() => onSubmit(v)}
        >
          <Check size={15} /> {submitLabel}
        </button>
      </div>
    </div>
  );
}

function optNum(raw: unknown): number | undefined {
  if (raw === null || raw === undefined || raw === "") return undefined;
  const n = typeof raw === "number" ? raw : parseFloat(String(raw));
  return Number.isFinite(n) ? n : undefined;
}

function carPayload(v: any, { includeImei }: { includeImei: boolean }) {
  const year = optNum(v.year);
  return {
    name: v.name.trim(),
    ...(includeImei ? { imei: v.imei.trim(), device_type: v.device_type } : {}),
    license_plate: v.license_plate || (includeImei ? undefined : null),
    sim_phone: v.sim_phone || (includeImei ? undefined : null),
    make: v.make || (includeImei ? undefined : null),
    model: v.model || (includeImei ? undefined : null),
    year: year ?? (includeImei ? undefined : null),
    vin: v.vin || (includeImei ? undefined : null),
    mass_kg: optNum(v.mass_kg) ?? (includeImei ? undefined : null),
    last_oil_change_odo: optNum(v.last_oil_change_odo) ?? (includeImei ? undefined : null),
    last_brake_service_odo: optNum(v.last_brake_service_odo) ?? (includeImei ? undefined : null),
  };
}

function AddCarModal({
  open, onClose, onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (car: Car) => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: (v: any) => api.registerCar(carPayload(v, { includeImei: true })),
    onSuccess: (car) => {
      queryClient.invalidateQueries({ queryKey: ["cars"] });
      queryClient.invalidateQueries({ queryKey: qk.overview });
      setError(null);
      onCreated(car);
    },
    onError: (e: Error) => setError(e.message),
  });

  return (
    <Modal open={open} onClose={onClose} title="Add your car">
      <CarForm
        busy={create.isPending}
        error={error}
        submitLabel="Register car"
        onSubmit={(v) => create.mutate(v)}
      />
      <p className="text-xs text-muted mt-3">
        After registering, you'll get a text-message template to point the tracker at this server.
      </p>
    </Modal>
  );
}

function EditCarModal({ car, open, onClose }: { car: Car; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const update = useMutation({
    mutationFn: (v: any) => api.updateCar(car.id, carPayload(v, { includeImei: false })),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cars"] });
      queryClient.invalidateQueries({ queryKey: qk.overview });
      setError(null);
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  return (
    <Modal open={open} onClose={onClose} title={`Edit ${car.name}`}>
      <CarForm
        initial={{
          name: car.name,
          license_plate: car.license_plate ?? "",
          sim_phone: car.sim_phone ?? "",
          make: car.make ?? "",
          model: car.model ?? "",
          year: car.year?.toString() ?? "",
          vin: car.vin ?? "",
          mass_kg: car.mass_kg?.toString() ?? "",
          last_oil_change_odo: car.last_oil_change_odo?.toString() ?? "",
          last_brake_service_odo: car.last_brake_service_odo?.toString() ?? "",
          imei: undefined,
          device_type: undefined,
        }}
        busy={update.isPending}
        error={error}
        submitLabel="Save"
        onSubmit={(v) => update.mutate(v)}
      />
    </Modal>
  );
}

// ── SMS setup helper ──────────────────────────────────────────────────────────
function smsBody(car: Car, host: string, port: number): string {
  if (car.device_type === "fmc001") {
    // Duplicate (second server) — keeps the primary platform working.
    return `  setparam 2010:2;2007:${host};2008:${port};2009:0`;
  }
  return `  setparam 2001:;2002:;2003:;2004:${host};2005:${port};2006:0`;
}

function SmsModal({ car, onClose }: { car: Car | null; onClose: () => void }) {
  const { data: settings } = useQuery({ queryKey: qk.settings, queryFn: api.settings });
  const [copied, setCopied] = useState(false);
  if (!car) return null;

  const host = settings?.tracker_public_host || "<YOUR_SERVER_IP>";
  const port = settings?.teltonika_port ?? 5123;
  const body = smsBody(car, host, port);

  return (
    <Modal open onClose={onClose} title={`Set up ${car.device_type.toUpperCase()} tracking`}>
      <ol className="text-sm text-slate-600 space-y-2 list-decimal list-inside mb-4">
        <li>Copy the text below.</li>
        <li>
          Send it as an SMS to the tracker's SIM
          {car.sim_phone ? <> (<span className="text-slate-800 font-medium">{car.sim_phone}</span>)</> : ""}.
        </li>
        <li>The tracker connects within a minute — the car turns green on Home.</li>
      </ol>
      <div className="relative">
        <pre className="card !bg-ink-850 p-3 text-xs overflow-x-auto whitespace-pre-wrap">{body}</pre>
        <button
          className="btn-ghost !p-2 absolute top-2 right-2"
          onClick={() => {
            navigator.clipboard.writeText(body);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
        </button>
      </div>
      <p className="text-xs text-muted mt-3">
        {car.device_type === "fmc001"
          ? "This uses the tracker's second server (Duplicate) — your existing platform keeps working."
          : "This points the tracker's main server at PREDICT."}
        {" "}Set <code className="text-slate-700 font-medium">TRACKER_PUBLIC_HOST</code> in .env to your server's
        public IP so this message is ready to send. Two leading spaces are required if the device
        has no SMS login.
      </p>
    </Modal>
  );
}

// ── Rule row (toggle + threshold) ─────────────────────────────────────────────
function RuleRow({ rule }: { rule: Rule }) {
  const queryClient = useQueryClient();
  const [threshold, setThreshold] = useState(rule.threshold_value?.toString() ?? "");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setThreshold(rule.threshold_value?.toString() ?? "");
  }, [rule.threshold_value]);

  const patch = useMutation({
    mutationFn: (body: any) => api.patchRule(rule.id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: qk.rules });
      setSaved(true);
      setTimeout(() => setSaved(false), 1200);
    },
  });

  const isScheduled = rule.rule_type === "scheduled";
  const isPredict = rule.key.startsWith("predict_");
  const unit = isScheduled
    ? "km"
    : rule.key === "unauthorized_movement"
      ? "km/h"
      : rule.rule_type === "behavior"
        ? "events/day"
        : isPredict
          ? "score"
          : "";
  const editableNumber = !isPredict && (rule.threshold_value !== null || isScheduled);

  return (
    <div className="card px-4 py-3 flex items-center gap-3">
      <Toggle
        checked={rule.is_active}
        onChange={(v) => patch.mutate({ is_active: v })}
        disabled={patch.isPending}
      />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-700">{rule.name}</div>
        <div className="text-xs text-muted truncate">
          {rule.description}
          {rule.severity === "critical" && <span className="text-bad"> · urgent</span>}
        </div>
      </div>
      {editableNumber && (
        <div className="flex items-center gap-1.5 shrink-0">
          {rule.operator && <span className="text-xs text-muted">{rule.operator}</span>}
          <input
            className="input !w-24 !py-1.5 text-right tabular-nums"
            value={isScheduled ? fmtValue(rule.interval_value ?? 0, 0) : threshold}
            disabled={isScheduled}
            inputMode="decimal"
            onChange={(e) => setThreshold(e.target.value)}
            onBlur={() => {
              const n = parseFloat(threshold);
              if (!Number.isNaN(n) && n !== rule.threshold_value) {
                patch.mutate({ threshold_value: n });
              }
            }}
          />
          {unit && <span className="text-xs text-muted w-8">{unit}</span>}
          {saved && <Check size={14} className="text-ok" />}
        </div>
      )}
      {isPredict && rule.threshold_value != null && (
        <div className="text-xs text-muted shrink-0 tabular-nums">
          fires &lt; {rule.threshold_value} health
        </div>
      )}
      {rule.key.startsWith("ml_anomaly_") && rule.threshold_value != null && (
        <div className="text-xs text-muted shrink-0 tabular-nums">
          fires ≥ {rule.threshold_value} anomaly
        </div>
      )}
    </div>
  );
}

// ── Preferences ───────────────────────────────────────────────────────────────
function Preferences({ settings }: { settings: { values: Record<string, string>; descriptions: Record<string, string> } }) {
  const queryClient = useQueryClient();
  const patch = useMutation({
    mutationFn: (values: Record<string, string>) => api.patchSettings(values),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: qk.settings }),
  });

  const askFirst = settings.values["ask_me_first"] !== "false";

  const num = (key: string, fallback: string) => settings.values[key] ?? fallback;
  const [speedLimit, setSpeedLimit] = useState(num("behavior.speed_limit_kmh", "120"));
  const [idleMin, setIdleMin] = useState(num("behavior.idle_minutes", "5"));
  const [highRpm, setHighRpm] = useState(num("behavior.high_rpm_threshold", "4000"));

  const saveNum = (key: string, raw: string) => {
    const n = parseFloat(raw);
    if (!Number.isNaN(n)) patch.mutate({ [key]: String(n) });
  };

  return (
    <section>
      <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-3">Preferences</h2>
      <div className="space-y-2">
        <div className="card px-4 py-3.5 flex items-center gap-3">
          <Toggle
            checked={askFirst}
            onChange={(v) => patch.mutate({ ask_me_first: v ? "true" : "false" })}
            disabled={patch.isPending}
          />
          <div className="flex-1">
            <div className="text-sm text-slate-700">Ask me first</div>
            <div className="text-xs text-muted">
              When PREDICT spots something fixable, it suggests it in Repairs instead of adding it
              straight to your to-do list.
            </div>
          </div>
        </div>

        {[
          { label: "Speeding threshold", unit: "km/h", value: speedLimit, set: setSpeedLimit, key: "behavior.speed_limit_kmh" },
          { label: "Idling counts after", unit: "min", value: idleMin, set: setIdleMin, key: "behavior.idle_minutes" },
          { label: "High-RPM threshold", unit: "RPM", value: highRpm, set: setHighRpm, key: "behavior.high_rpm_threshold" },
        ].map((f) => (
          <div key={f.key} className="card px-4 py-3 flex items-center gap-3">
            <div className="flex-1 text-sm text-slate-700">{f.label}</div>
            <input
              className="input !w-24 !py-1.5 text-right tabular-nums"
              value={f.value}
              inputMode="numeric"
              onChange={(e) => f.set(e.target.value)}
              onBlur={() => saveNum(f.key, f.value)}
            />
            <span className="text-xs text-muted w-10">{f.unit}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
