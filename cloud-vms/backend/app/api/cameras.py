"""Cameras, streams and per-camera analytics configuration."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.errors import bad_request
from ..core.logging import redact
from ..core.security import create_stream_token, encrypt_secret
from ..db import get_db
from ..deps import client_ip, get_camera_for, require, scoped_camera_ids
from ..models import Camera, CameraCredential, CameraHealth, User, Zone
from ..schemas import AnalyticsSettings, CameraIn, CameraOut, CameraPatch, HealthOut
from ..services.audit import audit
from ..services.camera_service import default_analytics_config, delete_objects_async, purge_camera_data, stream_url
from ..services.storage import UploadTooLarge, get_storage, save_upload
from ..workers.sources import probe

router = APIRouter(tags=["cameras"])
VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".m4v", ".webm", ".ts", ".mpg", ".mpeg", ".dav", ".h264"}


def camera_out(db: Session, cam: Camera) -> CameraOut:
    out = CameraOut.model_validate(cam)
    out.has_credentials = bool(cam.credential and (cam.credential.username_enc or cam.credential.password_enc))
    out.analytics_config = {**default_analytics_config(), **(cam.analytics_config or {})}
    out.zone_count = db.scalar(select(func.count(Zone.id)).where(Zone.camera_id == cam.id)) or 0
    return out


def _set_credentials(cam: Camera, username: str | None, password: str | None) -> None:
    if username is None and password is None:
        return
    if cam.credential is None:
        cam.credential = CameraCredential()
    if username is not None:
        cam.credential.username_enc = encrypt_secret(username) if username else ""
    if password is not None:
        cam.credential.password_enc = encrypt_secret(password) if password else ""


def _validate_reference(stream_type: str, ref: str) -> None:
    if stream_type == "rtsp" and not ref.lower().startswith(("rtsp://", "rtsps://")):
        raise bad_request("RTSP cameras need a URL starting with rtsp://")
    if stream_type == "http" and not ref.lower().startswith(("http://", "https://")):
        raise bad_request("HTTP streams need a URL starting with http:// or https://")
    if stream_type == "file":
        from ..workers.sources import resolve_file
        if not resolve_file(ref).exists():
            raise bad_request(f"Video file not found: {ref}. Upload it first or give a path on the server.")


@router.get("/cameras", response_model=list[CameraOut])
def list_cameras(user: User = Depends(require("cameras:view")), db: Session = Depends(get_db)):
    q = select(Camera).order_by(Camera.id)
    scope = scoped_camera_ids(user)
    if scope is not None:
        q = q.where(Camera.id.in_(scope))
    return [camera_out(db, c) for c in db.scalars(q)]


@router.post("/cameras", response_model=CameraOut, status_code=201)
def create_camera(body: CameraIn, request: Request, user: User = Depends(require("cameras:manage")),
                  db: Session = Depends(get_db)):
    _validate_reference(body.stream_type, body.stream_reference)
    cam = Camera(name=body.name, description=body.description, location=body.location,
                 stream_type=body.stream_type, stream_reference=body.stream_reference, live_url=body.live_url,
                 enabled=body.enabled, recording_enabled=body.recording_enabled,
                 analytics_enabled=body.analytics_enabled, worker_group=body.worker_group,
                 analytics_config=body.analytics_config.model_dump(exclude_none=True),
                 recording_config=body.recording_config.model_dump(exclude_none=True),
                 status="REGISTERED" if body.enabled else "DISABLED")
    _set_credentials(cam, body.username, body.password)
    db.add(cam)
    db.flush()
    audit(db, user, "camera.create", "camera", cam.id,
          {"name": cam.name, "stream_type": cam.stream_type, "reference": redact(cam.stream_reference)},
          client_ip(request))
    return camera_out(db, cam)


@router.post("/cameras/test-connection")
def test_connection(body: CameraIn, user: User = Depends(require("cameras:manage"))):
    """Try to open a stream before saving it."""
    from ..workers.sources import build_url
    url = build_url(body.stream_type, body.stream_reference, body.username or "", body.password or "")
    r = probe(body.stream_type, url)
    r.pop("frame", None)
    return r


@router.post("/cameras/upload-video")
async def upload_video(file: UploadFile = File(...), user: User = Depends(require("cameras:manage"))):
    """Upload a recorded video (e.g. college gate footage) to use as a camera source."""
    s = get_settings()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        raise bad_request(f"Unsupported video type '{ext}'. Use one of: {', '.join(sorted(VIDEO_EXT))}")
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file.filename).stem)[:60] or "video"
    name = f"{stem}_{uuid.uuid4().hex[:6]}{ext}"
    dst = s.uploads_dir / name
    try:
        save_upload(file.file, dst, s.max_video_upload_mb)
    except UploadTooLarge as exc:
        raise bad_request(str(exc))
    info = probe("file", str(dst))
    info.pop("frame", None)
    if not info.get("ok"):
        dst.unlink(missing_ok=True)
        raise bad_request("The file was uploaded but OpenCV could not decode it. Convert it to MP4 (H.264).")
    return {"stream_reference": name, "size_bytes": dst.stat().st_size, **info}


@router.get("/cameras/{camera_id}", response_model=CameraOut)
def get_camera(camera_id: int, user: User = Depends(require("cameras:view")), db: Session = Depends(get_db)):
    return camera_out(db, get_camera_for(user, db, camera_id))


@router.patch("/cameras/{camera_id}", response_model=CameraOut)
def update_camera(camera_id: int, body: CameraPatch, request: Request,
                  user: User = Depends(require("cameras:manage")), db: Session = Depends(get_db)):
    cam = get_camera_for(user, db, camera_id)
    data = body.model_dump(exclude_unset=True)
    stream_type = data.get("stream_type", cam.stream_type)
    if "stream_reference" in data or "stream_type" in data:
        _validate_reference(stream_type, data.get("stream_reference", cam.stream_reference))
    for k in ("name", "description", "location", "stream_type", "stream_reference", "live_url",
              "recording_enabled", "analytics_enabled", "worker_group"):
        if k in data and data[k] is not None:
            setattr(cam, k, data[k])
    if body.analytics_config is not None:
        cam.analytics_config = {**(cam.analytics_config or {}), **body.analytics_config.model_dump(exclude_none=True)}
    if body.recording_config is not None:
        cam.recording_config = {**(cam.recording_config or {}), **body.recording_config.model_dump(exclude_none=True)}
    if body.clear_credentials and cam.credential:
        cam.credential.username_enc = cam.credential.password_enc = ""
    _set_credentials(cam, data.get("username"), data.get("password"))
    cam.config_version += 1
    changed = sorted(k for k in data if k not in ("password",))
    audit(db, user, "camera.update", "camera", cam.id, {"fields": changed}, client_ip(request))
    return camera_out(db, cam)


@router.patch("/cameras/{camera_id}/analytics-config", response_model=CameraOut)
def update_analytics_config(camera_id: int, body: AnalyticsSettings, request: Request,
                            user: User = Depends(require("cameras:manage")), db: Session = Depends(get_db)):
    cam = get_camera_for(user, db, camera_id)
    cam.analytics_config = {**(cam.analytics_config or {}), **body.model_dump(exclude_none=True)}
    cam.config_version += 1
    audit(db, user, "camera.analytics_config", "camera", cam.id, body.model_dump(exclude_none=True),
          client_ip(request))
    return camera_out(db, cam)


@router.delete("/cameras/{camera_id}")
def delete_camera(camera_id: int, request: Request, user: User = Depends(require("cameras:manage")),
                  db: Session = Depends(get_db)):
    """Delete a camera together with all its statistics, incidents, recordings and stored media,
    so the overview no longer counts anything it saw."""
    from ..workers.supervisor import get_supervisor
    cam = get_camera_for(user, db, camera_id)
    name = cam.name
    sup = get_supervisor()
    if sup is not None:  # stop its pipeline first so nothing is written after the purge
        sup.forget_camera(camera_id)
    removed, keys = purge_camera_data(db, camera_id)
    db.delete(cam)
    audit(db, user, "camera.delete", "camera", camera_id, {"name": name, "removed": removed}, client_ip(request))
    delete_objects_async(keys)
    return {"ok": True, "removed": removed, "files": len(keys)}


def _set_enabled(camera_id: int, enabled: bool, request: Request, user: User, db: Session) -> CameraOut:
    cam = get_camera_for(user, db, camera_id)
    cam.enabled = enabled
    if not enabled:
        cam.status = "DISABLED"
    elif cam.status == "DISABLED":
        cam.status = "REGISTERED"
    audit(db, user, "camera.enable" if enabled else "camera.disable", "camera", cam.id, ip=client_ip(request))
    return camera_out(db, cam)


@router.post("/cameras/{camera_id}/enable", response_model=CameraOut)
def enable_camera(camera_id: int, request: Request, user: User = Depends(require("cameras:manage")),
                  db: Session = Depends(get_db)):
    return _set_enabled(camera_id, True, request, user, db)


@router.post("/cameras/{camera_id}/disable", response_model=CameraOut)
def disable_camera(camera_id: int, request: Request, user: User = Depends(require("cameras:manage")),
                   db: Session = Depends(get_db)):
    return _set_enabled(camera_id, False, request, user, db)


# ---- streams (operators may start/stop without full camera management rights)
@router.post("/cameras/{camera_id}/stream/start", response_model=CameraOut)
def stream_start(camera_id: int, request: Request, user: User = Depends(require("streams:control")),
                 db: Session = Depends(get_db)):
    return _set_enabled(camera_id, True, request, user, db)


@router.post("/cameras/{camera_id}/stream/stop", response_model=CameraOut)
def stream_stop(camera_id: int, request: Request, user: User = Depends(require("streams:control")),
                db: Session = Depends(get_db)):
    return _set_enabled(camera_id, False, request, user, db)


@router.get("/cameras/{camera_id}/stream/status")
def stream_status(camera_id: int, user: User = Depends(require("cameras:view")), db: Session = Depends(get_db)):
    from ..workers.supervisor import get_supervisor
    cam = get_camera_for(user, db, camera_id)
    sup = get_supervisor()
    live = sup.status()["cameras"].get(camera_id) if sup else None
    return {"camera_id": cam.id, "desired": "running" if cam.enabled else "stopped", "status": cam.status,
            "message": cam.status_message, "worker": live}


@router.get("/cameras/{camera_id}/health")
def camera_health(camera_id: int, minutes: int = Query(60, ge=1, le=60 * 24 * 7),
                  user: User = Depends(require("cameras:view")), db: Session = Depends(get_db)):
    from datetime import timedelta
    from ..core.timeutil import utcnow
    cam = get_camera_for(user, db, camera_id)
    rows = db.scalars(select(CameraHealth).where(CameraHealth.camera_id == cam.id,
                                                 CameraHealth.ts >= utcnow() - timedelta(minutes=minutes))
                      .order_by(CameraHealth.ts)).all()
    step = max(1, len(rows) // 300)
    return {"camera_id": cam.id, "status": cam.status, "message": cam.status_message,
            "last_seen_at": camera_out(db, cam).model_dump()["last_seen_at"],
            "series": [HealthOut.model_validate(r).model_dump() for r in rows[::step]]}


@router.get("/cameras/{camera_id}/snapshot")
def camera_snapshot(camera_id: int, user: User = Depends(require("cameras:view")), db: Session = Depends(get_db)):
    """Signed URL of the latest thumbnail (used by the zone editor)."""
    cam = get_camera_for(user, db, camera_id)
    if not cam.thumbnail_key:
        # no frame yet: grab one directly from the source (stream may be stopped)
        from ..analytics.annotate import encode_jpeg, resize_to_width
        from ..services.storage import thumbnail_key
        r = probe(cam.stream_type, stream_url(cam))
        if not r.get("ok"):
            raise bad_request("No frame available yet — start the camera or check the stream")
        frame = r["frame"]
        key = thumbnail_key(cam.id)
        get_storage().put_bytes(key, encode_jpeg(resize_to_width(frame, 1280), 85), "image/jpeg")
        cam.thumbnail_key, cam.frame_width, cam.frame_height = key, frame.shape[1], frame.shape[0]
        db.commit()
    return {"url": get_storage().signed_url(cam.thumbnail_key, 300), "width": cam.frame_width,
            "height": cam.frame_height}


@router.post("/cameras/{camera_id}/live-token")
def live_token(camera_id: int, user: User = Depends(require("live:view")), db: Session = Depends(get_db)):
    cam = get_camera_for(user, db, camera_id)
    token = create_stream_token(user.id, user.token_version, f"live:{cam.id}")
    return {"mjpeg_url": f"/api/live/{cam.id}/mjpeg?token={token}", "live_url": cam.live_url or None,
            "expires_in": get_settings().stream_token_ttl_seconds}
