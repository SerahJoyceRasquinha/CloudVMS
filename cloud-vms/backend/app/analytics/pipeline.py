"""Per-camera analytics pipeline: detections -> tracks -> rules -> incident decisions.

This module is pure Python (no database, no I/O) so the same code runs
inside the live worker, in offline evaluation scripts and in unit tests.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .classes import RIDEABLE, matches_object_types
from .event_engine import Decision, EngineConfig, EventEngine
from .geometry import intersection_over_first
from .reid import ReIdentifier, make_embedder
from .rules import LineRule, PolygonRule, ZoneSpec, build_rules
from .tracker import LOST, ByteTracker, TrackerConfig
from .datatypes import CrossingRecord, Detection, TrackView


@dataclass
class AnalyticsConfig:
    inference_fps: float = 5.0
    imgsz: int = 640
    detector_conf: float = 0.15  # low on purpose: ByteTrack uses weak boxes in its 2nd stage
    classes: list = field(default_factory=list)  # canonical classes/groups to keep; [] = all
    rider_suppression: bool = True
    rider_ioa: float = 0.3
    reid_enabled: bool = True
    reid_backend: str = "osnet"  # osnet (deep re-ID, default) | histogram (colour only, short-term)
    reid_window_seconds: float = 8.0  # short-term: occlusions, near where the object was lost
    reid_threshold: float = 0.65  # short-term similarity (histogram backend uses at least 0.8)
    reid_memory_seconds: float = 1800.0  # long-term: somebody who left the frame and came back
    reid_long_threshold: float = 0.75
    reid_samples: int = 3  # appearance samples collected before a track's identity is decided
    min_identity_hits: int = 3  # shorter-lived tracks are treated as noise and not counted
    tracker: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    evidence_pre_seconds: float = 5.0
    evidence_post_seconds: float = 8.0
    evidence_fps: float = 8.0
    evidence_width: int = 960
    preview_fps: float = 8.0
    preview_width: int = 960
    realtime: bool = True  # file sources: play at recorded speed (simulated live camera)
    loop: bool = True  # file sources: restart at the end
    anchor: str = "bottom_center"

    @classmethod
    def from_dict(cls, d: dict | None) -> "AnalyticsConfig":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class TrackSummary:
    uid: int
    root_uid: int
    cls: str
    group: str
    subtype: str
    first_ts: float
    last_ts: float
    hits: int
    mean_confidence: float
    trajectory: list  # [[t, x_norm, y_norm], ...]


@dataclass
class IdentityRecord:
    """A new unique object (person / vehicle) at this camera."""
    root_uid: int
    track_uid: int
    cls: str
    group: str
    subtype: str
    first_ts: float


@dataclass
class FrameResult:
    ts: float
    tracks: list[TrackView]
    lost: list[TrackView]
    crossings: list[CrossingRecord]
    decisions: list[Decision]
    terminated: list[TrackSummary]
    zone_occupancy: dict[int, int]
    line_totals: dict[int, dict]
    detections: list[Detection]
    tracker_ms: float = 0.0
    identities: list[IdentityRecord] = field(default_factory=list)  # newly counted unique objects
    remaps: list[tuple[int, int]] = field(default_factory=list)  # (track_uid, root_uid) re-identified late
    released: list[tuple] = field(default_factory=list)  # (root_uid, last_ts, prototypes) to remember


def suppress_riders(dets: list[Detection], min_ioa: float) -> list[Detection]:
    """Drop person boxes that sit on a bicycle / two-wheeler (they're riders, not pedestrians)."""
    rides = [d for d in dets if d.cls in RIDEABLE]
    if not rides:
        return dets
    out = []
    for d in dets:
        if d.cls == "person":
            ax, ay = (d.bbox[0] + d.bbox[2]) / 2, d.bbox[3]
            riding = False
            for r in rides:
                x1, y1, x2, y2 = r.bbox
                pad = 0.1 * (x2 - x1)
                if x1 - pad <= ax <= x2 + pad and y1 <= ay <= y2 + 0.1 * (y2 - y1) \
                        and intersection_over_first(d.bbox, r.bbox) >= min_ioa:
                    riding = True
                    break
            if riding:
                continue
        out.append(d)
    return out


class CameraAnalytics:
    def __init__(self, camera_id: int, config: AnalyticsConfig, zones: list[ZoneSpec],
                 local_time=None, device: str = "cpu", start_uid: int | None = None):
        self.camera_id = camera_id
        self.cfg = config
        self.local_time = local_time or (lambda ts: datetime.fromtimestamp(ts))
        # IDs are unique across restarts (microsecond start), so counts stored by root id never collide
        self.tracker = ByteTracker(TrackerConfig.from_dict(config.tracker), start_uid=start_uid or _session_uid())
        self.engine = EventEngine(camera_id, EngineConfig.from_dict(config.events))
        self.reid: ReIdentifier | None = None
        if config.reid_enabled:
            emb = make_embedder(config.reid_backend, device)
            short = config.reid_threshold if getattr(emb, "deep", False) else max(config.reid_threshold, 0.8)
            self.reid = ReIdentifier(emb, config.reid_window_seconds, short,
                                     memory_seconds=config.reid_memory_seconds,
                                     long_threshold=config.reid_long_threshold)
        self._pending: dict[int, list[np.ndarray]] = {}  # confirmed tracks whose identity isn't decided yet
        self._decided: set[int] = set()
        self.zones = zones
        self.size: tuple[int, int] | None = None
        self.lines: list[LineRule] = []
        self.polys: list[PolygonRule] = []
        self._frame_i = 0

    # ------------------------------------------------------------ configuration
    def set_zones(self, zones: list[ZoneSpec]) -> None:
        self.zones = zones
        if self.size:
            self.lines, self.polys = build_rules(zones, *self.size)

    def _ensure_size(self, w: int, h: int) -> None:
        if self.size != (w, h):
            self.size = (w, h)
            self.lines, self.polys = build_rules(self.zones, w, h)

    # ------------------------------------------------------------ main step
    def process(self, ts: float, frame: np.ndarray | None, detections: list[Detection],
                frame_size: tuple[int, int] | None = None) -> FrameResult:
        t0 = time.perf_counter()
        if frame is not None:
            h, w = frame.shape[:2]
        else:
            w, h = frame_size or (1920, 1080)
        self._ensure_size(w, h)
        self._frame_i += 1
        # the detector is shared by all cameras (fixed low threshold); apply this camera's own setting
        dets = [d for d in detections if d.confidence >= self.cfg.detector_conf]
        if self.cfg.classes:
            dets = [d for d in dets if matches_object_types(self.cfg.classes, d.cls)]
        if self.cfg.rider_suppression:
            dets = suppress_riders(dets, self.cfg.rider_ioa)

        out = self.tracker.update(dets, ts)
        diag = math.hypot(w, h)

        # identity: every confirmed track is either re-identified as somebody seen before or counted once
        identities, remaps, released = self._identify(out, frame, ts, diag)

        crossings: list[CrossingRecord] = []
        candidates = []
        local_dt = self.local_time(ts)
        for tv in out.tracks:
            for lr in self.lines:
                c = lr.update(tv, ts)
                if c:
                    crossings.append(c)
            for pr in self.polys:
                candidates.extend(pr.update(tv, ts, local_dt))
        for pr in self.polys:
            candidates.extend(pr.crowd_check(ts, out.tracks[0] if out.tracks else None))

        summaries: list[TrackSummary] = []
        for tr in out.terminated:
            for rule in (*self.lines, *self.polys):
                rule.forget(tr.uid)
            if tr.uid in self._pending:  # ended before its identity was decided
                rec = self._decide(tr, ts, diag, set(), remaps, final=True)
                if rec:
                    identities.append(rec)
            if self.reid is not None and not tr.retired:  # a retired track's identity lives on elsewhere
                root = tr.root_uid or tr.uid
                self.reid.release(root, tr.last_ts, tr.last_bbox)
                snap = self.reid.snapshot(root)
                if snap:
                    released.append((root, *snap))
            self._decided.discard(tr.uid)
            summaries.append(self._summary(tr, w, h))

        decisions = [self.engine.decide(c) for c in candidates]
        if self._frame_i % 100 == 0:
            self.engine.prune(ts)
            if self.reid is not None:
                self.reid.prune(ts)
        return FrameResult(
            ts=ts, tracks=out.tracks, lost=out.lost, crossings=crossings, decisions=decisions,
            terminated=summaries,
            zone_occupancy={p.zone.id: p.occupancy for p in self.polys},
            line_totals={lr.zone.id: dict(lr.totals) for lr in self.lines},
            detections=dets, tracker_ms=(time.perf_counter() - t0) * 1000,
            identities=identities, remaps=remaps, released=released)

    # ------------------------------------------------------------ identity
    def _identify(self, out, frame, ts: float, diag: float):
        identities: list[IdentityRecord] = []
        remaps: list[tuple[int, int]] = []
        remember: list[tuple] = []  # appearance of freshly decided identities (saved at once: survives a crash)
        for uid in out.confirmed_now:
            if uid not in self._decided:
                self._pending.setdefault(uid, [])
        visible_roots = {t.root_uid for t in out.tracks if t.uid in self._decided}
        for i, tv in enumerate(out.tracks):
            tr = self.tracker.get(tv.uid)
            if tr is None:
                continue
            pending = tr.uid in self._pending
            emb = None
            # sample appearance every frame while undecided, then now and then to learn other views
            if self.reid is not None and frame is not None and (pending or tr.hits % 5 == 0):
                emb = self.reid.embedder.embed(frame, tv.bbox)
            if pending:
                if emb is not None:
                    self._pending[tr.uid].append(emb)
                enough = len(self._pending[tr.uid]) >= max(1, self.cfg.reid_samples)
                if self.reid is None or enough or ts - tr.first_ts >= 2.0:
                    rec = self._decide(tr, ts, diag, visible_roots, remaps)
                    if rec:
                        identities.append(rec)
                    visible_roots.add(tr.root_uid or tr.uid)
                    snap = self.reid.snapshot(tr.root_uid) if self.reid is not None else None
                    if snap:
                        remember.append((tr.root_uid, *snap))
                    out.tracks[i] = tr.view(True)
            elif self.reid is not None and emb is not None:
                self.reid.observe(tr.root_uid or tr.uid, tr.group, ts, tv.bbox, emb)
        return identities, remaps, remember

    def _decide(self, tr, ts: float, diag: float, busy_roots: set[int], remaps: list, final: bool = False):
        """Link a track to an earlier identity, or register it as a new unique object."""
        embs = self._pending.pop(tr.uid, [])
        self._decided.add(tr.uid)
        mean = _mean_embedding(embs)
        root = None
        if self.reid is not None and mean is not None:
            start = (tr.trajectory[0][1], tr.trajectory[0][2]) if tr.trajectory else _foot(tr.last_bbox)
            root = self.reid.match(tr.group, tr.first_ts, start, mean, diag, exclude_roots=busy_roots)
        new = None
        if root is not None and root != tr.uid:
            old = tr.root_uid or tr.uid
            tr.root_uid = root
            remaps.append((tr.uid, root))
            for lr in self.lines:
                lr.remap(old, root)
            self._retire_lost(root, tr.uid)
        else:
            tr.root_uid = tr.uid
            if final and tr.hits < self.cfg.min_identity_hits:
                return None  # a flicker, not an object: not counted
            new = IdentityRecord(tr.uid, tr.uid, tr.cls, tr.group, tr.subtype, tr.first_ts)
        if self.reid is not None:
            for e in embs:
                self.reid.observe(tr.root_uid, tr.group, ts, tr.last_bbox, e)
            if not embs:
                self.reid.observe(tr.root_uid, tr.group, ts, tr.last_bbox, None)
        return new

    def _retire_lost(self, root: int, keep_uid: int) -> None:
        """A LOST track whose identity continues in a new track should end now."""
        for other in self.tracker.tracks:
            if other.uid != keep_uid and other.state == LOST and (other.root_uid or other.uid) == root:
                other.retired = True

    def flush(self) -> list[TrackSummary]:
        w, h = self.size or (1920, 1080)
        return [self._summary(t, w, h) for t in self.tracker.flush()]

    @staticmethod
    def _summary(tr, w: int, h: int) -> TrackSummary:
        traj = list(tr.trajectory)
        step = max(1, len(traj) // 50)
        pts = [[round(t, 2), round(x / w, 4), round(y / h, 4)] for t, x, y in traj[::step]]
        last_ts = tr.last_ts
        return TrackSummary(tr.uid, tr.root_uid or tr.uid, tr.cls, tr.group, tr.subtype, tr.first_ts,
                            last_ts, tr.hits, round(tr.mean_conf, 3), pts)


def _mean_embedding(embs: list[np.ndarray]) -> np.ndarray | None:
    if not embs:
        return None
    v = np.mean(np.stack(embs), axis=0)
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def _foot(bbox) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2, y2)


_last_session_uid = 0


def _session_uid() -> int:
    """First tracker id of a new pipeline: the clock in microseconds, and at least a million past the
    previous pipeline in this process, so ids stay unique across restarts and settings reloads."""
    global _last_session_uid
    _last_session_uid = max(int(time.time() * 1_000_000), _last_session_uid + 1_000_000)
    return _last_session_uid
