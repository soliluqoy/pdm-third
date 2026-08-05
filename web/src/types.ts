// PREDICT v3 — API types (mirror of the backend payloads)

export type Health = "green" | "yellow" | "red" | "grey";
export type Severity = "critical" | "warning" | "info";
export type AlertStatus = "active" | "resolved" | "dismissed";
export type WorkOrderStatus =
  | "suggested"
  | "open"
  | "in_progress"
  | "done"
  | "cancelled";
export type WorkOrderPriority = "urgent" | "high" | "medium" | "low";
export type WorkOrderSource = "auto" | "manual";

export interface LiveSensor {
  value: number;
  unit: string;
  ts: string;
}

export interface LiveState {
  live: boolean;
  age_seconds: number | null;
  last_seen: string | null;
  ignition: boolean | null;
  movement: boolean | null;
  connected: boolean;
  gps: {
    latitude: number | null;
    longitude: number | null;
    speed: number | null;
    satellites: number | null;
  } | null;
  sensors: Record<string, LiveSensor>;
}

export interface AlertCounts {
  critical?: number;
  warning?: number;
  info?: number;
  total?: number;
}

export interface CompactPrognostics {
  battery: number | null;
  brakes: number | null;
  oil: number | null;
  battery_rul_days?: number | null;
}

export interface Prognostics {
  vehicle_id: number;
  collecting?: boolean;
  battery_score: number | null;
  brake_score: number | null;
  oil_score: number | null;
  battery_rul_days: number | null;
  battery_rul_days_lo?: number | null;
  battery_rul_days_hi?: number | null;
  brake_remaining_km: number | null;
  brake_remaining_km_lo?: number | null;
  brake_remaining_km_hi?: number | null;
  oil_remaining_km: number | null;
  oil_remaining_km_lo?: number | null;
  oil_remaining_km_hi?: number | null;
  brake_energy_mj_total?: number | null;
  drivers: {
    battery?: { top_reason?: string; [k: string]: unknown };
    brakes?: { top_reason?: string; [k: string]: unknown };
    oil?: { top_reason?: string; [k: string]: unknown };
  };
  updated_at: string | null;
}

// /overview card
export interface OverviewCar {
  id: number;
  name: string;
  license_plate: string | null;
  device_type: string;
  health: Health;
  last_seen: string | null;
  live: LiveState;
  alerts: AlertCounts;
  open_work_orders: number;
  prognostics?: CompactPrognostics | null;
}

export interface Car {
  id: number;
  name: string;
  license_plate: string | null;
  device_type: string;
  health: Health;
  last_seen: string | null;
  live: LiveState;
  make: string | null;
  model: string | null;
  year: number | null;
  vin: string | null;
  imei: string;
  sim_phone: string | null;
  mass_kg?: number | null;
  oil_capacity_l?: number | null;
  brake_pad_capacity_mj?: number | null;
  regen_fraction?: number | null;
  last_oil_change_at?: string | null;
  last_oil_change_odo?: number | null;
  last_brake_service_at?: string | null;
  last_brake_service_odo?: number | null;
  created_at: string | null;
  open_alerts: number;
  open_work_orders: number;
  today_score: number | null;
  prognostics?: CompactPrognostics | null;
}

export interface VitalsSensor {
  sensor_type: string;
  name: string;
  unit: string;
  decimals: number;
  value: number;
  ts: string;
}

export interface VitalsGroup {
  group: string;
  label: string;
  sensors: VitalsSensor[];
}

export interface Vitals {
  live: boolean;
  last_seen: string | null;
  ignition: boolean | null;
  gps: LiveState["gps"];
  groups: VitalsGroup[];
}

export interface Alert {
  id: number;
  vehicle_id: number;
  vehicle_name: string | null;
  rule_id: number | null;
  severity: Severity;
  status: AlertStatus;
  title: string;
  message: string;
  trigger_value: number | null;
  trigger_timestamp: string | null;
  occurrence_count: number;
  created_at: string | null;
  resolved_at: string | null;
  work_order_id: number | null;
}

export interface WorkOrder {
  id: number;
  vehicle_id: number;
  vehicle_name: string | null;
  alert_id: number | null;
  title: string;
  description: string | null;
  priority: WorkOrderPriority;
  status: WorkOrderStatus;
  source: WorkOrderSource;
  created_at: string | null;
  due_date: string | null;
  started_at: string | null;
  completed_at: string | null;
  completion_notes: string | null;
  cost: number | null;
  odometer_at_completion: number | null;
}

