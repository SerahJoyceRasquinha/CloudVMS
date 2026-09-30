"""One camera's live pipeline.

Capture thread:   source -> (evidence ring buffer, thumbnails) -> frame sampling -> inference service
Analytics thread: detections -> tracker -> rules -> incidents / crossings -> preview frames

The two threads are decoupled by a small bounded queue, so slow analytics or a
slow database never blocks frame capture, and one camera failing never takes
down the others.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..analytics.annotate import draw_hud, draw_tracks, draw_zones, encode_jpeg, resize_to_width
from ..analytics.pipeline import AnalyticsConfig, CameraAnalytics
from ..analytics.rules import ZoneSpec
from ..core.config import get_settings
from ..core.logging import redact
from ..core.timeutil import local_tz
from ..db import session_scope
from ..models import Camera
from ..services.event_service import notify_async, persist_decision
from ..services.storage import get_storage, thumbnail_key
from .evidence import EvidenceJob, EvidenceWriter, FrameRing, store_snapshot
from .inference import InferenceJob, InferenceService
from .sources import VideoSource

log = logging.getLogger("vms.camera")


def _epoch(dt: datetime) -> float:
    from datetime import timezone
    return dt.replace(tzinfo=timezone.utc).timestamp()  # stored as naive UTC


@dataclass
class CameraRuntimeConfig:
    camera_id: int
    name: str
    stream_type: str
    url: str
    analytics_enabled: bool
    analytics: AnalyticsConfig
    zones: list[ZoneSpec]
    config_version: int = 1
    zones_signature: str = ""


class RateMeter:
    def __init__(self, window: float = 5.0):
        self.window = window
        self.ts: deque[float] = deque()

    def tick(self, now: float | None = None) -> None:
        now = now or time.monotonic()
        self.ts.append(now)
        while self.ts and now - self.ts[0] > self.window:
            self.ts.popleft()

    @property
    def rate(self) -> float:
        if len(self.ts) < 2:
            return 0.0
        span = self.ts[-1] - self.ts[0]
        return (len(self.ts) - 1) / span if span > 0 else 0.0


@dataclass
class CameraMetrics:
    input_fps: float = 0.0
    inference_fps: float = 0.0
    frames_dropped: int = 0
    reconnects: int = 0
    inference_ms: float = 0.0
    end_to_end_ms: float = 0.0
    tracker_ms: float = 0.0
    active_tracks: int = 0
    queue_depth: int = 0
    events_created: int = 0
    events_merged: int = 0
    events_suppressed: int = 0
    crossings: int = 0
    unique_objects: int = 0
    reidentified: int = 0
    line_totals: dict = field(default_factory=dict)


class CameraWorker:
    def __init__(self, rc: CameraRuntimeConfig, inference: InferenceService | None, evidence: EvidenceWriter,
                 db_writer, frame_bus, model_version_fn, device: str = "cpu"):
        self.rc = rc
        self.inference = inference
        self.evidence = evidence
        self.db_writer = db_writer
        self.bus = frame_bus
        self.model_version_fn = model_version_fn
        self.device = device
        self.status = "STARTING"
        self.status_message = ""
        self.last_frame_wall = 0.0
        self.metrics = CameraMetrics()
        self.frame_size: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._results: queue.Queue = queue.Queue(maxsize=4)
        # Offline analysis (a file read as fast as possible) must not drop frames the way a live camera
        # does, or counts change from run to run: capture waits for a free slot instead. The number of
        # slots matches the inference queue limit, so no queue downstream can overflow.
        # (realtime / stream type are part of the stream signature: changing them restarts the worker.)
        self._offline = rc.stream_type == "file" and not rc.analytics.realtime
        self._offline_slots = threading.Semaphore(2)
        self._lock = threading.Lock()
        self._evidence_jobs: list[EvidenceJob] = []
        self._in_meter, self._inf_meter = RateMeter(), RateMeter()
        self._lat_ms: deque[float] = deque(maxlen=50)
        self._inf_ms: deque[float] = deque(maxlen=50)
        self._trk_ms: deque[float] = deque(maxlen=50)
        self._build_analytics()
        self._threads = [threading.Thread(target=self._capture_loop, name=f"cam{rc.camera_id}-capture", daemon=True),
                         threading.Thread(target=self._analytics_loop, name=f"cam{rc.camera_id}-analytics",
                                          daemon=True)]

    # ------------------------------------------------------------ lifecycle
    def _build_analytics(self) -> None:
        cfg = self.rc.analytics
        tz = local_tz()
        old = getattr(self, "analytics", None)
        self.analytics = CameraAnalytics(self.rc.camera_id, cfg, self.rc.zones,
                                         local_time=lambda ts: datetime.fromtimestamp(ts, tz), device=self.device)
        if old is not None:
            # a settings change must not make the camera forget who it has already counted
            for tr in old.flush():
                self.db_writer.track(self.rc.camera_id, tr)
            same_embedder = (old.reid is not None and self.analytics.reid is not None
                             and old.reid.embedder.name == self.analytics.reid.embedder.name)
            if same_embedder:
                self.analytics.reid.identities = old.reid.identities
                for ident in self.analytics.reid.identities.values():
                    ident.active = False
        else:
            self._restore_identities()
        self.ring = FrameRing(cfg.evidence_pre_seconds + 1, cfg.evidence_fps)

    def _restore_identities(self) -> None:
        """Remember who was already counted before a restart (within the re-ID memory window)."""
        reid = self.analytics.reid
        if reid is None or reid.memory <= 0:
            return
        from ..analytics.reid import decode_protos
        from ..core.timeutil import utcnow
        from ..models import Identity
        from datetime import timedelta
        try:
            with session_scope() as db:
                rows = db.query(Identity).filter(
                    Identity.camera_id == self.rc.camera_id, Identity.embedder == reid.embedder.name,
                    Identity.appearance.isnot(None),
                    Identity.last_seen_at >= utcnow() - timedelta(seconds=reid.memory)).all()
                for r in rows:
                    protos = decode_protos(r.appearance)
                    if protos and all(p.shape == protos[0].shape for p in protos):
                        reid.restore(r.root_uid, r.object_group, _epoch(r.last_seen_at), protos)
            if rows:
                log.info("camera %s: remembered %d identities from before the restart", self.rc.camera_id, len(rows))
        except Exception:
            log.exception("camera %s: could not restore re-identification memory", self.rc.camera_id)

    def _remember_all(self) -> None:
        reid = self.analytics.reid
        if reid is None:
            return
        for root, ident in list(reid.identities.items()):
            if ident.protos:
                self.db_writer.remember(self.rc.camera_id, root, reid.embedder.name, ident.last_ts, ident.protos)

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        self.flush_evidence()
        for s in self.analytics.flush():
            self.db_writer.track(self.rc.camera_id, s)
        self._remember_all()
        if self.inference:
            self.inference.remove_camera(self.rc.camera_id)
        self.bus.clear(self.rc.camera_id)
        self.status = "DISABLED"

    @property
    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def reload(self, rc: CameraRuntimeConfig) -> None:
        """Hot-apply new analytics settings or zones without dropping the stream."""
        with self._lock:
            old = self.rc
            self.rc = rc
            if rc.analytics.to_dict() != old.analytics.to_dict():
                self._build_analytics()
            elif rc.zones_signature != old.zones_signature:
                self.analytics.set_zones(rc.zones)
        log.info("camera %s: configuration reloaded", rc.camera_id)

    # ------------------------------------------------------------ capture
    def _capture_loop(self) -> None:
        rc = self.rc
        cfg = rc.analytics
        s = get_settings()
        src = VideoSource(rc.stream_type, rc.url, realtime=cfg.realtime, loop=cfg.loop)
        backoff = 1.0
        last_sample = 0.0
        last_preview = 0.0
        last_thumb = 0.0
        while not self._stop.is_set():
            if src.cap is None:
                if src.open():
                    self.status, self.status_message = "ONLINE", ""
                    self.frame_size = (src.width, src.height)
                    self.last_frame_wall = time.time()
                    backoff = 1.0
                    log.info("camera %s: connected (%dx%d @ %.1f fps)", rc.camera_id, src.width, src.height, src.fps)
                else:
                    self.metrics.reconnects += 1
                    self._mark_offline_if_stale(s.camera_offline_after_seconds, "cannot connect to the stream")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            ok, frame, ts = src.read()
            if not ok:
                if src.ended:
                    self.status, self.status_message = "OFFLINE", "end of video file"
                    src.close()
                    self._stop.wait(5)
                    continue
                self.metrics.reconnects += 1
                log.warning("camera %s: read failed, reconnecting", rc.camera_id)
                src.close()
                self._mark_offline_if_stale(s.camera_offline_after_seconds, "stream interrupted")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            now = time.monotonic()
            self.last_frame_wall = time.time()
            if self.status != "ONLINE":
                self.status, self.status_message = "ONLINE", ""
            self._in_meter.tick(now)
            h, w = frame.shape[:2]
            self.frame_size = (w, h)
            cfg = self.rc.analytics

            # evidence ring buffer + active post-event captures
            if self.ring.due(ts):
                jpeg = encode_jpeg(resize_to_width(frame, cfg.evidence_width), 80)
                self.ring.push(ts, jpeg)
                self._feed_evidence(ts, jpeg)

            if now - last_thumb > 15:
                last_thumb = now
                self._save_thumbnail(frame)

            if self.rc.analytics_enabled and self.inference is not None:
                if ts - last_sample >= 1.0 / max(0.1, cfg.inference_fps) - 0.01:  # 10 ms slack for float timing
                    last_sample = ts
                    if self._offline:
                        while not self._stop.is_set() and not self._offline_slots.acquire(timeout=0.5):
                            pass
                        if self._stop.is_set():
                            break
                    accepted = self.inference.submit(InferenceJob(rc.camera_id, ts, frame, self._on_result))
                    if not accepted:
                        self.metrics.frames_dropped += 1
                        if self._offline:
                            self._offline_slots.release()  # the displaced frame will never come back
            elif now - last_preview >= 1.0 / cfg.preview_fps:
                last_preview = now
                img = resize_to_width(frame, cfg.preview_width).copy()
                draw_hud(img, [f"{rc.name}  (analytics off)"])
                self.bus.publish(rc.camera_id, encode_jpeg(img, 75))
        src.close()

    def _mark_offline_if_stale(self, after: float, message: str) -> None:
        if time.time() - self.last_frame_wall > after or self.last_frame_wall == 0:
            self.status, self.status_message = "OFFLINE", message
        else:
            self.status_message = message

    def _save_thumbnail(self, frame: np.ndarray) -> None:
        try:
            key = thumbnail_key(self.rc.camera_id)
            get_storage().put_bytes(key, encode_jpeg(resize_to_width(frame, 1280), 85), "image/jpeg")
            h, w = frame.shape[:2]
            with session_scope() as db:
                cam = db.get(Camera, self.rc.camera_id)
                if cam is not None:
                    cam.thumbnail_key, cam.frame_width, cam.frame_height = key, w, h
        except Exception as exc:
            log.warning("camera %s: thumbnail update failed: %s", self.rc.camera_id, exc)

    def _feed_evidence(self, ts: float, jpeg: bytes) -> None:
        with self._lock:
            done = [j for j in self._evidence_jobs if j.add(ts, jpeg)]
            for j in done:
                self._evidence_jobs.remove(j)
        for j in done:
            self.evidence.submit(j)

    # ------------------------------------------------------------ analytics
    def _on_result(self, job: InferenceJob, dets, infer_ms: float) -> None:
        try:
            self._results.put_nowait((job, dets, infer_ms))
        except queue.Full:
            try:
                self._results.get_nowait()
                self.metrics.frames_dropped += 1
            except queue.Empty:
                pass
            self._results.put_nowait((job, dets, infer_ms))

    def _analytics_loop(self) -> None:
        last_preview = 0.0
        while not self._stop.is_set():
            try:
                job, dets, infer_ms = self._results.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    analytics, rc = self.analytics, self.rc
                res = analytics.process(job.ts, job.frame, dets)
                self._inf_meter.tick()
                self._inf_ms.append(infer_ms)
                self._trk_ms.append(res.tracker_ms)
                self.metrics.active_tracks = len(res.tracks)
                self.metrics.line_totals = res.line_totals
                s = get_settings()
                for c in res.crossings:
                    self.db_writer.crossing(rc.camera_id, c)
                    self.metrics.crossings += 1
                for ident in res.identities:
                    self.db_writer.identity(rc.camera_id, ident)
                    self.metrics.unique_objects += 1
                for track_uid, root_uid in res.remaps:
                    self.db_writer.remap(rc.camera_id, track_uid, root_uid)
                    self.metrics.reidentified += 1
                if res.released and analytics.reid is not None:
                    for root_uid, last_ts, protos in res.released:
                        self.db_writer.remember(rc.camera_id, root_uid, analytics.reid.embedder.name, last_ts, protos)
                for t in res.terminated:
                    self.db_writer.track(rc.camera_id, t)
                if s.store_raw_detections:
                    for d in res.detections:
                        self.db_writer.detection(rc.camera_id, job.ts, d, self.model_version_fn())
                highlight = self._handle_decisions(res, job)
                now = time.monotonic()
                if highlight or now - last_preview >= 1.0 / rc.analytics.preview_fps:
                    last_preview = now
                    img = self._annotate(job.frame, res, highlight, rc)
                    self.bus.publish(rc.camera_id, encode_jpeg(img, 75))
                self._lat_ms.append((time.perf_counter() - job.submitted) * 1000)
            except Exception:
                log.exception("camera %s: analytics step failed", self.rc.camera_id)
            finally:
                if self._offline:
                    self._offline_slots.release()

    def _annotate(self, frame, res, highlight, rc, width: int | None = None):
        img = frame.copy()
        draw_zones(img, rc.zones, res.zone_occupancy, res.line_totals)
        draw_tracks(img, res.tracks, highlight)
        m = self.metrics
        draw_hud(img, [f"{rc.name}", f"in {self._in_meter.rate:.1f} fps | ai {self._inf_meter.rate:.1f} fps | "
                       f"{(sum(self._lat_ms) / len(self._lat_ms)) if self._lat_ms else 0:.0f} ms | "
                       f"tracks {m.active_tracks}"])
        return resize_to_width(img, width or rc.analytics.preview_width)

    def _handle_decisions(self, res, job) -> set[int]:
        highlight: set[int] = set()
        rc = self.rc
        for d in res.decisions:
            if d.kind == "drop":
                self.metrics.events_suppressed += 1
                continue
            try:
                with session_scope() as db:
                    ev, is_new = persist_decision(db, rc.camera_id, d, self.model_version_fn(),
                                                  rc.analytics.events.get("escalate_at_objects", 3))
                    ev_id = ev.id if ev else None
            except Exception:
                log.exception("camera %s: could not store event", rc.camera_id)
                continue
            if ev_id is None:
                continue
            if d.candidate.track_uid:
                highlight.add(d.candidate.track_uid)
            if not is_new:
                self.metrics.events_merged += 1
                continue
            self.metrics.events_created += 1
            log.info("camera %s: %s (event %s)", rc.camera_id, d.candidate.title, ev_id,
                     extra={"camera_id": rc.camera_id, "event_id": ev_id})
            snap = self._annotate(job.frame, res, {d.candidate.track_uid} if d.candidate.track_uid else set(), rc,
                                  width=1280)
            threading.Thread(target=store_snapshot, args=(ev_id, rc.camera_id, d.candidate.ts, encode_jpeg(snap, 88)),
                             daemon=True).start()
            cfg = rc.analytics
            pre = self.ring.since(d.candidate.ts - cfg.evidence_pre_seconds)
            ej = EvidenceJob(ev_id, rc.camera_id, d.candidate.ts, d.candidate.ts + cfg.evidence_post_seconds,
                             cfg.evidence_fps, list(pre))
            with self._lock:
                self._evidence_jobs.append(ej)
            notify_async(ev_id)
        return highlight

    # ------------------------------------------------------------ health
    def snapshot_metrics(self) -> CameraMetrics:
        m = self.metrics
        m.input_fps = round(self._in_meter.rate, 2)
        m.inference_fps = round(self._inf_meter.rate, 2)
        m.inference_ms = round(sum(self._inf_ms) / len(self._inf_ms), 1) if self._inf_ms else 0.0
        m.end_to_end_ms = round(sum(self._lat_ms) / len(self._lat_ms), 1) if self._lat_ms else 0.0
        m.tracker_ms = round(sum(self._trk_ms) / len(self._trk_ms), 2) if self._trk_ms else 0.0
        m.queue_depth = (self.inference.queue_depth(self.rc.camera_id) if self.inference else 0) + self._results.qsize()
        if time.time() - self.last_frame_wall > 5 and self.status == "ONLINE":
            m.input_fps = 0.0
        return m

    def flush_evidence(self) -> None:
        """Encode partially captured clips (used when stopping)."""
        with self._lock:
            jobs, self._evidence_jobs = self._evidence_jobs, []
        for j in jobs:
            self.evidence.submit(j)

    def __repr__(self) -> str:
        return f"<CameraWorker {self.rc.camera_id} {self.status} {redact(self.rc.url)}>"
