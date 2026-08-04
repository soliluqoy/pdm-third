// Predictive: ML model status, per-vehicle anomaly scores, and evaluation
import { useQuery } from "@tanstack/react-query";
import { BrainCircuit, CheckCircle2, Clock, FlaskConical, Loader2, XCircle } from "lucide-react";
import { api } from "../api";
import EmptyState from "../components/EmptyState";
import type { Car, ModelEvaluation, ModelStatus } from "../types";

const COMPONENT_LABELS: Record<string, string> = {
  battery: "Battery",
  cooling: "Cooling",
  oil: "Oil system",
  engine: "Engine",
};

function scoreTint(score: number | null | undefined): string {
  if (score === null || score === undefined) return "text-muted";
  if (score >= 80) return "text-bad";
  if (score >= 60) return "text-warn";
  return "text-ok";
}

export default function PredictivePage() {
  const { data: status } = useQuery({
    queryKey: ["model-status"],
    queryFn: api.modelStatus,
    refetchInterval: 60_000,
  });
  const { data: evaluation } = useQuery({
    queryKey: ["model-evaluation"],
    queryFn: api.modelEvaluation,
    refetchInterval: 60_000,
  });
  const { data: cars } = useQuery({ queryKey: ["cars"], queryFn: api.cars });

  const trainedCount = Object.values(status?.models ?? {}).filter(
    (m) => m.status === "trained",
  ).length;
  const totalModels = Object.keys(status?.models ?? {}).length;

  return (
    <div className="space-y-6">
      {/* ── Header stat cards ─────────────────────────────────────────── */}
      <section className="grid gap-4 sm:grid-cols-3">
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
            <BrainCircuit size={16} className="text-accent" />
            Models trained
          </div>
          <div className="mt-1 text-2xl font-bold text-slate-900 tabular-nums">
            {trainedCount}<span className="text-muted text-base font-normal"> / {totalModels}</span>
          </div>
          <div className="text-xs text-muted mt-0.5">
            {trainedCount === totalModels && totalModels > 0
              ? "All component models ready"
              : "Training once enough fleet data accumulates"}
          </div>
        </div>
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
            <FlaskConical size={16} className="text-brand" />
            Evaluation
          </div>
          <div className="mt-1 text-sm text-slate-600">
            {evaluation?.status === "no_failures_yet" ? (
              <span className="text-muted">Waiting for failure labels</span>
            ) : (
              <span className="text-ok">Precision/recall computed</span>
            )}
          </div>
          <div className="text-xs text-muted mt-0.5">
            Mark a work order "Component failed" to teach the models
          </div>
        </div>
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
            <Clock size={16} className="text-warn" />
            How it works
          </div>
          <div className="mt-1 text-xs text-muted leading-relaxed">
            Anomaly scores (higher = worse) vs the fleet norm — not physical
            health. Values above the threshold fire a suggested work order
            (shadow mode). Car-page health scores use the opposite polarity.
          </div>
        </div>
      </section>

      {/* ── Model status ──────────────────────────────────────────────── */}
      <section>
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-3">
          Component models
        </h2>
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(status?.models ?? {}).map(([component, m]) => (
            <div key={component} className="card p-4">
              <div className="flex items-center gap-2">
                {m.status === "trained" ? (
                  <CheckCircle2 size={16} className="text-ok" />
                ) : (
                  <Loader2 size={16} className="text-muted animate-spin" />
                )}
                <span className="font-medium text-slate-800">
                  {COMPONENT_LABELS[component] ?? component}
                </span>
                <span
                  className={`chip ml-auto ${
                    m.status === "trained" ? "bg-ok/10 text-ok" : "bg-ink-800 text-muted"
                  }`}
                >
                  {m.status === "trained" ? `v${m.version}` : "collecting data"}
                </span>
              </div>
              <div className="mt-2 text-xs text-muted">
                {m.status === "trained" ? (
                  <>
                    Trained {m.trained_at ? new Date(m.trained_at).toLocaleString() : ""}
                    {" · "}fires above {m.threshold} anomaly
                  </>
                ) : (
                  <>
                    Needs ≥ {m.min_train_rows} fleet-days of data before training
                  </>
                )}
              </div>
              <div className="mt-2 flex flex-wrap gap-1">
                {m.features.map((f) => (
                  <span key={f} className="chip bg-ink-800 text-muted !text-[10px]">
                    {f.replace(/_/g, " ")}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ── Per-vehicle anomaly scores ────────────────────────────────── */}
      <section>
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-1">
          Fleet anomaly scores
        </h2>
        <p className="text-xs text-muted mb-3">
          Higher = more anomalous (0–100). Not the same as Car-page component health.
        </p>
        {cars?.length ? (
          <div className="space-y-2">
            {cars.map((car) => (
              <CarScoreRow key={car.id} car={car} />
            ))}
          </div>
        ) : (
          <EmptyState
            icon={BrainCircuit}
            title="No cars yet"
            body="Add a car and let telemetry accumulate — anomaly scores appear once models are trained."
          />
        )}
      </section>

      {/* ── Evaluation ────────────────────────────────────────────────── */}
      <section>
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-3">
          Model evaluation
        </h2>
        {evaluation?.status === "no_failures_yet" ? (
          <div className="card p-5 text-sm text-muted">
            No failure labels yet. When you complete a work order, choose{" "}
            <span className="text-slate-800 font-medium">"Component failed"</span> — that
            teaches the models what failure actually looks like, and precision/recall
            appear here.
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {Object.entries(evaluation?.results ?? {}).map(([component, r]) => (
              <div key={component} className="card p-4">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-800">
                    {COMPONENT_LABELS[component] ?? component}
                  </span>
                  {r.status === "evaluated" ? (
                    <span className="chip bg-ok/10 text-ok ml-auto">evaluated</span>
                  ) : (
                    <span className="chip bg-ink-800 text-muted ml-auto">not trained</span>
                  )}
                </div>
                {r.status === "evaluated" ? (
                  <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                    <div className="text-muted">
                      Precision:{" "}
                      <span className="font-semibold text-slate-800">
                        {((r.precision ?? 0) * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div className="text-muted">
                      Recall:{" "}
                      <span className="font-semibold text-slate-800">
                        {((r.recall ?? 0) * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div className="text-muted">
                      True pos: <span className="tabular-nums">{r.true_positives}</span>
                    </div>
                    <div className="text-muted">
                      False pos: <span className="tabular-nums">{r.false_positives}</span>
                    </div>
                  </div>
                ) : (
                  <div className="mt-2 text-xs text-muted">No trained model to evaluate.</div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function CarScoreRow({ car }: { car: Car }) {
  const { data } = useQuery({
    queryKey: ["model-scores", car.id],
    queryFn: () => api.vehicleModelScores(car.id),
    refetchInterval: 60_000,
  });
  const scores = data?.scores ?? {};

  return (
    <div className="card px-4 py-3 flex items-center gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-slate-800">{car.name}</div>
        <div className="text-xs text-muted">
          {Object.keys(scores).length
            ? "Latest daily features vs fleet norm (anomaly)"
            : "No anomaly score yet — needs a trained model + telemetry"}
        </div>
      </div>
      <div className="flex gap-2 shrink-0">
        {Object.entries(scores).map(([component, score]) => (
          <div
            key={component}
            className="text-right"
            title={`${COMPONENT_LABELS[component] ?? component}: anomaly ${Math.round(score)} (higher = worse)`}
          >
            <div className={`text-sm font-semibold tabular-nums ${scoreTint(score)}`}>
              {Math.round(score)}
            </div>
            <div className="text-[10px] text-muted uppercase tracking-wide">
              {COMPONENT_LABELS[component] ?? component}
            </div>
          </div>
        ))}
        {Object.keys(scores).length === 0 && (
          <XCircle size={18} className="text-muted" />
        )}
      </div>
    </div>
  );
}