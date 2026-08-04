// PREDICT v3 — REST client (thin fetch wrapper) + shared query keys
import type {
  Alert, AppSettings, Car, DrivingCalendar, DrivingSummary, History,
  MaintenanceEntry, ModelStatus, ModelEvaluation, OverviewCar, Prognostics,
  Rule, Summary, TimelineEvent, Trip, DrivingEvent, Vitals, WorkOrder,
} from "./types";

const BASE = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail))
        detail = body.detail.map((d: any) => d.msg).join("; ");
    } catch { /* keep default */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const patch = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });

export const qk = {
  overview: ["overview"] as const,
  summary: ["summary"] as const,
  car: (id: number) => ["car", id] as const,
  vitals: (id: number) => ["vitals", id] as const,
  alerts: (status: string) => ["alerts", status] as const,
  workorders: (status: string) => ["workorders", status] as const,
  history: ["maintenance-history"] as const,
  driving: ["driving-summary"] as const,
  trips: (id: number, day?: string | null) => ["trips", id, day ?? "all"] as const,
  drivingCalendar: (id: number, year: number, month: number) =>
    ["driving-calendar", id, year, month] as const,
  drivingEvents: (id: number, day?: string | null) =>
    ["driving-events", id, day ?? "all"] as const,
  timeline: (id: number) => ["timeline", id] as const,
  prognostics: (id: number) => ["prognostics", id] as const,
  settings: ["settings"] as const,
  rules: ["rules"] as const,
};

export const api = {
  // overview / header
  overview: () => request<OverviewCar[]>("/overview"),
  summary: () => request<Summary>("/live/summary"),

  // cars
  cars: () => request<Car[]>("/cars"),
  car: (id: number) => request<Car>(`/cars/${id}`),
  registerCar: (body: unknown) => post<Car>("/cars", body),
  updateCar: (id: number, body: unknown) => patch<Car>(`/cars/${id}`, body),
  deleteCar: (id: number) => request(`/cars/${id}`, { method: "DELETE" }),
  vitals: (id: number) => request<Vitals>(`/cars/${id}/vitals`),
  prognostics: (id: number) => request<Prognostics>(`/cars/${id}/prognostics`),
  history: (id: number, sensor: string, hours: number) =>
    request<History>(`/cars/${id}/history?sensor_type=${sensor}&hours=${hours}`),
  timeline: (id: number) => request<{ events: TimelineEvent[] }>(`/cars/${id}/timeline`),
  drivingEvents: (id: number, day?: string | null) => {
    const tz = new Date().getTimezoneOffset();
    const q = day
      ? `?day=${day}&tz_offset=${tz}&limit=300`
      : `?days=7&limit=100`;
    return request<DrivingEvent[]>(`/cars/${id}/driving-events${q}`);
  },

  // alerts (active + history — alerts are never deleted)
  alerts: (status = "active", vehicleId?: number) =>
    request<Alert[]>(
      `/alerts?status=${status}${vehicleId ? `&vehicle_id=${vehicleId}` : ""}`,
    ),
  resolveAlert: (id: number) => post<Alert>(`/alerts/${id}/resolve`),
  dismissAlert: (id: number) => post<Alert>(`/alerts/${id}/dismiss`),

  // work orders (maintenance board) + history
  workorders: (status = "board", vehicleId?: number) =>
    request<WorkOrder[]>(
      `/workorders?status=${status}${vehicleId ? `&vehicle_id=${vehicleId}` : ""}`,
    ),
  createWorkOrder: (body: unknown) => post<WorkOrder>("/workorders", body),
  approveWorkOrder: (id: number) => post<WorkOrder>(`/workorders/${id}/approve`),
  startWorkOrder: (id: number) => post<WorkOrder>(`/workorders/${id}/start`),
  completeWorkOrder: (id: number, body: {
    notes?: string; cost?: number; odometer?: number;
    failure_class?: string; failure_component?: string; failure_symptom?: string;
  }) => post<WorkOrder>(`/workorders/${id}/complete`, body),
  cancelWorkOrder: (id: number) => post<WorkOrder>(`/workorders/${id}/cancel`),

  // maintenance history
  maintenanceHistory: (vehicleId?: number) =>
    request<MaintenanceEntry[]>(`/maintenance${vehicleId ? `?vehicle_id=${vehicleId}` : ""}`),
  maintenanceCsvUrl: (vehicleId?: number) =>
    `${BASE}/maintenance/export.csv${vehicleId ? `?vehicle_id=${vehicleId}` : ""}`,

  // driving
  drivingSummary: () => request<DrivingSummary[]>("/driving/summary"),
  trips: (id: number, day?: string | null) => {
    const tz = new Date().getTimezoneOffset();
    const q = day ? `?day=${day}&tz_offset=${tz}` : "";
    return request<Trip[]>(`/driving/cars/${id}/trips${q}`);
  },
  drivingCalendar: (id: number, year: number, month: number) => {
    const tz = new Date().getTimezoneOffset();
    return request<DrivingCalendar>(
      `/driving/cars/${id}/calendar?year=${year}&month=${month}&tz_offset=${tz}`,
    );
  },

  // rules (toggle + threshold only)
  rules: () => request<Rule[]>("/rules"),
  patchRule: (id: number, body: unknown) => patch<Rule>(`/rules/${id}`, body),

  // predictive models (ML)
  modelStatus: () => request<ModelStatus>("/models/status"),
  modelEvaluation: () => request<ModelEvaluation>("/models/evaluate"),
  vehicleModelScores: (id: number) =>
    request<{ vehicle_id: number; scores: Record<string, number> }>(`/models/vehicles/${id}`),

  // settings
  settings: () => request<AppSettings>("/settings"),
  patchSettings: (values: Record<string, string>) =>
    patch("/settings", { values }),
};
