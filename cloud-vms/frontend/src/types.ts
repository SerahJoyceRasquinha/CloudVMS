// Mirrors backend/app/schemas.py — keep the two in sync.

export type Severity = "low" | "medium" | "high" | "critical";
export type EventStatus = "NEW" | "ACKNOWLEDGED" | "INVESTIGATING" | "RESOLVED" | "DISMISSED";

export interface User {
  id: number;
  username: string;
  full_name: string;
  is_active: boolean;
  camera_scope: number[] | null;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
  roles: string[];
  permissions: string[];
}

export interface Role { id: number; name: string; description: string; permissions: string[] }

export interface AnalyticsConfig {
  inference_fps: number;
  imgsz: number;
  detector_conf: number;
  classes: string[];
  rider_suppression: boolean;
  reid_enabled: boolean;
  reid_backend: "histogram" | "osnet";
  reid_window_seconds: number;
  reid_threshold: number;
  reid_memory_seconds?: number;
  reid_long_threshold?: number;
  evidence_pre_seconds: number;
  evidence_post_seconds: number;
  evidence_fps: number;
  preview_fps: number;
  realtime: boolean;
  loop: boolean;
  tracker: Record<string, unknown>;
  events: Record<string, unknown>;
}

export interface Camera {
  id: number;
  name: string;
  description: string;
  location: string;
  stream_type: "file" | "rtsp" | "http" | "webcam";
  stream_reference: string;
  has_credentials: boolean;
  live_url: string;
  status: string;
  status_message: string;
  enabled: boolean;
  recording_enabled: boolean;
  analytics_enabled: boolean;
  worker_group: string;
  analytics_config: AnalyticsConfig;
  recording_config: Record<string, number>;
  frame_width: number | null;
  frame_height: number | null;
  created_at: string;
  updated_at: string;
  last_seen_at: string | null;
  zone_count: number;
}

export interface ScheduleWindow { days: number[]; start: string; end: string }

export interface Policy {
  id?: number;
  name: string;
  object_types: string[];
  schedule: ScheduleWindow[];
  authorized_roles_or_conditions?: Record<string, unknown>;
  action: "alert" | "allow";
  severity: Severity | null;
  enabled: boolean;
}

export type ZoneType = "intrusion" | "restricted" | "authorized" | "monitoring" | "counting" | "line";

export interface Zone {
  id: number;
  camera_id: number;
  name: string;
  zone_type: ZoneType;
  shape: "polygon" | "line";
  geometry: { points: [number, number][] };
  enabled: boolean;
  severity: Severity;
  default_action: "alert" | "allow";
  config: Record<string, unknown>;
  version: number;
  policies: Policy[];
  created_at: string;
  updated_at: string;
}

export interface EventItem {
  id: number;
  camera_id: number;
  camera_name: string;
  zone_id: number | null;
  zone_name: string;
  track_id: number | null;
  event_type: string;
  object_type: string;
  event_timestamp: string;
  created_at: string;
  severity: Severity;
  status: EventStatus;
  confidence: number;
  bounding_box: number[] | null;
  title: string;
  metadata: Record<string, any>;
  has_snapshot: boolean;
  has_clip: boolean;
  rule_version: string;
  model_version: string;
  notes: string;
  acknowledged_by: number | null;
  acknowledged_at: string | null;
  resolved_at: string | null;
}

export interface Evidence {
  id: number;
  kind: "snapshot" | "clip";
  media_type: string;
  duration_s: number;
  size_bytes: number;
  upload_status: string;
  checksum_sha256: string;
  url: string | null;
}

export interface Page<T> { items: T[]; total: number; page: number; page_size: number }

export interface Recording {
  id: number;
  camera_id: number;
  start_ts: string;
  end_ts: string;
  duration_s: number;
  size_bytes: number;
  status: string;
}

export interface SeriesBucket {
  bucket: string;
  person_in: number;
  person_out: number;
  vehicle_in: number;
  vehicle_out: number;
  person_seen: number;
  vehicle_seen: number;
  events: number;
}

export interface Summary {
  range: { start: string; end: string; bucket: "hour" | "day" };
  cameras: { total: number; online: number; offline: number; disabled: number; starting: number; recording: number };
  counting_lines_configured: boolean;
  people: { entries: number; exits: number; on_site_estimate: number; unique_seen: number };
  vehicles: {
    entries: number; exits: number; on_site_estimate: number; unique_seen: number;
    types_in: Record<string, number>; types_seen: Record<string, number>;
  };
  series: SeriesBucket[];
  peak: SeriesBucket | null;
  events: {
    total: number; open: number; by_type: Record<string, number>; by_severity: Record<string, number>;
    by_status: Record<string, number>; mean_time_to_ack_s: number | null;
    recent: { id: number; title: string; event_type: string; severity: Severity; status: EventStatus;
      camera_id: number; camera_name: string; event_timestamp: string }[];
  };
  per_camera: ({ camera_id: number; name: string; status: string; person_in: number; person_out: number;
    vehicle_in: number; vehicle_out: number; person_seen: number; vehicle_seen: number; events: number;
    input_fps?: number; inference_fps?: number; end_to_end_ms?: number; frames_dropped?: number })[];
  processing: { cameras_processing: number; avg_end_to_end_ms: number | null; avg_inference_ms: number | null;
    total_inference_fps: number; cpu_percent: number | null; mem_mb: number | null };
  storage_bytes: number;
  workers: { detector?: string; device?: string; inference?: Record<string, number> };
}

export interface ModelVersion {
  id: number; name: string; role: "shared" | "person" | "vehicle"; weights: string; source: string; dataset: string;
  class_map: Record<string, string>; metrics: Record<string, any>; notes: string; is_active: boolean;
  available: boolean; created_at: string;
}

export interface Dataset { id: number; name: string; kind: string; path: string; status: string; stats: Record<string, any>; created_at: string }

export interface TrainingJob {
  id: number; dataset_id: number; base_model: string; role: string; params: Record<string, any>; status: string;
  result_model_id: number | null; metrics: Record<string, any>; created_at: string; finished_at: string | null;
}

export interface AuditEntry {
  id: number; ts: string; user_id: number | null; username: string; action: string; target_type: string;
  target_id: string; details: Record<string, any>; ip: string; request_id: string;
}
