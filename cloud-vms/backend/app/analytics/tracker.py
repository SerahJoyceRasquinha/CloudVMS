"""ByteTrack-style multi-object tracker (Zhang et al., ECCV 2022).

Key idea kept from ByteTrack: low-confidence detections are *not* thrown
away; after high-confidence boxes are matched, the leftovers are used to keep
existing tracks alive through partial occlusion.

Differences for this project:
* time-based constant-velocity prediction (alpha-beta filter) because the
  inference rate is configurable and frames arrive at irregular intervals;
* association is gated by object group (a person box never continues a
  vehicle track);
* an explicit lifecycle NEW -> ACTIVE -> LOST -> TERMINATED;
* a centre-distance fallback helps at low inference FPS when boxes of the
  same object no longer overlap between samples.
"""
from __future__ import annotations

import itertools
import math
from collections import Counter, deque
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from .geometry import iou
from .datatypes import Detection, TrackView

NEW, ACTIVE, LOST, TERMINATED = "NEW", "ACTIVE", "LOST", "TERMINATED"


@dataclass
class TrackerConfig:
    high_thresh: float = 0.45
    low_thresh: float = 0.1
    new_track_thresh: float = 0.5
    match_thresh: float = 0.8  # max cost (1 - similarity) for the first association
    second_match_thresh: float = 0.6
    unconfirmed_match_thresh: float = 0.7
    min_hits: int = 2  # observations before a track is confirmed (ACTIVE)
    max_lost_seconds: float = 2.0
    distance_fallback: bool = True
    alpha: float = 0.7
    beta: float = 0.2
    history: int = 300

    @classmethod
    def from_dict(cls, d: dict | None) -> "TrackerConfig":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class _Track:
    uid: int
    group: str
    cx: float
    cy: float
    w: float
    h: float
    first_ts: float
    last_ts: float
    vx: float = 0.0
    vy: float = 0.0
    hits: int = 1
    state: str = NEW
    conf_last: float = 0.0
    conf_sum: float = 0.0
    root_uid: int = 0
    cls_votes: Counter = field(default_factory=Counter)
    subtype_votes: Counter = field(default_factory=Counter)
    trajectory: deque = field(default_factory=lambda: deque(maxlen=300))
    last_bbox: tuple = (0, 0, 0, 0)
    appearance: object = None  # optional re-ID embedding
    retired: bool = False  # its identity continues in another track: end it at the next update

    def predict(self, ts: float) -> tuple[float, float, float, float]:
        dt = max(0.0, min(ts - self.last_ts, 1.5))  # do not extrapolate too far
        cx, cy = self.cx + self.vx * dt, self.cy + self.vy * dt
        return (cx - self.w / 2, cy - self.h / 2, cx + self.w / 2, cy + self.h / 2)

    def update(self, det: Detection, ts: float, alpha: float, beta: float) -> None:
        x1, y1, x2, y2 = det.bbox
        mcx, mcy, mw, mh = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
        dt = max(ts - self.last_ts, 1e-3)
        pcx, pcy = self.cx + self.vx * dt, self.cy + self.vy * dt
        rx, ry = mcx - pcx, mcy - pcy
        self.cx, self.cy = pcx + alpha * rx, pcy + alpha * ry
        self.vx += beta * rx / dt
        self.vy += beta * ry / dt
        self.w += alpha * (mw - self.w)
        self.h += alpha * (mh - self.h)
        self.last_ts = ts
        self.hits += 1
        self.conf_last = det.confidence
        self.conf_sum += det.confidence
        self.cls_votes[det.cls] += det.confidence
        if det.subtype:
            self.subtype_votes[det.subtype] += det.confidence
        self.last_bbox = det.bbox

    @property
    def cls(self) -> str:
        return self.cls_votes.most_common(1)[0][0] if self.cls_votes else "unknown"

    @property
    def subtype(self) -> str:
        return self.subtype_votes.most_common(1)[0][0] if self.subtype_votes else ""

    @property
    def mean_conf(self) -> float:
        return self.conf_sum / max(1, self.hits)

    def view(self, matched: bool) -> TrackView:
        return TrackView(uid=self.uid, root_uid=self.root_uid or self.uid, cls=self.cls, group=self.group,
                         subtype=self.subtype, bbox=tuple(self.last_bbox), confidence=self.conf_last,
                         mean_confidence=self.mean_conf, state=self.state, hits=self.hits,
                         first_ts=self.first_ts, last_ts=self.last_ts, matched=matched)


@dataclass
class TrackerOutput:
    tracks: list[TrackView]  # confirmed tracks observed in this frame
    lost: list[TrackView]  # confirmed tracks temporarily missing
    confirmed_now: list[int]  # uids that became ACTIVE in this frame
    terminated: list[_Track]  # finished tracks (for summaries / zone exits)


