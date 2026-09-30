"""Worker supervisor: reconciles running camera pipelines with the desired state in the database.

The API never runs video work itself. It only changes *desired state* (camera
enabled, recording on/off, zones, analytics settings, active models) in the
database. A supervisor — embedded in the API process for a single-machine
setup, or started separately with ``python -m app.workers.run`` on one or more
worker machines — polls that state and starts, stops or hot-reloads camera
pipelines for the cameras in its ``worker_group``.
"""
from __future__ import annotations

import logging
import threading
import time

import psutil
from sqlalchemy import select

from ..analytics.detector import Detector
from ..core.config import get_settings
from ..core.timeutil import from_epoch, utcnow
from ..db import session_scope
from ..models import Camera, CameraHealth
from ..services.audit import get_setting
from ..services.camera_service import runtime_config
from ..services.event_service import auto_resolve, create_system_event
from ..services.model_registry import active_specs
from ..services.retention import run_retention
from .camera_worker import CameraWorker
from .dbwriter import DbWriter
from .evidence import EvidenceWriter
from .framebus import get_frame_bus
from .inference import InferenceService
from .recorder import Recorder

log = logging.getLogger("vms.supervisor")


class Supervisor:
    def __init__(self, group: str | None = None, detector_factory=None):
        s = get_settings()
        self.group = group or s.worker_group
        self.detector_factory = detector_factory  # tests inject a FakeDetector here
        self.workers: dict[int, CameraWorker] = {}
        self.recorders: dict[int, Recorder] = {}
        self._stream_sig: dict[int, tuple] = {}
        self._rec_sig: dict[int, tuple] = {}
        self._last_status: dict[int, str] = {}
        self._offline_keys: dict[int, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()  # reconcile loop vs. API requests (camera deletion)
        self.detector = None
        self.models_version = None
        self.model_version_str = ""
        self.detector_error = ""
        self._detector_retry_at = 0.0
        self.inference: InferenceService | None = None
        self.evidence = EvidenceWriter()
        self.db_writer = DbWriter()
        self.bus = get_frame_bus()
        self.proc = psutil.Process()
        self.proc.cpu_percent(None)
        self.started_at = time.time()
        self.last_error = ""

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "Supervisor":
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        for cid in list(self.workers):
            self._stop_worker(cid)
        for cid in list(self.recorders):
            self._stop_recorder(cid)
        if self.inference:
            self.inference.stop()
        self.evidence.shutdown()
        self.db_writer.stop()

    def _loop(self) -> None:
        s = get_settings()
        last_health = last_retention = 0.0
        while not self._stop.is_set():
            try:
                with self._lock:
                    self.reconcile()
                now = time.monotonic()
                if now - last_health >= s.health_interval_seconds:
                    last_health = now
                    self.persist_health()
                if now - last_retention >= 3600:
                    last_retention = now
                    run_retention()
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("supervisor tick failed")
            self._stop.wait(s.supervisor_interval_seconds)

    # ------------------------------------------------------------ detector
    def _try_ensure_detector(self) -> None:
        """A detector that fails to load (missing weights, no internet) must not stop the
        supervisor: cameras still stream and record, and loading is retried once a minute."""
        now = time.monotonic()
        if now < self._detector_retry_at:
            return
        try:
            self.ensure_detector()
            self.detector_error = ""
        except Exception as exc:
            self._detector_retry_at = now + 60
            self.detector_error = f"detector failed to load: {exc}"[:300]
            log.exception("detector failed to load; running cameras without AI and retrying in 60 s")

    def ensure_detector(self) -> None:
        with session_scope() as db:
            version = (get_setting(db, "models_version"), get_setting(db, "detector_profile"))
            if version == self.models_version and self.detector is not None:
                return
            if self.detector_factory:
                det = self.detector_factory()
            else:
                profile, specs = active_specs(db)
                s = get_settings()
                det = Detector(specs, s.device, imgsz=640).load()
        self.detector = det
        self.models_version = version
        self.model_version_str = det.version
        if self.inference is None:
            s = get_settings()
            self.inference = InferenceService(det, s.inference_batch_size, threads=s.inference_threads)
        else:
            self.inference.swap_detector(det)

    # ------------------------------------------------------------ reconcile
    def reconcile(self) -> None:
        with session_scope() as db:
            cams = list(db.scalars(select(Camera).where(Camera.worker_group == self.group)))
            wanted = {}
            for cam in cams:
                rc = runtime_config(db, cam)
                rec_cfg = dict(cam.recording_config or {})
                wanted[cam.id] = (cam.enabled, cam.enabled and cam.recording_enabled, rc, rec_cfg)
        if any(v[0] and v[2].analytics_enabled for v in wanted.values()):
            self._try_ensure_detector()

        for cid in list(self.workers):
            if cid not in wanted or not wanted[cid][0]:
                self._stop_worker(cid)
        for cid in list(self.recorders):
            if cid not in wanted or not wanted[cid][1]:
                self._stop_recorder(cid)

        for cid, (run, record, rc, rec_cfg) in wanted.items():
            sig = (rc.stream_type, rc.url, rc.analytics.realtime, rc.analytics.loop)
            if run:
                w = self.workers.get(cid)
                if w is None or not w.alive:
                    self._start_worker(rc, sig)
                elif self._stream_sig.get(cid) != sig:
                    log.info("camera %s: stream settings changed, restarting", cid)
                    self._stop_worker(cid)
                    self._start_worker(rc, sig)
                elif w.inference is None and self.inference is not None:
                    w.inference = self.inference  # detector became available after the worker started
                elif (w.rc.analytics.to_dict() != rc.analytics.to_dict()
                      or w.rc.zones_signature != rc.zones_signature
                      or w.rc.analytics_enabled != rc.analytics_enabled or w.rc.name != rc.name):
                    w.inference = self.inference
                    w.reload(rc)
            rsig = (rc.stream_type, rc.url, tuple(sorted(rec_cfg.items())), rc.analytics.loop)
            if record:
                r = self.recorders.get(cid)
                if r is None or self._rec_sig.get(cid) != rsig:
                    if r:
                        self._stop_recorder(cid)
                    r = Recorder(cid, rc.stream_type, rc.url, rec_cfg, loop_files=rc.analytics.loop)
                    self.recorders[cid] = r
                    self._rec_sig[cid] = rsig
                    r.start()
                failure = r.poll()
                r.maybe_restart()
                if failure:
                    with session_scope() as db:
                        create_system_event(db, cid, "RECORDING_FAILURE", f"Recording failing: {failure[:120]}",
                                            "high", f"{cid}:RECORDING_FAILURE:{int(time.time() // 3600)}")

    def forget_camera(self, cid: int) -> None:
        """Stop a camera's pipeline and recorder right away and write out what they still hold,
        so nothing arrives in the database after its data has been purged."""
        with self._lock:
            w = self.workers.pop(cid, None)
            self._stream_sig.pop(cid, None)
            if w:
                w.stop()
            self._stop_recorder(cid)
            self._last_status.pop(cid, None)
            self._offline_keys.pop(cid, None)
        self.db_writer.flush()

    def _start_worker(self, rc, sig) -> None:
        w = CameraWorker(rc, self.inference, self.evidence,
                         self.db_writer, self.bus, lambda: self.model_version_str,
                         device=getattr(self.detector, "device", "cpu") or "cpu")
        self.workers[rc.camera_id] = w
        self._stream_sig[rc.camera_id] = sig
        w.start()
        log.info("camera %s: pipeline started", rc.camera_id)

    def _stop_worker(self, cid: int) -> None:
        w = self.workers.pop(cid, None)
        if w:
            w.stop()
            self._set_status(cid, "DISABLED", "")
            log.info("camera %s: pipeline stopped", cid)

    def _stop_recorder(self, cid: int) -> None:
        r = self.recorders.pop(cid, None)
        self._rec_sig.pop(cid, None)
        if r:
            r.stop()

    # ------------------------------------------------------------ health
    def _set_status(self, cid: int, status: str, message: str) -> None:
        with session_scope() as db:
            cam = db.get(Camera, cid)
            if cam:
                cam.status, cam.status_message = status, message[:300]

    def persist_health(self) -> None:
        cpu = self.proc.cpu_percent(None)
        mem = self.proc.memory_info().rss / 1e6
        gpu = 0.0
        try:
            import torch
            if torch.cuda.is_available():
                gpu = torch.cuda.memory_allocated() / 1e6
        except Exception:
            pass
        with session_scope() as db:
            for cid, w in list(self.workers.items()):
                m = w.snapshot_metrics()
                cam = db.get(Camera, cid)
                if cam is None:
                    continue
                status = w.status if w.status != "STARTING" else "REGISTERED"
                prev = self._last_status.get(cid)
                cam.status, cam.status_message = status, w.status_message[:300]
                if w.last_frame_wall:
                    cam.last_seen_at = from_epoch(w.last_frame_wall)
                db.add(CameraHealth(camera_id=cid, ts=utcnow(), status=status, input_fps=m.input_fps,
                                    inference_fps=m.inference_fps, frames_dropped=m.frames_dropped,
                                    reconnects=m.reconnects, inference_ms=m.inference_ms,
                                    end_to_end_ms=m.end_to_end_ms, tracker_ms=m.tracker_ms,
                                    active_tracks=m.active_tracks, queue_depth=m.queue_depth,
                                    cpu_percent=cpu, mem_mb=round(mem, 1), gpu_mem_mb=round(gpu, 1)))
                # CAMERA_OFFLINE incidents on ONLINE -> OFFLINE transitions, auto-resolved on recovery
                if status == "OFFLINE" and prev == "ONLINE" and w.status_message != "end of video file":
                    key = f"{cid}:CAMERA_OFFLINE:{int(time.time())}"
                    create_system_event(db, cid, "CAMERA_OFFLINE", f"Camera '{cam.name}' went offline",
                                        "high", key, {"message": w.status_message})
                    self._offline_keys[cid] = key
                elif status == "ONLINE" and cid in self._offline_keys:
                    auto_resolve(db, self._offline_keys.pop(cid), "camera recovered automatically")
                self._last_status[cid] = status

    def status(self) -> dict:
        return {
            "group": self.group,
            "uptime_s": round(time.time() - self.started_at),
            "detector": self.model_version_str,
            "detector_error": self.detector_error,
            "device": getattr(self.detector, "device", None),
            "inference": {
                "processed": self.inference.processed if self.inference else 0,
                "batches": self.inference.batches if self.inference else 0,
                "last_batch_ms": round(self.inference.last_batch_ms, 1) if self.inference else 0,
                "queue_depth": self.inference.queue_depth() if self.inference else 0,
            },
            "cameras": {cid: {"status": w.status, "message": w.status_message, **vars(w.snapshot_metrics())}
                        for cid, w in self.workers.items()},
            "recorders": {cid: {"running": r.proc is not None, "segments": r.segments_written,
                                "error": r.last_error} for cid, r in self.recorders.items()},
            "db_writer_dropped": self.db_writer.dropped,
            "last_error": self.last_error,
        }


_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor | None:
    return _supervisor


def start_supervisor(detector_factory=None) -> Supervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = Supervisor(detector_factory=detector_factory).start()
    return _supervisor


def stop_supervisor() -> None:
    global _supervisor
    if _supervisor is not None:
        _supervisor.stop()
        _supervisor = None
