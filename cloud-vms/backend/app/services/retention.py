"""Retention: delete expired recordings, evidence and high-frequency rows safely.

Order matters for consistency: the object is deleted first, then its
database row. If the object deletion fails, the row stays (and is retried
next run), so the database never points at objects we think are gone
while they still exist unaccounted for.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select

from ..core.config import get_settings
from ..core.timeutil import utcnow
from ..db import session_scope
from ..models import CameraHealth, Crossing, Detection, EventEvidence, Identity, RecordingSegment, Track
from .storage import get_storage

log = logging.getLogger("vms.retention")


def run_retention() -> dict:
    s = get_settings()
    storage = get_storage()
    now = utcnow()
    stats = {"recordings": 0, "evidence": 0, "tracks": 0, "health": 0, "failed": 0}
    with session_scope() as db:
        for seg in db.scalars(select(RecordingSegment).where(RecordingSegment.retention_until < now).limit(500)):
            try:
                storage.delete(seg.object_key)
                db.delete(seg)
                stats["recordings"] += 1
            except Exception as exc:
                stats["failed"] += 1
                log.warning("could not delete %s: %s", seg.object_key, exc)
        for ev in db.scalars(select(EventEvidence).where(EventEvidence.retention_until < now).limit(500)):
            try:
                if ev.object_key:
                    storage.delete(ev.object_key)
                db.delete(ev)
                stats["evidence"] += 1
            except Exception as exc:
                stats["failed"] += 1
                log.warning("could not delete %s: %s", ev.object_key, exc)
        cutoff = now - timedelta(days=s.retention_tracks_days)
        stats["tracks"] += db.execute(delete(Track).where(Track.last_seen_at < cutoff)).rowcount or 0
        db.execute(delete(Crossing).where(Crossing.ts < cutoff))
        db.execute(delete(Identity).where(Identity.first_seen_at < cutoff))
        db.execute(delete(Detection).where(Detection.frame_ts < now - timedelta(days=2)))
        stats["health"] = db.execute(delete(CameraHealth).where(
            CameraHealth.ts < now - timedelta(days=s.retention_health_days))).rowcount or 0
    if any(stats.values()):
        log.info("retention cleanup: %s", stats)
    return stats
