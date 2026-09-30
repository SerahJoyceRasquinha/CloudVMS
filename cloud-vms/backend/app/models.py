"""SQLAlchemy ORM models (transactional metadata only — video lives in object storage)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .core.timeutil import utcnow
from .db import Base


# ============================================================ identity / RBAC
class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    permissions: Mapped[list] = mapped_column(JSON, default=list)


class UserRole(Base):
    __tablename__ = "user_roles"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # None -> all cameras; list[int] -> only these cameras
    camera_scope: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    roles: Mapped[list[Role]] = relationship(secondary="user_roles", lazy="selectin")

    @property
    def permissions(self) -> set[str]:
        out: set[str] = set()
        for r in self.roles:
            out.update(r.permissions or [])
        return out

    def can_see_camera(self, camera_id: int) -> bool:
        return self.camera_scope is None or camera_id in (self.camera_scope or [])


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(80), default="")
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(40), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
    request_id: Mapped[str] = mapped_column(String(64), default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ============================================================ cameras
class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    stream_type: Mapped[str] = mapped_column(String(20))  # file | rtsp | http | webcam
    stream_reference: Mapped[str] = mapped_column(String(500))  # never contains credentials
    live_url: Mapped[str] = mapped_column(String(500), default="")  # optional MediaMTX HLS url
    status: Mapped[str] = mapped_column(String(20), default="REGISTERED")
    status_message: Mapped[str] = mapped_column(String(300), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # desired: stream running
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    analytics_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    worker_group: Mapped[str] = mapped_column(String(40), default="default")
    analytics_config: Mapped[dict] = mapped_column(JSON, default=dict)
    recording_config: Mapped[dict] = mapped_column(JSON, default=dict)
    frame_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    frame_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    thumbnail_key: Mapped[str] = mapped_column(String(300), default="")
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    credential: Mapped[Optional["CameraCredential"]] = relationship(
        back_populates="camera", uselist=False, cascade="all, delete-orphan", lazy="selectin")


class CameraCredential(Base):
    """Stream credentials kept apart from camera metadata and encrypted at rest."""
    __tablename__ = "camera_credentials_reference"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), unique=True)
    username_enc: Mapped[str] = mapped_column(Text, default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    camera: Mapped[Camera] = relationship(back_populates="credential")


class CameraHealth(Base):
    __tablename__ = "camera_health"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20))
    input_fps: Mapped[float] = mapped_column(Float, default=0)
    inference_fps: Mapped[float] = mapped_column(Float, default=0)
    frames_dropped: Mapped[int] = mapped_column(Integer, default=0)
    reconnects: Mapped[int] = mapped_column(Integer, default=0)
    inference_ms: Mapped[float] = mapped_column(Float, default=0)
    end_to_end_ms: Mapped[float] = mapped_column(Float, default=0)
    tracker_ms: Mapped[float] = mapped_column(Float, default=0)
    active_tracks: Mapped[int] = mapped_column(Integer, default=0)
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0)
    mem_mb: Mapped[float] = mapped_column(Float, default=0)
    gpu_mem_mb: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (Index("ix_health_cam_ts", "camera_id", "ts"),)


class RecordingSegment(Base):
    __tablename__ = "recording_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    duration_s: Mapped[float] = mapped_column(Float, default=0)
    object_key: Mapped[str] = mapped_column(String(300), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="complete")  # complete | incomplete | failed
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    retention_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    __table_args__ = (Index("ix_rec_cam_start", "camera_id", "start_ts"),)


# ============================================================ zones & policies
class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    # intrusion | restricted | authorized | monitoring | counting (polygons); line (entry/exit/virtual boundary)
    zone_type: Mapped[str] = mapped_column(String(20))
    shape: Mapped[str] = mapped_column(String(10), default="polygon")  # polygon | line
    geometry: Mapped[dict] = mapped_column(JSON)  # {"points": [[x, y], ...]} normalised 0..1
    coordinate_system: Mapped[str] = mapped_column(String(20), default="normalized")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    default_action: Mapped[str] = mapped_column(String(10), default="allow")  # alert | allow
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    policies: Mapped[list["ZonePolicy"]] = relationship(
        back_populates="zone", cascade="all, delete-orphan", lazy="selectin", order_by="ZonePolicy.id")


class ZonePolicy(Base):
    __tablename__ = "zone_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    object_types: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["vehicle"] or ["person","two_wheeler"]
    schedule: Mapped[list] = mapped_column(JSON, default=list)  # [] = always in force
    authorized_roles_or_conditions: Mapped[dict] = mapped_column(JSON, default=dict)
    action: Mapped[str] = mapped_column(String(10), default="alert")  # alert | allow
    severity: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    zone: Mapped[Zone] = relationship(back_populates="policies")


# ============================================================ perception outputs
class Detection(Base):
    """Optional raw detection log (disabled by default: VMS_STORE_RAW_DETECTIONS)."""
    __tablename__ = "detections"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(Integer)
    frame_ts: Mapped[datetime] = mapped_column(DateTime)
    class_name: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float)
    bbox: Mapped[list] = mapped_column(JSON)
    model_version: Mapped[str] = mapped_column(String(120))
    __table_args__ = (Index("ix_det_cam_ts", "camera_id", "frame_ts"),)


class Track(Base):
    """Summary of a finished track (written when the tracker terminates it)."""
    __tablename__ = "tracks"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(Integer)
    track_uid: Mapped[int] = mapped_column(BigInteger)  # tracker id (unique per camera across restarts)
    root_uid: Mapped[int] = mapped_column(BigInteger)  # identity after re-identification
    object_class: Mapped[str] = mapped_column(String(40))
    object_group: Mapped[str] = mapped_column(String(20))
    subtype: Mapped[str] = mapped_column(String(60), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    observations: Mapped[int] = mapped_column(Integer, default=0)
    track_confidence: Mapped[float] = mapped_column(Float, default=0)
    trajectory: Mapped[list] = mapped_column(JSON, default=list)  # [[t, x, y], ...] normalised, downsampled
    tracking_status: Mapped[str] = mapped_column(String(20), default="TERMINATED")
    __table_args__ = (Index("ix_track_cam_first", "camera_id", "first_seen_at"),
                      Index("ix_track_first_group", "first_seen_at", "object_group"))


class Crossing(Base):
    """One object crossing a counting line in one direction (dedup per root track)."""
    __tablename__ = "crossings"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(Integer)
    zone_id: Mapped[int] = mapped_column(Integer)
    track_uid: Mapped[int] = mapped_column(BigInteger)
    root_uid: Mapped[int] = mapped_column(BigInteger)
    object_class: Mapped[str] = mapped_column(String(40))
    object_group: Mapped[str] = mapped_column(String(20))
    subtype: Mapped[str] = mapped_column(String(60), default="")
    direction: Mapped[str] = mapped_column(String(4))  # in | out
    ts: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (Index("ix_cross_ts_group", "ts", "object_group"),
                      Index("ix_cross_cam_ts", "camera_id", "ts"))


class Identity(Base):
    """One unique person / vehicle at a camera (after re-identification): the basis of the
    "seen" counts. Written as soon as the object's identity is decided, so counts are live."""
    __tablename__ = "identities"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(Integer)
    root_uid: Mapped[int] = mapped_column(BigInteger)
    object_class: Mapped[str] = mapped_column(String(40))
    object_group: Mapped[str] = mapped_column(String(20))
    subtype: Mapped[str] = mapped_column(String(60), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    # re-ID memory survives restarts: appearance prototypes (float16, base64) and when last seen
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    embedder: Mapped[str] = mapped_column(String(40), default="")
    appearance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    __table_args__ = (Index("ix_ident_first_group", "first_seen_at", "object_group"),
                      Index("ix_ident_cam_root", "camera_id", "root_uid"))


# ============================================================ events
class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    zone_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    track_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    object_type: Mapped[str] = mapped_column(String(40), default="")
    event_timestamp: Mapped[datetime] = mapped_column(DateTime)  # source frame time
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="NEW")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    bounding_box: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    snapshot_object_key: Mapped[str] = mapped_column(String(300), default="")
    video_clip_object_key: Mapped[str] = mapped_column(String(300), default="")
    rule_version: Mapped[str] = mapped_column(String(60), default="")
    model_version: Mapped[str] = mapped_column(String(160), default="")
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    acknowledged_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    evidence: Mapped[list["EventEvidence"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (Index("ix_event_cam_ts", "camera_id", "event_timestamp"),
                      Index("ix_event_type_ts", "event_type", "event_timestamp"),
                      Index("ix_event_status_ts", "status", "event_timestamp"))


class EventEvidence(Base):
    __tablename__ = "event_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # snapshot | clip
    object_key: Mapped[str] = mapped_column(String(300), default="")
    media_type: Mapped[str] = mapped_column(String(40), default="")
    duration_s: Mapped[float] = mapped_column(Float, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    camera_id: Mapped[int] = mapped_column(Integer)
    source_start_ts: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source_end_ts: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64), default="")
    upload_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|uploaded|failed
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    retention_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    event: Mapped[Event] = relationship(back_populates="evidence")


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20))  # webhook
    target: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================ models, datasets, experiments
class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(String(20))  # shared | person | vehicle
    weights: Mapped[str] = mapped_column(String(400))  # path or ultralytics hub name
    framework: Mapped[str] = mapped_column(String(40), default="ultralytics")
    class_map: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(40), default="pretrained")  # pretrained | fine-tuned
    dataset: Mapped[str] = mapped_column(String(200), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(30), default="detection")
    path: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), default="ready")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingJob(Base):
    __tablename__ = "training_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    base_model: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="shared")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    log_path: Mapped[str] = mapped_column(String(400), default="")
    result_model_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PerfRun(Base):
    """Stored results of benchmark / evaluation runs shown on the performance page."""
    __tablename__ = "perf_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(30))  # scaling | event_eval | detection_eval
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


__all__ = [n for n in dir() if n[0].isupper()]
_ = UniqueConstraint  # keep import used
