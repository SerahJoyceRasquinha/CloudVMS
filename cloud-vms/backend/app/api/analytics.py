"""Analytics & performance endpoints."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.timeutil import iso_z, local_day_bounds_utc, to_local, utcnow
from ..db import get_db
from ..deps import get_camera_for, require, scoped_camera_ids
from ..models import Camera, CameraHealth, PerfRun, User
from ..services import stats_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _range(start: Optional[datetime], end: Optional[datetime]) -> tuple[datetime, datetime]:
    def n(dt):
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt and dt.tzinfo else dt
    if start is None and end is None:
        return local_day_bounds_utc()  # default: today (campus local time)
    e = n(end) or utcnow()
    s = n(start) or e - timedelta(days=1)
    return s, e


def _camera_filter(user: User, camera_id: list[int]):
    scope = scoped_camera_ids(user)
    if camera_id:
        return [c for c in camera_id if scope is None or c in scope]
    return scope


@router.get("/summary")
def analytics_summary(start: Optional[datetime] = None, end: Optional[datetime] = None,
                      camera_id: list[int] = Query(default=[]), user: User = Depends(require("analytics:view")),
                      db: Session = Depends(get_db)):
    from ..workers.supervisor import get_supervisor
    s, e = _range(start, end)
    sup = get_supervisor()
    live = None
    if sup and "system:view" in user.permissions:
        st = sup.status()
        live = {"detector": st["detector"], "device": st["device"], "inference": st["inference"]}
    return stats_service.summary(db, s, e, _camera_filter(user, camera_id), live)


@router.get("/cameras/{camera_id}")
def camera_analytics(camera_id: int, start: Optional[datetime] = None, end: Optional[datetime] = None,
                     user: User = Depends(require("analytics:view")), db: Session = Depends(get_db)):
    cam = get_camera_for(user, db, camera_id)
    s, e = _range(start, end)
    return {"camera": {"id": cam.id, "name": cam.name, "status": cam.status},
            **stats_service.summary(db, s, e, [cam.id])}


@router.get("/crossings.csv")
def export_crossings(start: Optional[datetime] = None, end: Optional[datetime] = None,
                     camera_id: list[int] = Query(default=[]), user: User = Depends(require("analytics:view")),
                     db: Session = Depends(get_db)):
    s, e = _range(start, end)
    wanted = _camera_filter(user, camera_id)
    existing = [c for c in db.scalars(select(Camera.id)) if wanted is None or c in wanted]  # not deleted ones
    rows = stats_service.unique_crossings(db, s, e, existing)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_local", "timestamp_utc", "camera_id", "group", "class", "model_label", "direction"])
    for r in rows:
        w.writerow([to_local(r["ts"]).isoformat(timespec="seconds"), iso_z(r["ts"]), r["camera_id"], r["group"],
                    r["cls"], r["subtype"], r["direction"]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=unique_crossings.csv"})


@router.get("/performance")
def performance(minutes: int = Query(60, ge=5, le=60 * 24 * 7), camera_id: list[int] = Query(default=[]),
                user: User = Depends(require("analytics:view")), db: Session = Depends(get_db)):
    """Time series of processing metrics + stored benchmark / evaluation runs."""
    since = utcnow() - timedelta(minutes=minutes)
    scope = _camera_filter(user, camera_id)
    q = select(CameraHealth).where(CameraHealth.ts >= since)
    if scope is not None:
        q = q.where(CameraHealth.camera_id.in_(scope or [-1]))
    rows = db.scalars(q.order_by(CameraHealth.ts)).all()
    # bucket to ~120 points
    span = max(1.0, minutes * 60 / 120)
    buckets: dict[int, dict] = {}
    for r in rows:
        b = int((r.ts - since).total_seconds() // span)
        d = buckets.setdefault(b, {"n": 0, "cams": set(), "input_fps": 0.0, "inference_fps": 0.0,
                                   "end_to_end_ms": 0.0, "inference_ms": 0.0, "cpu_percent": 0.0, "mem_mb": 0.0,
                                   "frames_dropped": 0, "ts": r.ts})
        d["n"] += 1
        d["cams"].add(r.camera_id)
        for k in ("input_fps", "inference_fps", "end_to_end_ms", "inference_ms"):
            d[k] += getattr(r, k)
        d["cpu_percent"] = max(d["cpu_percent"], r.cpu_percent)
        d["mem_mb"] = max(d["mem_mb"], r.mem_mb)
        d["frames_dropped"] = max(d["frames_dropped"], r.frames_dropped)
    series = []
    for b in sorted(buckets):
        d = buckets[b]
        n = d["n"]
        series.append({"ts": iso_z(d["ts"]), "cameras": len(d["cams"]),
                       "input_fps": round(d["input_fps"] / n, 2), "inference_fps": round(d["inference_fps"] / n, 2),
                       "end_to_end_ms": round(d["end_to_end_ms"] / n, 1), "inference_ms": round(d["inference_ms"] / n, 1),
                       "cpu_percent": round(d["cpu_percent"], 1), "mem_mb": round(d["mem_mb"], 1),
                       "frames_dropped": d["frames_dropped"]})
    runs = db.scalars(select(PerfRun).order_by(PerfRun.created_at.desc()).limit(50)).all()
    return {"series": series,
            "runs": [{"id": r.id, "name": r.name, "kind": r.kind, "config": r.config, "results": r.results,
                      "created_at": iso_z(r.created_at)} for r in runs],
            "health_rows": db.scalar(select(func.count(CameraHealth.id))) or 0}
