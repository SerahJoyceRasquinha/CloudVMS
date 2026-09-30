"""Request / response schemas (kept in sync with frontend/src/types.ts)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from .core.timeutil import iso_z

UTC = Annotated[datetime, PlainSerializer(iso_z, return_type=Optional[str])]
Severity = Literal["low", "medium", "high", "critical"]
T = TypeVar("T")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# ------------------------------------------------------------------ auth / users
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class RoleOut(ORM):
    id: int
    name: str
    description: str
    permissions: list[str]


class UserOut(ORM):
    id: int
    username: str
    full_name: str
    is_active: bool
    camera_scope: Optional[list[int]]
    must_change_password: bool
    created_at: UTC
    last_login_at: Optional[UTC] = None
    roles: list[str] = []
    permissions: list[str] = []

    @classmethod
    def of(cls, u) -> "UserOut":
        return cls(id=u.id, username=u.username, full_name=u.full_name, is_active=u.is_active,
                   camera_scope=u.camera_scope, must_change_password=u.must_change_password,
                   created_at=u.created_at, last_login_at=u.last_login_at,
                   roles=sorted(r.name for r in u.roles), permissions=sorted(u.permissions))


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.\-]+$")
    full_name: str = ""
    password: str = Field(min_length=8, max_length=200)
    roles: list[str] = ["viewer"]
    camera_scope: Optional[list[int]] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    is_active: Optional[bool] = None
    roles: Optional[list[str]] = None
    camera_scope: Optional[list[int]] = None
    scope_all_cameras: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


# ------------------------------------------------------------------ cameras
class AnalyticsSettings(BaseModel):
    """Editable subset of the per-camera analytics configuration."""
    inference_fps: Optional[float] = Field(default=None, ge=0.2, le=30)
    imgsz: Optional[int] = Field(default=None, ge=320, le=1920)
    detector_conf: Optional[float] = Field(default=None, ge=0.05, le=0.9)
    classes: Optional[list[str]] = None
    rider_suppression: Optional[bool] = None
    reid_enabled: Optional[bool] = None
    reid_backend: Optional[Literal["histogram", "osnet"]] = None
    reid_window_seconds: Optional[float] = Field(default=None, ge=0, le=120)
    reid_threshold: Optional[float] = Field(default=None, ge=0.3, le=0.99)
    reid_memory_seconds: Optional[float] = Field(default=None, ge=0, le=86400)
    reid_long_threshold: Optional[float] = Field(default=None, ge=0.5, le=0.99)
    evidence_pre_seconds: Optional[float] = Field(default=None, ge=0, le=60)
    evidence_post_seconds: Optional[float] = Field(default=None, ge=1, le=120)
    evidence_fps: Optional[float] = Field(default=None, ge=1, le=30)
    preview_fps: Optional[float] = Field(default=None, ge=1, le=30)
    realtime: Optional[bool] = None
    loop: Optional[bool] = None
    tracker: Optional[dict[str, Any]] = None
    events: Optional[dict[str, Any]] = None


class RecordingSettings(BaseModel):
    segment_seconds: Optional[int] = Field(default=None, ge=2, le=3600)
    fps: Optional[int] = Field(default=None, ge=1, le=30)
    max_height: Optional[int] = Field(default=None, ge=240, le=2160)
    crf: Optional[int] = Field(default=None, ge=18, le=40)


class CameraIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    location: str = ""
    stream_type: Literal["file", "rtsp", "http", "webcam"]
    stream_reference: str = Field(min_length=1, max_length=500)
    username: Optional[str] = None
    password: Optional[str] = None
    live_url: str = ""
    enabled: bool = True
    recording_enabled: bool = False
    analytics_enabled: bool = True
    worker_group: str = "default"
    analytics_config: AnalyticsSettings = AnalyticsSettings()
    recording_config: RecordingSettings = RecordingSettings()

    @field_validator("stream_reference")
    @classmethod
    def no_inline_credentials(cls, v: str) -> str:
        if "://" in v and "@" in v.split("://", 1)[1].split("/", 1)[0]:
            raise ValueError("put the username/password in the credential fields, not in the URL")
        return v.strip()


class CameraPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = None
    location: Optional[str] = None
    stream_type: Optional[Literal["file", "rtsp", "http", "webcam"]] = None
    stream_reference: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    clear_credentials: bool = False
    live_url: Optional[str] = None
    recording_enabled: Optional[bool] = None
    analytics_enabled: Optional[bool] = None
    worker_group: Optional[str] = None
    analytics_config: Optional[AnalyticsSettings] = None
    recording_config: Optional[RecordingSettings] = None

    @field_validator("stream_reference")
    @classmethod
    def no_inline_credentials(cls, v):
        return CameraIn.no_inline_credentials(v) if v else v


class CameraOut(ORM):
    id: int
    name: str
    description: str
    location: str
    stream_type: str
    stream_reference: str
    has_credentials: bool = False
    live_url: str
    status: str
    status_message: str
    enabled: bool
    recording_enabled: bool
    analytics_enabled: bool
    worker_group: str
    analytics_config: dict
    recording_config: dict
    frame_width: Optional[int]
    frame_height: Optional[int]
    created_at: UTC
    updated_at: UTC
    last_seen_at: Optional[UTC] = None
    zone_count: int = 0


class HealthOut(ORM):
    ts: UTC
    status: str
    input_fps: float
    inference_fps: float
    frames_dropped: int
    reconnects: int
    inference_ms: float
    end_to_end_ms: float
    tracker_ms: float
    active_tracks: int
    queue_depth: int
    cpu_percent: float
    mem_mb: float
    gpu_mem_mb: float


# ------------------------------------------------------------------ zones
class ScheduleWindow(BaseModel):
    days: list[int] = Field(default_factory=lambda: list(range(7)))
    start: str = Field(pattern=r"^([01]\d|2[0-4]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-4]):[0-5]\d$")


class PolicyIn(BaseModel):
    name: str = ""
    object_types: list[str] = []
    schedule: list[ScheduleWindow] = []
    authorized_roles_or_conditions: dict = {}
    action: Literal["alert", "allow"] = "alert"
    severity: Optional[Severity] = None
    enabled: bool = True


class PolicyOut(ORM):
    id: int
    name: str
    object_types: list[str]
    schedule: list[dict]
    authorized_roles_or_conditions: dict
    action: str
    severity: Optional[str]
    enabled: bool


ZoneType = Literal["intrusion", "restricted", "authorized", "monitoring", "counting", "line"]


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    zone_type: ZoneType
    points: list[list[float]]
    enabled: bool = True
    severity: Severity = "medium"
    default_action: Literal["alert", "allow"] = "allow"
    config: dict[str, Any] = {}
    policies: list[PolicyIn] = []


class ZonePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    zone_type: Optional[ZoneType] = None
    points: Optional[list[list[float]]] = None
    enabled: Optional[bool] = None
    severity: Optional[Severity] = None
    default_action: Optional[Literal["alert", "allow"]] = None
    config: Optional[dict[str, Any]] = None
    policies: Optional[list[PolicyIn]] = None


class ZoneOut(ORM):
    id: int
    camera_id: int
    name: str
    zone_type: str
    shape: str
    geometry: dict
    coordinate_system: str
    enabled: bool
    severity: str
    default_action: str
    config: dict
    version: int
    created_by: Optional[int]
    created_at: UTC
    updated_at: UTC
    policies: list[PolicyOut]


# ------------------------------------------------------------------ events
class EvidenceOut(ORM):
    id: int
    kind: str
    media_type: str
    duration_s: float
    size_bytes: int
    upload_status: str
    checksum_sha256: str
    source_start_ts: Optional[UTC] = None
    source_end_ts: Optional[UTC] = None
    url: Optional[str] = None


class EventOut(ORM):
    id: int
    camera_id: int
    camera_name: str = ""
    zone_id: Optional[int]
    zone_name: str = ""
    track_id: Optional[int]
    event_type: str
    object_type: str
    event_timestamp: UTC
    created_at: UTC
    severity: str
    status: str
    confidence: float
    bounding_box: Optional[list[float]]
    title: str
    metadata: dict = Field(default_factory=dict, validation_alias="metadata_")
    has_snapshot: bool = False
    has_clip: bool = False
    rule_version: str
    model_version: str
    notes: str
    acknowledged_by: Optional[int]
    acknowledged_at: Optional[UTC] = None
    resolved_at: Optional[UTC] = None


class EventPatch(BaseModel):
    status: Optional[Literal["NEW", "ACKNOWLEDGED", "INVESTIGATING", "RESOLVED", "DISMISSED"]] = None
    severity: Optional[Severity] = None
    note: str = Field(default="", max_length=2000)


# ------------------------------------------------------------------ recordings
class RecordingOut(ORM):
    id: int
    camera_id: int
    start_ts: UTC
    end_ts: UTC
    duration_s: float
    size_bytes: int
    status: str


# ------------------------------------------------------------------ models / data
class ModelOut(ORM):
    id: int
    name: str
    role: str
    weights: str
    source: str
    dataset: str
    class_map: dict
    metrics: dict
    notes: str
    is_active: bool
    available: bool = True
    created_at: UTC


class ModelIn(BaseModel):
    name: str = Field(min_length=2, max_length=120, pattern=r"^[A-Za-z0-9_.\-]+$")
    role: Literal["shared", "person", "vehicle"]
    weights: str = Field(min_length=2, max_length=400)
    class_map: dict[str, str]
    dataset: str = ""
    notes: str = ""


class DatasetOut(ORM):
    id: int
    name: str
    kind: str
    path: str
    status: str
    stats: dict
    created_at: UTC


class TrainingIn(BaseModel):
    dataset_id: int
    base_model: str = "yolo26n.pt"
    role: Literal["shared", "person", "vehicle"] = "shared"
    epochs: int = Field(default=30, ge=1, le=500)
    imgsz: int = Field(default=640, ge=320, le=1920)
    batch: int = Field(default=8, ge=1, le=128)
    device: str = "auto"


class TrainingOut(ORM):
    id: int
    dataset_id: int
    base_model: str
    role: str
    params: dict
    status: str
    result_model_id: Optional[int]
    metrics: dict
    created_at: UTC
    finished_at: Optional[UTC] = None


class AuditOut(ORM):
    id: int
    ts: UTC
    user_id: Optional[int]
    username: str
    action: str
    target_type: str
    target_id: str
    details: dict
    ip: str
    request_id: str