class ByteTracker:
    def __init__(self, config: TrackerConfig | None = None, start_uid: int = 1):
        self.cfg = config or TrackerConfig()
        self._ids = itertools.count(start_uid)
        self.tracks: list[_Track] = []

    # --------------------------------------------------------------- helpers
    def _similarity(self, pred: tuple, det: Detection) -> float:
        s = iou(pred, det.bbox)
        if self.cfg.distance_fallback and s < 0.2:
            pcx, pcy = (pred[0] + pred[2]) / 2, (pred[1] + pred[3]) / 2
            dcx, dcy = (det.bbox[0] + det.bbox[2]) / 2, (det.bbox[1] + det.bbox[3]) / 2
            diag = math.hypot(pred[2] - pred[0], pred[3] - pred[1]) or 1.0
            area_ratio = ((det.bbox[2] - det.bbox[0]) * (det.bbox[3] - det.bbox[1]) + 1e-6) / \
                         ((pred[2] - pred[0]) * (pred[3] - pred[1]) + 1e-6)
            if 0.4 < area_ratio < 2.5:  # similar size
                dsim = max(0.0, 1.0 - math.hypot(dcx - pcx, dcy - pcy) / (0.8 * diag)) * 0.5
                s = max(s, dsim)
        return s

    def _match(self, tracks: list[_Track], dets: list[Detection], ts: float, max_cost: float):
        if not tracks or not dets:
            return [], list(range(len(tracks))), list(range(len(dets)))
        cost = np.full((len(tracks), len(dets)), 1e6, dtype=np.float64)
        for i, t in enumerate(tracks):
            pred = t.predict(ts)
            for j, d in enumerate(dets):
                if d.group != t.group:
                    continue
                c = 1.0 - self._similarity(pred, d)
                if c <= max_cost:
                    cost[i, j] = c
        rows, cols = linear_sum_assignment(cost)
        matches, used_t, used_d = [], set(), set()
        for r, c in zip(rows, cols):
            if cost[r, c] <= max_cost:
                matches.append((r, c))
                used_t.add(r)
                used_d.add(c)
        return (matches, [i for i in range(len(tracks)) if i not in used_t],
                [j for j in range(len(dets)) if j not in used_d])

    # --------------------------------------------------------------- main
    def update(self, detections: list[Detection], ts: float) -> TrackerOutput:
        cfg = self.cfg
        high = [d for d in detections if d.confidence >= cfg.high_thresh]
        low = [d for d in detections if cfg.low_thresh <= d.confidence < cfg.high_thresh]
        active = [t for t in self.tracks if t.state == ACTIVE]
        lost = [t for t in self.tracks if t.state == LOST and not t.retired]
        unconfirmed = [t for t in self.tracks if t.state == NEW]
        matched_ids: set[int] = set()
        confirmed_now: list[int] = []

        def apply(track: _Track, det: Detection) -> None:
            track.update(det, ts, cfg.alpha, cfg.beta)
            x1, y1, x2, y2 = det.bbox
            track.trajectory.append((ts, (x1 + x2) / 2, y2))
            if track.state in (NEW, LOST) and track.hits >= cfg.min_hits:
                if track.state == NEW:
                    confirmed_now.append(track.uid)
                track.state = ACTIVE
            matched_ids.add(track.uid)

        # 1) high-confidence detections vs active + lost tracks
        pool = active + lost
        m1, un_t1, un_d1 = self._match(pool, high, ts, cfg.match_thresh)
        for ti, di in m1:
            apply(pool[ti], high[di])
        remaining_high = [high[j] for j in un_d1]
        # 2) low-confidence detections vs still-unmatched *active* tracks (ByteTrack's BYTE step)
        remaining_active = [pool[i] for i in un_t1 if pool[i].state == ACTIVE]
        m2, _, _ = self._match(remaining_active, low, ts, cfg.second_match_thresh)
        for ti, di in m2:
            apply(remaining_active[ti], low[di])
        # 3) unconfirmed tracks vs leftover high detections
        m3, un_t3, un_d3 = self._match(unconfirmed, remaining_high, ts, cfg.unconfirmed_match_thresh)
        for ti, di in m3:
            apply(unconfirmed[ti], remaining_high[di])
        leftovers = [remaining_high[j] for j in un_d3]

        # lifecycle for unmatched tracks
        terminated: list[_Track] = []
        survivors: list[_Track] = []
        for t in self.tracks:
            if t.uid in matched_ids:
                survivors.append(t)
                continue
            if t.state == NEW:
                continue  # unconfirmed and missed -> drop silently (likely a false positive)
            if t.state == ACTIVE:
                t.state = LOST
            if t.retired or ts - t.last_ts > cfg.max_lost_seconds:
                t.state = TERMINATED
                terminated.append(t)
            else:
                survivors.append(t)

        # 4) start new tracks
        for d in leftovers:
            if d.confidence < cfg.new_track_thresh:
                continue
            x1, y1, x2, y2 = d.bbox
            t = _Track(uid=next(self._ids), group=d.group, cx=(x1 + x2) / 2, cy=(y1 + y2) / 2,
                       w=x2 - x1, h=y2 - y1, first_ts=ts, last_ts=ts, conf_last=d.confidence,
                       conf_sum=d.confidence, last_bbox=d.bbox)
            t.trajectory = deque(maxlen=cfg.history)
            t.trajectory.append((ts, (x1 + x2) / 2, y2))
            t.cls_votes[d.cls] += d.confidence
            if d.subtype:
                t.subtype_votes[d.subtype] += d.confidence
            t.root_uid = t.uid
            if cfg.min_hits <= 1:
                t.state = ACTIVE
                confirmed_now.append(t.uid)
            survivors.append(t)
            matched_ids.add(t.uid)
        self.tracks = survivors

        views = [t.view(True) for t in self.tracks if t.state == ACTIVE and t.uid in matched_ids]
        lost_views = [t.view(False) for t in self.tracks if t.state == LOST]
        return TrackerOutput(tracks=views, lost=lost_views, confirmed_now=confirmed_now,
                             terminated=terminated)

    def get(self, uid: int) -> _Track | None:
        for t in self.tracks:
            if t.uid == uid:
                return t
        return None

    def flush(self) -> list[_Track]:
        """Terminate everything (stream stopped)."""
        out = [t for t in self.tracks if t.state in (ACTIVE, LOST)]
        for t in out:
            t.state = TERMINATED
        self.tracks = []
        return out
