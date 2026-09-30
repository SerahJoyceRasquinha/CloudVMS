"""Evidence capture: rolling pre-event buffer, post-event capture, H.264 clip encoding, upload."""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import get_settings
from ..core.timeutil import from_epoch
from ..db import session_scope
from ..models import Event, EventEvidence
from ..services.event_service import retention_date
from ..services.storage import evidence_key, get_storage, sha256_file

log = logging.getLogger("vms.evidence")


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # bundled binary (works on Windows without installing ffmpeg)
    return imageio_ffmpeg.get_ffmpeg_exe()


class FrameRing:
    """JPEG-compressed rolling buffer (a few MB per camera instead of hundreds)."""

    def __init__(self, seconds: float, fps: float):
        self.fps = fps
        self.frames: deque[tuple[float, bytes]] = deque(maxlen=max(2, int(seconds * fps) + 2))
        self._last = 0.0
        self._lock = threading.Lock()

    def due(self, ts: float) -> bool:
        return ts - self._last >= 1.0 / self.fps

    def push(self, ts: float, jpeg: bytes) -> None:
        with self._lock:
            self.frames.append((ts, jpeg))
            self._last = ts

    def since(self, ts_from: float) -> list[tuple[float, bytes]]:
        with self._lock:
            return [f for f in self.frames if f[0] >= ts_from]


@dataclass
class EvidenceJob:
    event_id: int
    camera_id: int
    event_ts: float
    until_ts: float
    fps: float
    frames: list = field(default_factory=list)

    def add(self, ts: float, jpeg: bytes) -> bool:
        """Append a post-event frame. Returns True once the job has enough footage."""
        if not self.frames or ts > self.frames[-1][0]:
            self.frames.append((ts, jpeg))
        return ts >= self.until_ts


def encode_clip(frames: list[tuple[float, bytes]], fps: float, out: Path) -> bool:
    if len(frames) < 2:
        return False
    span = max(0.5, frames[-1][0] - frames[0][0])
    real_fps = max(1.0, min(30.0, (len(frames) - 1) / span))  # keep clip duration truthful
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe",
           "-framerate", f"{real_fps:.3f}", "-c:v", "mjpeg", "-i", "-",
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        for _, jpeg in frames:
            p.stdin.write(jpeg)
        p.stdin.close()
        err = p.stderr.read().decode(errors="ignore")
        rc = p.wait(timeout=120)
        if rc != 0:
            log.error("ffmpeg failed (%s): %s", rc, err[-500:])
            return False
        return out.exists() and out.stat().st_size > 0
    except Exception:
        log.exception("clip encoding failed")
        return False


class EvidenceWriter:
    def __init__(self, workers: int = 2):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evidence")

    def submit(self, job: EvidenceJob) -> None:
        self.pool.submit(self._write, job)

    def _write(self, job: EvidenceJob) -> None:
        with session_scope() as db:
            if db.get(Event, job.event_id) is None:
                return  # incident (or its camera) deleted while the clip was being captured
        s = get_settings()
        storage = get_storage()
        ev_dt = from_epoch(job.event_ts)
        key = evidence_key(job.camera_id, ev_dt, job.event_id)
        with tempfile.TemporaryDirectory(dir=s.tmp_dir) as td:
            out = Path(td) / "clip.mp4"
            ok = encode_clip(job.frames, job.fps, out)
            with session_scope() as db:
                row = EventEvidence(event_id=job.event_id, kind="clip", camera_id=job.camera_id,
                                    media_type="video/mp4",
                                    source_start_ts=from_epoch(job.frames[0][0]) if job.frames else None,
                                    source_end_ts=from_epoch(job.frames[-1][0]) if job.frames else None,
                                    retention_until=retention_date(s.retention_evidence_days))
                db.add(row)
                if not ok:
                    row.upload_status = "failed"
                    return
                row.checksum_sha256 = sha256_file(out)
                row.duration_s = round(job.frames[-1][0] - job.frames[0][0], 2)
                try:
                    size, attempts = storage.put_file_with_retry(key, out, "video/mp4", move=True)
                    row.object_key, row.size_bytes, row.upload_attempts = key, size, attempts
                    row.upload_status = "uploaded"
                    ev = db.get(Event, job.event_id)
                    if ev is not None:
                        ev.video_clip_object_key = key
                except Exception as exc:
                    row.upload_status, row.upload_attempts = "failed", 3
                    log.error("evidence upload failed for event %s: %s", job.event_id, exc)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True)


def store_snapshot(event_id: int, camera_id: int, event_ts: float, jpeg: bytes) -> None:
    from ..services.storage import snapshot_key
    s = get_settings()
    with session_scope() as db:
        if db.get(Event, event_id) is None:
            return  # deleted meanwhile
    key = snapshot_key(camera_id, from_epoch(event_ts), event_id)
    try:
        size = get_storage().put_bytes(key, jpeg, "image/jpeg")
        status = "uploaded"
    except Exception as exc:
        log.error("snapshot upload failed for event %s: %s", event_id, exc)
        size, status = 0, "failed"
    with session_scope() as db:
        db.add(EventEvidence(event_id=event_id, kind="snapshot", camera_id=camera_id, media_type="image/jpeg",
                             object_key=key if status == "uploaded" else "", size_bytes=size,
                             source_start_ts=from_epoch(event_ts), source_end_ts=from_epoch(event_ts),
                             upload_status=status, upload_attempts=1,
                             retention_until=retention_date(s.retention_evidence_days)))
        ev = db.get(Event, event_id)
        if ev is not None and status == "uploaded":
            ev.snapshot_object_key = key
