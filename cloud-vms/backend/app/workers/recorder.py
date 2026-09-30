"""Continuous recording with FFmpeg — a pipeline fully separate from AI inference.

Each recording camera runs one FFmpeg process that reads the source directly
and writes fixed-length H.264 segments (fragmented MP4, so a segment cut short
by a crash is still playable). Finished segments are picked up from FFmpeg's
segment list, uploaded to object storage and registered in the database.
Segments left behind by a crash are recovered as ``incomplete`` on restart.
"""
from __future__ import annotations

import csv
import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2

from ..core.config import get_settings
from ..core.logging import redact
from ..db import session_scope
from ..models import Camera, RecordingSegment
from ..services.storage import get_storage, recording_key
from .evidence import ffmpeg_exe

log = logging.getLogger("vms.recorder")

DEFAULT_RECORDING = {"segment_seconds": 60, "fps": 10, "max_height": 720, "crf": 28}


def _parse_segment_start(name: str) -> datetime:
    local = datetime.strptime(Path(name).stem[:15], "%Y%m%d-%H%M%S")
    return local.astimezone(timezone.utc).replace(tzinfo=None)  # FFmpeg strftime = OS local time


def _probe_duration(path: Path) -> float:
    cap = cv2.VideoCapture(str(path))
    try:
        n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS)
        return float(n / fps) if n > 0 and fps > 0 else 0.0
    finally:
        cap.release()


class Recorder:
    def __init__(self, camera_id: int, stream_type: str, url: str, config: dict | None = None,
                 loop_files: bool = True):
        self.camera_id, self.stream_type, self.url = camera_id, stream_type, url
        self.cfg = {**DEFAULT_RECORDING, **(config or {})}
        self.loop_files = loop_files
        self.dir = get_settings().tmp_dir / "recording" / f"cam{camera_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.list_file = self.dir / "segments.csv"
        self.proc: subprocess.Popen | None = None
        self._ingested: set[str] = set()
        self.consecutive_failures = 0
        self._next_start = 0.0
        self.last_error = ""
        self.segments_written = 0

    @property
    def supported(self) -> bool:
        return self.stream_type in ("file", "rtsp", "http")

    def _cmd(self) -> list[str]:
        c = self.cfg
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
        if self.stream_type == "file":
            cmd += ["-re"] + (["-stream_loop", "-1"] if self.loop_files else [])
        elif self.stream_type == "rtsp":
            cmd += ["-rtsp_transport", "tcp"]
        cmd += ["-i", self.url, "-an", "-map", "0:v:0",
                "-vf", f"fps={c['fps']},scale=-2:'min({c['max_height']},ih)'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(c["crf"]), "-pix_fmt", "yuv420p",
                "-g", str(int(c["fps"]) * 2), "-f", "segment", "-segment_time", str(c["segment_seconds"]),
                "-reset_timestamps", "1", "-segment_format", "mp4",
                "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
                "-segment_list", str(self.list_file), "-segment_list_type", "csv",
                "-strftime", "1", str(self.dir / "%Y%m%d-%H%M%S.mp4")]
        return cmd

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not self.supported:
            self.last_error = f"recording is not supported for {self.stream_type} sources"
            return
        self.recover()
        self.list_file.unlink(missing_ok=True)
        self._ingested.clear()
        log_path = self.dir / "ffmpeg.log"
        self._log = open(log_path, "ab")
        self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._log)
        self._started_at = time.time()
        log.info("camera %s: recording started (%s)", self.camera_id, redact(self.url))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:  # 'q' asks FFmpeg to finish the current segment cleanly (works on Windows too)
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None
        self.ingest()
        self.recover()  # anything unlisted (killed mid-segment) becomes 'incomplete'
        if getattr(self, "_log", None):
            self._log.close()

    def poll(self) -> str | None:
        """Called periodically. Ingests finished segments; restarts FFmpeg with backoff.
        Returns an error message when recording has failed repeatedly."""
        self.ingest()
        if self.proc is None or not self.supported:
            return None
        rc = self.proc.poll()
        if rc is None:
            if time.time() - self._started_at > 30:
                self.consecutive_failures = 0
            return None
        # FFmpeg exited
        self.consecutive_failures += 1
        tail = (self.dir / "ffmpeg.log").read_text(errors="ignore")[-400:] if (self.dir / "ffmpeg.log").exists() else ""
        self.last_error = redact(tail.strip().splitlines()[-1] if tail.strip() else f"ffmpeg exited with {rc}")
        log.warning("camera %s: recorder exited (%s): %s", self.camera_id, rc, self.last_error)
        self.proc = None
        backoff = min(60, 2 ** self.consecutive_failures)
        self._next_start = time.time() + backoff
        return self.last_error if self.consecutive_failures >= 3 else None

    def maybe_restart(self) -> None:
        if self.proc is None and self.supported and time.time() >= self._next_start:
            self.start()

    # ---------------------------------------------------------------- ingestion
    def ingest(self) -> int:
        if not self.list_file.exists():
            return 0
        n = 0
        with open(self.list_file, newline="") as f:
            rows = list(csv.reader(f))
        for row in rows:
            if not row or row[0] in self._ingested:
                continue
            name = row[0]
            try:
                duration = float(row[2]) - float(row[1])
            except (IndexError, ValueError):
                duration = 0.0
            if self._register(self.dir / name, duration, "complete"):
                n += 1
            self._ingested.add(name)
        return n

    def recover(self) -> int:
        """Register leftover segment files from a crashed run as 'incomplete'."""
        n = 0
        listed = set()
        if self.list_file.exists():
            self.ingest()
            listed = set(self._ingested)
        for path in sorted(self.dir.glob("*.mp4")):
            if path.name in listed:
                continue
            if path.stat().st_size < 1024:
                path.unlink(missing_ok=True)
                continue
            if self._register(path, _probe_duration(path), "incomplete"):
                n += 1
        return n

    def _register(self, path: Path, duration: float, status: str) -> bool:
        if not path.exists():
            return False
        s = get_settings()
        try:
            start = _parse_segment_start(path.name)
        except ValueError:
            start = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(tzinfo=None)
        key = recording_key(self.camera_id, start, path.name)
        seg = RecordingSegment(camera_id=self.camera_id, start_ts=start,
                               end_ts=start + timedelta(seconds=max(0.0, duration)),
                               duration_s=round(duration, 2), object_key=key, status=status,
                               retention_until=start + timedelta(days=s.retention_recordings_days))
        try:
            size, attempts = get_storage().put_file_with_retry(key, path, "video/mp4", move=True)
            seg.size_bytes, seg.upload_attempts = size, attempts
        except Exception as exc:
            seg.status, seg.upload_attempts = "failed", 3
            log.error("camera %s: segment upload failed: %s", self.camera_id, exc)
        with session_scope() as db:
            if db.get(Camera, self.camera_id) is None:  # camera deleted meanwhile: don't keep the file
                if seg.status != "failed":
                    get_storage().delete(key)
                return False
            if db.query(RecordingSegment).filter_by(object_key=key).first() is None:
                db.add(seg)
        self.segments_written += 1
        return True
