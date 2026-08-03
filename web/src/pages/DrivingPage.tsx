// Driving: score per car, calendar by day, trips, and notable events
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronLeft, ChevronRight, Gauge } from "lucide-react";
import { useMemo, useState } from "react";
import { api, qk } from "../api";
import EmptyState from "../components/EmptyState";
import {
  EVENT_LABELS, EVENT_TRIGGERS, EVENT_TYPE_ORDER,
  eventComparableValue, eventTriggerDetail, eventValueLabel,
  fmtDateTime, fmtDuration, fmtValue, scoreColor,
} from "../format";
import type { DrivingCalendarDay, DrivingEvent, DrivingSummary } from "../types";

type EventGroup = {
  type: string;
  items: DrivingEvent[];
  device: number;
  derived: number;
  peak: DrivingEvent | null;
  latest: DrivingEvent;
};

function todayISO(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function groupEvents(events: DrivingEvent[]): EventGroup[] {
  const map = new Map<string, DrivingEvent[]>();
  for (const e of events) {
    const list = map.get(e.event_type) ?? [];
    list.push(e);
    map.set(e.event_type, list);
  }
  const rank = (t: string) => {
    const i = EVENT_TYPE_ORDER.indexOf(t);
    return i === -1 ? 99 : i;
  };
  return [...map.entries()]
    .map(([type, items]) => {
      const sorted = [...items].sort(
        (a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime(),
      );
      let peak: DrivingEvent | null = null;
      let peakN = -Infinity;
      for (const e of sorted) {
        const n = eventComparableValue(e.event_type, e.value, e.source);
        if (n !== null && n > peakN) {
          peakN = n;
          peak = e;
        }
      }
      return {
        type,
        items: sorted,
        device: sorted.filter((e) => e.source === "device").length,
        derived: sorted.filter((e) => e.source === "derived").length,
        peak,
        latest: sorted[0],
      };
    })
    .sort((a, b) => rank(a.type) - rank(b.type) || b.items.length - a.items.length);
}

function fmtDayLabel(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
  });
}

