"""Statistics for the dashboard and analytics pages.

Definitions (documented so the numbers can be defended in a report):
* **Unique entries / exits** — distinct tracked objects (root track after
  short-term re-identification) that crossed a counting line in the "in" /
  "out" direction. One person counts once per direction per camera even if
  they hover around the line. Cross-camera identity is *not* assumed.
* **Unique objects seen** — distinct identities at a camera after
  re-identification (OSNet appearance matching): somebody who leaves the
  frame and comes back within the re-ID memory counts once. An identity is
  stored the moment it is decided, so the numbers update live.
* **On-site estimate** — entries minus exits since the start of the period;
  an estimate only, it drifts if an exit happens off-camera.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.timeutil import iso_z, to_local, utcnow
from ..models import (Camera, CameraHealth, Crossing, Event, EventEvidence, Identity, RecordingSegment, Track,
                      Zone)

OPEN_STATUSES = ("NEW", "ACKNOWLEDGED", "INVESTIGATING")


def _scope(q, col, camera_ids):
    return q.where(col.in_(camera_ids or [-1])) if camera_ids is not None else q


def unique_crossings(db: Session, start: datetime, end: datetime, camera_ids) -> list[dict]:
    q = select(Crossing.camera_id, Crossing.root_uid, Crossing.object_group, Crossing.object_class,
               Crossing.subtype, Crossing.direction, Crossing.ts).where(Crossing.ts >= start, Crossing.ts < end)
    q = _scope(q, Crossing.camera_id, camera_ids).order_by(Crossing.ts)
    seen, out = set(), []
    for cam, root, group, cls, sub, direction, ts in db.execute(q):
        k = (cam, root, direction)
        if k in seen:
            continue
        seen.add(k)
        out.append({"camera_id": cam, "group": group, "cls": cls, "subtype": sub, "direction": direction, "ts": ts})
    return out


def unique_seen(db: Session, start: datetime, end: datetime, camera_ids) -> list[tuple]:
    """(camera_id, group, class, first_seen) per unique object."""
    seen = {}
    q = select(Identity.camera_id, Identity.root_uid, Identity.object_group, Identity.object_class,
               Identity.first_seen_at).where(Identity.first_seen_at >= start, Identity.first_seen_at < end)
    for cam, root, group, cls, ts in db.execute(_scope(q, Identity.camera_id, camera_ids)):
        seen.setdefault((cam, root), (group, cls, ts))
    # periods recorded before identities existed: fall back to the finished tracks of that time
    first_ident = dict(db.execute(_scope(select(Identity.camera_id, func.min(Identity.first_seen_at))
                                         .group_by(Identity.camera_id), Identity.camera_id, camera_ids)).all())
    q = select(Track.camera_id, Track.root_uid, Track.object_group, Track.object_class, Track.first_seen_at) \
        .where(Track.first_seen_at >= start, Track.first_seen_at < end)
    for cam, root, group, cls, ts in db.execute(_scope(q, Track.camera_id, camera_ids)):
        if cam in first_ident and ts >= first_ident[cam]:
            continue
        seen.setdefault((cam, root), (group, cls, ts))
    return [(k[0], *v) for k, v in seen.items()]


def summary(db: Session, start: datetime, end: datetime, camera_ids, live: dict | None = None) -> dict:
    cams_q = _scope(select(Camera), Camera.id, camera_ids)
    cams = list(db.scalars(cams_q))
    cam_ids = [c.id for c in cams]
    # only cameras that exist: rows left behind by a deleted camera must never be counted
    camera_ids = cam_ids
    stale = utcnow() - timedelta(seconds=30)

    def eff_status(c: Camera) -> str:
        if not c.enabled:
            return "DISABLED"
        if c.status == "ONLINE" and (c.last_seen_at is None or c.last_seen_at < stale):
            return "OFFLINE"  # never trust ONLINE without a recent heartbeat
        return c.status

    status_counts = Counter(eff_status(c) for c in cams)
    lines_configured = bool(db.scalar(select(func.count(Zone.id)).where(Zone.camera_id.in_(cam_ids or [-1]),
                                                                        Zone.shape == "line", Zone.enabled)))
    crossings = unique_crossings(db, start, end, camera_ids)
    seen = unique_seen(db, start, end, camera_ids)

    counts = {g: {"in": 0, "out": 0} for g in ("person", "vehicle")}
    vehicle_types_in: Counter = Counter()
    per_cam = defaultdict(lambda: {"person_in": 0, "person_out": 0, "vehicle_in": 0, "vehicle_out": 0,
                                   "person_seen": 0, "vehicle_seen": 0, "events": 0})
    span_days = (end - start).total_seconds() / 86400
    bucket_fmt = "%Y-%m-%d %H:00" if span_days <= 2.01 else "%Y-%m-%d"
    series: dict[str, dict] = {}

    def bucket(ts: datetime) -> dict:
        k = to_local(ts).strftime(bucket_fmt)
        return series.setdefault(k, {"bucket": k, "person_in": 0, "person_out": 0, "vehicle_in": 0,
                                     "vehicle_out": 0, "person_seen": 0, "vehicle_seen": 0, "events": 0})

    for c in crossings:
        if c["group"] not in counts:
            continue
        counts[c["group"]][c["direction"]] += 1
        per_cam[c["camera_id"]][f"{c['group']}_{c['direction']}"] += 1
        bucket(c["ts"])[f"{c['group']}_{c['direction']}"] += 1
        if c["group"] == "vehicle" and c["direction"] == "in":
            vehicle_types_in[c["cls"]] += 1
    seen_counts = Counter()
    vehicle_types_seen: Counter = Counter()
    for cam, group, cls, ts in seen:
        seen_counts[group] += 1
        if group in ("person", "vehicle"):
            per_cam[cam][f"{group}_seen"] += 1
            bucket(ts)[f"{group}_seen"] += 1
        if group == "vehicle":
            vehicle_types_seen[cls] += 1

    ev_q = _scope(select(Event).where(Event.event_timestamp >= start, Event.event_timestamp < end),
                  Event.camera_id, camera_ids)
    events = list(db.scalars(ev_q.order_by(Event.event_timestamp.desc())))
    by_type, by_sev, by_status = Counter(), Counter(), Counter()
    ack_secs = []
    for e in events:
        by_type[e.event_type] += 1
        by_sev[e.severity] += 1
        by_status[e.status] += 1
        per_cam[e.camera_id]["events"] += 1
        bucket(e.event_timestamp)["events"] += 1
        if e.acknowledged_at:
            ack_secs.append((e.acknowledged_at - e.created_at).total_seconds())
    open_all = db.scalar(_scope(select(func.count(Event.id)).where(Event.status.in_(OPEN_STATUSES)),
                                Event.camera_id, camera_ids)) or 0

    peak = max(series.values(), key=lambda b: b["person_in"] + b["vehicle_in"], default=None)
    names = {c.id: c.name for c in cams}
    health = _latest_health(db, cam_ids)
    storage_bytes = (db.scalar(_scope(select(func.coalesce(func.sum(RecordingSegment.size_bytes), 0)),
                                      RecordingSegment.camera_id, camera_ids)) or 0) + \
                    (db.scalar(_scope(select(func.coalesce(func.sum(EventEvidence.size_bytes), 0)),
                                      EventEvidence.camera_id, camera_ids)) or 0)
    return {
        "range": {"start": iso_z(start), "end": iso_z(end), "bucket": "hour" if bucket_fmt.endswith("00") else "day"},
        "cameras": {"total": len(cams), "online": status_counts.get("ONLINE", 0),
                    "offline": status_counts.get("OFFLINE", 0) + status_counts.get("ERROR", 0),
                    "disabled": status_counts.get("DISABLED", 0),
                    "starting": status_counts.get("REGISTERED", 0),
                    "recording": sum(1 for c in cams if c.enabled and c.recording_enabled)},
        "counting_lines_configured": lines_configured,
        "people": {"entries": counts["person"]["in"], "exits": counts["person"]["out"],
                   "on_site_estimate": max(0, counts["person"]["in"] - counts["person"]["out"]),
                   "unique_seen": seen_counts.get("person", 0)},
        "vehicles": {"entries": counts["vehicle"]["in"], "exits": counts["vehicle"]["out"],
                     "on_site_estimate": max(0, counts["vehicle"]["in"] - counts["vehicle"]["out"]),
                     "unique_seen": seen_counts.get("vehicle", 0),
                     "types_in": dict(vehicle_types_in.most_common()),
                     "types_seen": dict(vehicle_types_seen.most_common())},
        "series": [series[k] for k in sorted(series)],
        "peak": peak,
        "events": {"total": len(events), "open": open_all, "by_type": dict(by_type), "by_severity": dict(by_sev),
                   "by_status": dict(by_status),
                   "mean_time_to_ack_s": round(sum(ack_secs) / len(ack_secs), 1) if ack_secs else None,
                   "recent": [{"id": e.id, "title": e.title, "event_type": e.event_type, "severity": e.severity,
                               "status": e.status, "camera_id": e.camera_id, "camera_name": names.get(e.camera_id, ""),
                               "event_timestamp": iso_z(e.event_timestamp)} for e in events[:10]]},
        "per_camera": [{"camera_id": cid, "name": names.get(cid, f"#{cid}"),
                        "status": eff_status(next(c for c in cams if c.id == cid)), **per_cam[cid],
                        **{k: v for k, v in health.get(cid, {}).items() if k != "status"}} for cid in cam_ids],
        "processing": _processing(health),
        "storage_bytes": int(storage_bytes),
        "workers": live or {},
    }


def _latest_health(db: Session, cam_ids: list[int]) -> dict[int, dict]:
    if not cam_ids:
        return {}
    sub = select(CameraHealth.camera_id, func.max(CameraHealth.ts).label("ts")) \
        .where(CameraHealth.camera_id.in_(cam_ids)).group_by(CameraHealth.camera_id).subquery()
    rows = db.scalars(select(CameraHealth).join(sub, (CameraHealth.camera_id == sub.c.camera_id) &
                                                (CameraHealth.ts == sub.c.ts))).all()
    fresh = utcnow() - timedelta(seconds=60)
    return {r.camera_id: {"input_fps": r.input_fps, "inference_fps": r.inference_fps,
                          "end_to_end_ms": r.end_to_end_ms, "inference_ms": r.inference_ms,
                          "frames_dropped": r.frames_dropped, "active_tracks": r.active_tracks,
                          "cpu_percent": r.cpu_percent, "mem_mb": r.mem_mb, "fresh": r.ts >= fresh}
            for r in rows}


def _processing(health: dict[int, dict]) -> dict:
    live = [h for h in health.values() if h.get("fresh") and h["inference_fps"] > 0]
    if not live:
        return {"cameras_processing": 0, "avg_end_to_end_ms": None, "avg_inference_ms": None,
                "total_inference_fps": 0, "cpu_percent": None, "mem_mb": None}
    return {"cameras_processing": len(live),
            "avg_end_to_end_ms": round(sum(h["end_to_end_ms"] for h in live) / len(live), 1),
            "avg_inference_ms": round(sum(h["inference_ms"] for h in live) / len(live), 1),
            "total_inference_fps": round(sum(h["inference_fps"] for h in live), 2),
            "cpu_percent": max(h["cpu_percent"] for h in live), "mem_mb": max(h["mem_mb"] for h in live)}