export type FailureClass = "preventive" | "reactive" | null;
export type FailureComponent = "battery" | "brakes" | "oil" | "cooling" | "engine" | "other";

export interface MaintenanceEntry {
  id: number;
  vehicle_id: number;
  vehicle_name: string | null;
  work_order_id: number | null;
  event_type: string;
  title: string;
  notes: string | null;
  cost: number | null;
  odometer: number | null;
  failure_class: FailureClass;
  event_date: string | null;
}

export interface HistoryPoint {
  ts: string;
  value: number;
  min?: number;
  max?: number;
}

export interface History {
  sensor_type: string;
  name: string;
  unit: string;
  decimals: number;
  resolution: string;
  points: HistoryPoint[];
}

export interface TimelineEvent {
  ts: string;
  kind: "alert" | "work_order" | "maintenance" | "dtc" | "health" | "trip";
  severity: Severity | null;
  title: string;
  detail: string | null;
  ref_id: number;
  status: string | null;
}

export interface DrivingSummary {
  vehicle_id: number;
  name: string;
  today_score: number | null;
  avg_score_7d: number | null;
  distance_14d_km: number;
  trips_14d: number;
  open_trips?: number;
  trend: { date: string; score: number; distance_km: number; trips: number }[];
  events_14d: Record<string, number>;
}

export interface Trip {
  id: number;
  start_ts: string;
  end_ts: string | null;
  is_open: boolean;
  distance_km: number | null;
  duration_seconds: number | null;
  max_speed: number | null;
  avg_speed: number | null;
  idle_seconds: number | null;
  fuel_used: number | null;
  events: number;
}

export interface DrivingEvent {
  id: number;
  ts: string;
  event_type: string;
  value: number | null;
  latitude?: number | null;
  longitude?: number | null;
  source: "device" | "derived";
  trip_id?: number | null;
}

export interface DrivingCalendarDay {
  date: string;
  trips: number;
  events: number;
  distance_km: number;
  score: number | null;
}

export interface DrivingCalendar {
  year: number;
  month: number;
  days: DrivingCalendarDay[];
}

export interface Rule {
  id: number;
  key: string;
  name: string;
  description: string | null;
  rule_type: string;
  vehicle_id: number | null;
  sensor_type: string | null;
  operator: string | null;
  threshold_value: number | null;
  duration_seconds: number | null;
  dtc_code: string | null;
  interval_value: number | null;
  interval_unit: string | null;
  severity: Severity;
  recommendation: string | null;
  auto_work_order: boolean;
  priority: WorkOrderPriority;
  is_active: boolean;
}

export interface AppSettings {
  values: Record<string, string>;
  descriptions: Record<string, string>;
  tracker_public_host: string;
  teltonika_port: number;
  device_models: string[];
}

export interface Summary {
  cars: Record<string, number>;
  cars_total: number;
  alerts: Record<string, number>;
  alerts_total: number;
  urgent: number;
  work_orders: Record<string, number>;
  suggested: number;
  open_todos?: number;
}

// Predictive ML models
export interface ModelStatusEntry {
  status: "trained" | "not_trained";
  version?: number;
  trained_at?: string;
  features: string[];
  threshold?: number;
  min_train_rows?: number;
}

export interface ModelStatus {
  models: Record<string, ModelStatusEntry>;
}

export interface ModelEvaluation {
  status: "ok" | "no_failures_yet";
  detail?: string;
  results?: Record<string, {
    status: "evaluated" | "not_trained";
    true_positives?: number;
    false_positives?: number;
    false_negatives?: number;
    precision?: number;
    recall?: number;
  }>;
}

export type WsEvent =
  | { type: "telemetry"; data: LiveState & { vehicle_id: number } }
  | { type: "health"; data: { vehicle_id: number; health: Health } }
  | { type: "alert"; data: Alert }
  | { type: "alert_resolved"; data: { id: number; vehicle_id: number; title: string; auto: boolean } }
  | { type: "work_order"; data: WorkOrder }
  | { type: "trip"; data: { vehicle_id: number; trip_id: number; action: string } }
  | { type: "driving_event"; data: { vehicle_id: number; event_type: string } }
  | { type: "settings"; data: { values: Record<string, string> } };
