"""Camera helpers shared by the API and the workers."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.pipeline import AnalyticsConfig
from ..analytics.policy import PolicySpec
from ..analytics.rules import ZoneSpec
from ..core.security import decrypt_secret
from ..models import Camera, Zone
from ..workers.sources import build_url

STREAM_TYPES = ("file", "rtsp", "http", "webcam")
CAMERA_STATUSES = ("REGISTERED", "ONLINE", "OFFLINE", "ERROR", "DISABLED")


def default_analytics_config() -> dict:
    return AnalyticsConfig().to_dict()


def merged_analytics(cam: Camera) -> AnalyticsConfig:
    return AnalyticsConfig.from_dict({**default_analytics_config(), **(cam.analytics_config or {})})


def zone_to_spec(z: Zone) -> ZoneSpec:
    return ZoneSpec(
        id=z.id, camera_id=z.camera_id, name=z.name, zone_type=z.zone_type, shape=z.shape,
        points=(z.geometry or {}).get("points", []), severity=z.severity, default_action=z.default_action,
        config=z.config or {}, enabled=z.enabled, version=z.version,
        policies=[PolicySpec(p.id, p.object_types or [], p.schedule or [], p.action, p.severity, p.enabled, p.name)
                  for p in z.policies])


def camera_zones(db: Session, camera_id: int) -> list[Zone]:
    return list(db.scalars(select(Zone).where(Zone.camera_id == camera_id).order_by(Zone.id)))


def zones_signature(zones: list[Zone]) -> str:
    raw = json.dumps([[z.id, z.version, z.enabled, str(z.updated_at),
                       [[p.id, p.enabled, p.action, p.object_types, p.schedule] for p in z.policies]]
                      for z in zones], default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


def stream_url(cam: Camera) -> str:
    user = pwd = ""
    if cam.credential:
        user = decrypt_secret(cam.credential.username_enc) if cam.credential.username_enc else ""
        pwd = decrypt_secret(cam.credential.password_enc) if cam.credential.password_enc else ""
    return build_url(cam.stream_type, cam.stream_reference, user, pwd)


def runtime_config(db: Session, cam: Camera):
    from ..workers.camera_worker import CameraRuntimeConfig
    zones = camera_zones(db, cam.id)
    # Zones are always filtered by *this* camera id: one camera's zones can never leak to another.
    specs = [zone_to_spec(z) for z in zones if z.camera_id == cam.id]
    return CameraRuntimeConfig(camera_id=cam.id, name=cam.name, stream_type=cam.stream_type, url=stream_url(cam),
                               analytics_enabled=cam.analytics_enabled, analytics=merged_analytics(cam),
                               zones=specs, config_version=cam.config_version,
                               zones_signature=zones_signature(zones))


def purge_camera_data(db: Session, camera_id: int) -> tuple[dict, list[str]]:
    """Delete everything recorded for a camera: counts, identities, incidents, recordings, health.

    Tracks, crossings and identities have no foreign key to the camera (they are written in bulk by
    the workers), so without this they would keep counting in the overview after the camera is gone.
    Returns row counts and the storage keys (video, snapshots, thumbnail) to delete once committed."""
    from sqlalchemy import delete

    from ..models import (CameraHealth, Crossing, Detection, Event, EventEvidence, Identity, RecordingSegment,
                          Track)
    cam = db.get(Camera, camera_id)
    keys = [k for (k,) in db.execute(select(RecordingSegment.object_key)
                                     .where(RecordingSegment.camera_id == camera_id)) if k]
    keys += [k for (k,) in db.execute(select(EventEvidence.object_key)
                                      .where(EventEvidence.camera_id == camera_id)) if k]
    if cam is not None and cam.thumbnail_key:
        keys.append(cam.thumbnail_key)
    event_ids = select(Event.id).where(Event.camera_id == camera_id)
    counts = {}
    for name, stmt in (
            ("evidence", delete(EventEvidence).where((EventEvidence.camera_id == camera_id)
                                                     | EventEvidence.event_id.in_(event_ids))),
            ("events", delete(Event).where(Event.camera_id == camera_id)),
            ("crossings", delete(Crossing).where(Crossing.camera_id == camera_id)),
            ("identities", delete(Identity).where(Identity.camera_id == camera_id)),
            ("tracks", delete(Track).where(Track.camera_id == camera_id)),
            ("detections", delete(Detection).where(Detection.camera_id == camera_id)),
            ("recordings", delete(RecordingSegment).where(RecordingSegment.camera_id == camera_id)),
            ("health", delete(CameraHealth).where(CameraHealth.camera_id == camera_id))):
        counts[name] = db.execute(stmt.execution_options(synchronize_session=False)).rowcount or 0
    return counts, sorted(set(keys))


def purge_orphaned_data(db: Session) -> dict:
    """Remove statistics of cameras that no longer exist (deleted by an older version, or while a
    separate worker was still writing). SQLite may give a new camera the id of a deleted one, and
    it would then inherit the old counts."""
    from sqlalchemy import delete

    from ..models import CameraHealth, Crossing, Detection, Event, EventEvidence, Identity, RecordingSegment, Track
    alive = select(Camera.id)
    counts = {}
    for name, model in (("tracks", Track), ("crossings", Crossing), ("identities", Identity),
                        ("detections", Detection), ("evidence", EventEvidence), ("events", Event),
                        ("recordings", RecordingSegment), ("health", CameraHealth)):
        stmt = delete(model).where(model.camera_id.not_in(alive)).execution_options(synchronize_session=False)
        n = db.execute(stmt).rowcount or 0
        if n:
            counts[name] = n
    return counts


def delete_objects_async(keys: list[str]) -> None:
    """Remove stored media in the background (can be thousands of recording segments)."""
    if not keys:
        return
    import logging
    import threading

    from .storage import get_storage

    def run():
        storage, failed = get_storage(), 0
        for k in keys:
            try:
                storage.delete(k)
            except Exception:
                failed += 1
        logging.getLogger("vms.camera").info("deleted %d stored files of a removed camera (%d failed)",
                                             len(keys) - failed, failed)
    threading.Thread(target=run, name="purge-media", daemon=True).start()