export default function DrivingPage() {
  const now = new Date();
  const { data: summary } = useQuery({
    queryKey: qk.driving,
    queryFn: api.drivingSummary,
    refetchInterval: 60_000,
  });
  const [selected, setSelected] = useState<number | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [monthCursor, setMonthCursor] = useState({
    year: now.getFullYear(),
    month: now.getMonth() + 1,
  });
  const [day, setDay] = useState<string | null>(todayISO());

  const active = selected ?? summary?.[0]?.vehicle_id ?? null;

  const { data: calendar } = useQuery({
    queryKey: qk.drivingCalendar(active!, monthCursor.year, monthCursor.month),
    queryFn: () => api.drivingCalendar(active!, monthCursor.year, monthCursor.month),
    enabled: active !== null,
  });

  const { data: trips } = useQuery({
    queryKey: qk.trips(active!, day),
    queryFn: () => api.trips(active!, day),
    enabled: active !== null,
  });

  const { data: events } = useQuery({
    queryKey: qk.drivingEvents(active!, day),
    queryFn: () => api.drivingEvents(active!, day),
    enabled: active !== null,
  });

  const groups = useMemo(
    () => (events?.length ? groupEvents(events) : []),
    [events],
  );

  const dayMeta = useMemo(() => {
    if (!day || !calendar) return null;
    return calendar.days.find((d) => d.date === day) ?? null;
  }, [calendar, day]);

  if (summary && summary.length === 0) {
    return (
      <EmptyState
        icon={Gauge}
        title="Nothing to score yet"
        body="Driving scores, trips, and events appear after your first drive."
      />
    );
  }

  return (
    <div className="space-y-5">
      {/* Scorecards */}
      <div className="grid gap-4 md:grid-cols-2">
        {summary?.map((s) => (
          <ScoreCard
            key={s.vehicle_id}
            s={s}
            active={active === s.vehicle_id}
            onClick={() => setSelected(s.vehicle_id)}
          />
        ))}
      </div>

      {active !== null && (
        <DayCalendar
          year={monthCursor.year}
          month={monthCursor.month}
          days={calendar?.days ?? []}
          selected={day}
          onSelect={(d) => setDay(d)}
          onClear={() => setDay(null)}
          onPrev={() => setMonthCursor((c) => {
            if (c.month === 1) return { year: c.year - 1, month: 12 };
            return { year: c.year, month: c.month - 1 };
          })}
          onNext={() => setMonthCursor((c) => {
            if (c.month === 12) return { year: c.year + 1, month: 1 };
            return { year: c.year, month: c.month + 1 };
          })}
          onToday={() => {
            const t = new Date();
            setMonthCursor({ year: t.getFullYear(), month: t.getMonth() + 1 });
            setDay(todayISO());
          }}
        />
      )}

      {/* Trips */}
      {active !== null && (
        <section>
          <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-2.5">
            {day ? `Trips · ${fmtDayLabel(day)}` : "Recent trips"}
            {dayMeta && dayMeta.trips > 0 && (
              <span className="ml-2 normal-case tracking-normal font-normal">
                {dayMeta.trips} trip{dayMeta.trips > 1 ? "s" : ""}
                {dayMeta.distance_km ? ` · ${fmtValue(dayMeta.distance_km, 1)} km` : ""}
              </span>
            )}
          </h2>
          <div className="space-y-1.5">
            {trips?.length ? (
              trips.map((t) => (
                 <div key={t.id} className="card px-4 py-3 flex items-center gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-slate-700">
                      {fmtDateTime(t.start_ts)}
                      {t.is_open && <span className="chip bg-ok/10 text-ok ml-2">Underway</span>}
                    </div>
                    <div className="text-xs text-muted mt-0.5 flex flex-wrap gap-x-3">
                      <span>{t.distance_km !== null ? `${fmtValue(t.distance_km, 1)} km` : "—"}</span>
                      <span>{fmtDuration(t.duration_seconds)}</span>
                      {t.fuel_used !== null && <span>{fmtValue(t.fuel_used, 1)} L fuel</span>}
                      {t.idle_seconds ? <span>idled {fmtDuration(t.idle_seconds)}</span> : null}
                    </div>
                  </div>
                  <div className="text-right text-xs text-muted shrink-0">
                    <div>max {fmtValue(t.max_speed)} km/h</div>
                    {t.events > 0 && (
                      <div className="text-warn mt-0.5">
                        {t.events} event{t.events > 1 ? "s" : ""}
                      </div>
                    )}
                  </div>
                </div>
              ))
            ) : (
              <div className="card p-6 text-sm text-muted">
                {day ? "No trips on this day." : "No trips recorded yet."}
              </div>
            )}
          </div>
        </section>
      )}

      {/* Grouped by flag type */}
      {active !== null && (
        <section>
          <div className="flex items-baseline justify-between gap-3 mb-2.5">
            <h2 className="text-sm font-semibold text-muted uppercase tracking-wide">
              {day ? `Flags · ${fmtDayLabel(day)}` : "Notable moments (7 days)"}
            </h2>
            <p className="text-[11px] text-muted shrink-0">
              {dayMeta ? `${dayMeta.events} event${dayMeta.events === 1 ? "" : "s"} this day` : "Grouped by flag · expand for instances"}
            </p>
          </div>
          {groups.length > 0 ? (
            <div className="card divide-y divide-line overflow-hidden">
              {groups.map((g) => {
                const label = EVENT_LABELS[g.type] ?? g.type;
                const isOpen = !!open[g.type];
                const peakLabel = g.peak
                  ? eventValueLabel(g.peak.event_type, g.peak.value, g.peak.source)
                  : "";
                const sourceBits = [
                  g.device ? `${g.device} Dev` : null,
                  g.derived ? `${g.derived} Est` : null,
                ].filter(Boolean).join(" · ");
                return (
                  <div key={g.type}>
                    <button
                      type="button"
                      onClick={() => setOpen((o) => ({ ...o, [g.type]: !o[g.type] }))}
                      className="w-full px-3.5 py-2.5 flex items-center gap-3 text-left hover:bg-ink-850"
                      title={EVENT_TRIGGERS[g.type]}
                    >
                      <ChevronDown
                        className={`w-3.5 h-3.5 text-muted shrink-0 transition-transform ${
                          isOpen ? "" : "-rotate-90"
                        }`}
                      />
                      <div className="flex-1 min-w-0 flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                        <span className="text-sm text-slate-800 font-medium">{label}</span>
                        <span className="text-xs text-warn font-medium">×{g.items.length}</span>
                        {peakLabel && (
                          <span className="text-xs text-muted">peak {peakLabel}</span>
                        )}
                        <span className="text-[11px] text-muted">
                          latest {fmtDateTime(g.latest.ts)}
                        </span>
                        {sourceBits && (
                          <span className="text-[11px] text-muted">{sourceBits}</span>
                        )}
                      </div>
                    </button>
                    {isOpen && (
                      <div className="bg-ink-850/60 border-t border-line/60">
                        <p className="px-3.5 pt-2 pb-1 text-[11px] text-muted">
                          {EVENT_TRIGGERS[g.type]}
                        </p>
                        <div className="overflow-x-auto max-h-56 overflow-y-auto">
                          <table className="w-full text-left text-xs">
                            <thead className="sticky top-0 bg-ink-900/95 backdrop-blur-sm">
                              <tr className="text-[11px] uppercase tracking-wide text-muted">
                                <th className="px-3.5 py-1.5 font-medium">When</th>
                                <th className="px-3 py-1.5 font-medium">Measured</th>
                                <th className="px-3 py-1.5 font-medium">Src</th>
                                <th className="px-3 py-1.5 font-medium hidden sm:table-cell">Trip</th>
                                <th className="px-3 py-1.5 font-medium hidden md:table-cell">GPS</th>
                              </tr>
                            </thead>
                            <tbody>
                              {g.items.slice(0, 50).map((e) => (
                                <EventRow key={e.id} e={e} />
                              ))}
                            </tbody>
                          </table>
                          {g.items.length > 50 && (
                            <p className="px-3.5 py-1.5 text-[11px] text-muted">
                              Showing 50 of {g.items.length}
                            </p>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="card p-6 text-sm text-muted">
              {day ? "No flags on this day." : "No notable moments yet."}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function DayCalendar({
  year, month, days, selected, onSelect, onClear, onPrev, onNext, onToday,
}: {
  year: number;
  month: number;
  days: DrivingCalendarDay[];
  selected: string | null;
  onSelect: (day: string) => void;
  onClear: () => void;
  onPrev: () => void;
  onNext: () => void;
  onToday: () => void;
}) {
  const byDate = useMemo(() => {
    const m = new Map<string, DrivingCalendarDay>();
    for (const d of days) m.set(d.date, d);
    return m;
  }, [days]);

  const daysInMonth = new Date(year, month, 0).getDate();
  const label = new Date(year, month - 1, 1).toLocaleDateString(undefined, {
    month: "short", year: "numeric",
  });
  const today = todayISO();

  return (
    <section className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <div className="flex items-center gap-0.5 shrink-0">
        <button type="button" onClick={onPrev}
                className="p-1 rounded-md text-muted hover:bg-ink-800 hover:text-slate-700"
                aria-label="Previous month">
          <ChevronLeft className="w-3.5 h-3.5" />
        </button>
        <span className="text-xs font-medium text-slate-600 w-[4.75rem] text-center tabular-nums">
          {label}
        </span>
        <button type="button" onClick={onNext}
                className="p-1 rounded-md text-muted hover:bg-ink-800 hover:text-slate-700"
                aria-label="Next month">
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="flex-1 min-w-0 overflow-x-auto">
        <div className="flex gap-0.5 w-max">
          {Array.from({ length: daysInMonth }, (_, i) => i + 1).map((n) => {
            const iso = `${year}-${String(month).padStart(2, "0")}-${String(n).padStart(2, "0")}`;
            const meta = byDate.get(iso);
            const has = !!meta && (meta.trips > 0 || meta.events > 0);
            const isSel = selected === iso;
            const isToday = iso === today;
            return (
              <button
                key={iso}
                type="button"
                onClick={() => onSelect(iso)}
                title={
                  has
                    ? `${iso}: ${meta!.trips} trips · ${meta!.events} events`
                    : iso
                }
                className={`relative w-7 h-7 shrink-0 rounded-md text-[11px] tabular-nums transition-colors
                  ${isSel ? "bg-brand/15 text-brand font-semibold" : "text-slate-500 hover:bg-ink-800"}
                  ${isToday && !isSel ? "outline outline-1 outline-line" : ""}
                  ${!has && !isSel ? "text-muted/70" : ""}
                  ${has && !isSel ? "text-slate-700" : ""}`}
              >
                {n}
                {has && (
                  <span className={`absolute bottom-0.5 left-1/2 -translate-x-1/2 w-1 h-1 rounded-full
                    ${isSel ? "bg-brand" : "bg-warn"}`} />
                )}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex items-center gap-2 shrink-0 text-[11px]">
        <button type="button" onClick={onToday} className="text-brand hover:underline">
          Today
        </button>
        {selected ? (
          <button type="button" onClick={onClear} className="text-muted hover:text-slate-700">
            All
          </button>
        ) : null}
        {selected && (
          <span className="text-muted hidden sm:inline">{fmtDayLabel(selected)}</span>
        )}
      </div>
    </section>
  );
}

function EventRow({ e }: { e: DrivingEvent }) {
  const measured = eventValueLabel(e.event_type, e.value, e.source);
  const detail = eventTriggerDetail(e.event_type, e.value, e.source);
  const hasGps = e.latitude != null && e.longitude != null;
  return (
    <tr
      className="border-t border-line/40 hover:bg-ink-850/60"
      title={detail}
    >
      <td className="px-3.5 py-1 text-muted whitespace-nowrap">{fmtDateTime(e.ts)}</td>
      <td className="px-3 py-1 text-warn whitespace-nowrap">{measured || "—"}</td>
      <td className="px-3 py-1 whitespace-nowrap">
        <span className={e.source === "device" ? "text-brand" : "text-muted"}>
          {e.source === "device" ? "Dev" : "Est"}
        </span>
      </td>
      <td className="px-3 py-1 text-muted whitespace-nowrap hidden sm:table-cell">
        {e.trip_id != null ? `#${e.trip_id}` : "—"}
      </td>
      <td className="px-3 py-1 text-muted whitespace-nowrap hidden md:table-cell font-mono text-[11px]">
        {hasGps
          ? `${e.latitude!.toFixed(4)}, ${e.longitude!.toFixed(4)}`
          : "—"}
      </td>
    </tr>
  );
}

function ScoreCard({
  s, active, onClick,
}: {
  s: DrivingSummary;
  active: boolean;
  onClick: () => void;
}) {
  const score = s.today_score ?? s.avg_score_7d;
  const color = scoreColor(score);
  const maxScore = Math.max(...s.trend.map((t) => t.score), 100);
  return (
    <button
      onClick={onClick}
      className={`card card-hover p-5 text-left ${active ? "!border-brand/40" : ""}`}
    >
      <div className="flex items-center gap-4">
        <div className="relative w-16 h-16 shrink-0">
          <svg viewBox="0 0 64 64" className="-rotate-90 w-16 h-16">
            <circle cx="32" cy="32" r="27" fill="none"
                    stroke="rgba(15,23,42,0.08)" strokeWidth="6" />
            <circle
              cx="32" cy="32" r="27" fill="none"
              stroke={color} strokeWidth="6" strokeLinecap="round"
              strokeDasharray={2 * Math.PI * 27}
              strokeDashoffset={2 * Math.PI * 27 * (1 - (score ?? 0) / 100)}
            />
          </svg>
          <div className="absolute inset-0 flex items-center justify-center">
            <span className="text-lg font-bold" style={{ color }}>
              {score !== null && score !== undefined ? Math.round(score) : "—"}
            </span>
          </div>
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-semibold text-slate-900">{s.name}</div>
          <div className="text-xs text-muted mt-0.5">
            {s.trips_14d} trips · {fmtValue(s.distance_14d_km, 0)} km (14 days)
          </div>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {Object.entries(s.events_14d).map(([type, count]) => (
              <span
                key={type}
                className="chip bg-ink-800 text-muted"
                title={EVENT_TRIGGERS[type]}
              >
                {EVENT_LABELS[type] ?? type} ×{count}
              </span>
            ))}
          </div>
        </div>
      </div>
      {s.trend.length > 1 && (
        <div className="mt-4 flex items-end gap-1 h-10">
          {s.trend.map((t) => (
            <div
              key={t.date}
              title={`${t.date}: ${t.score}`}
              className="flex-1 rounded-sm"
              style={{
                height: `${Math.max(8, (t.score / maxScore) * 100)}%`,
                background: scoreColor(t.score),
                opacity: 0.75,
              }}
            />
          ))}
        </div>
      )}
    </button>
  );
}
