"""Spatial / temporal rules evaluated on tracked objects.

* ``LineRule``      – counts objects crossing an entry/exit line (unique per root track)
* ``PolygonRule``   – zone occupancy + intrusion / restricted-access / loitering / crowd

Intrusion follows the definition in Lohani et al., *Perimeter Intrusion
Detection by Video Surveillance: A Survey*, Sensors 2022: an object of a
non-authorised class inside the protected area during the protected time.
Here "non-authorised class" and "protected time" are expressed through the
zone's PolicySet. Every rule is per camera; nothing is shared across cameras.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .classes import matches_object_types
from .geometry import point_in_polygon, segments_intersect, signed_side, to_pixels
from .policy import PolicySet, PolicySpec
from .datatypes import CrossingRecord, EventCandidate, TrackView

EVENT_TYPE_FOR_ZONE = {
    "intrusion": "INTRUSION_DETECTED",
    "restricted": "RESTRICTED_AREA_ACCESS",
}
RULE_VERSION = "rules-1.0"


@dataclass
class ZoneSpec:
    id: int
    camera_id: int
    name: str
    zone_type: str  # intrusion | restricted | authorized | monitoring | counting | line
    shape: str  # polygon | line
    points: list  # normalised
    severity: str = "medium"
    default_action: str = "allow"
    config: dict = field(default_factory=dict)
    policies: list[PolicySpec] = field(default_factory=list)
    enabled: bool = True
    version: int = 1

    def policy_set(self) -> PolicySet:
        return PolicySet(self.default_action, self.severity, self.policies)


# ============================================================ line counting
@dataclass
class _LineState:
    side: int = 0  # committed side: +1 / -1 / 0 unknown
    last_point: tuple | None = None


class LineRule:
    def __init__(self, zone: ZoneSpec, width: int, height: int):
        self.zone = zone
        cfg = zone.config or {}
        self.a, self.b = to_pixels(zone.points, width, height)
        self.deadband = float(cfg.get("deadband_frac", 0.012)) * math.hypot(width, height)
        # "positive" side = the side the arrow drawn on the line points to (see annotate.draw_zones)
        in_side = str(cfg.get("in_side", "positive"))
        self.in_sign = -1 if in_side in ("negative", "right") else 1
        self.count_classes = cfg.get("count_classes") or []
        self.count_once = bool(cfg.get("count_once", True))
        self.anchor = cfg.get("anchor", "bottom_center")
        self.state: dict[int, _LineState] = {}
        self.counted: dict[str, set[int]] = {"in": set(), "out": set()}
        self.totals = {"in": 0, "out": 0}

    def update(self, t: TrackView, ts: float) -> CrossingRecord | None:
        if self.count_classes and not matches_object_types(self.count_classes, t.cls):
            return None
        p = t.anchor(self.anchor)
        st = self.state.setdefault(t.uid, _LineState())
        d = signed_side(p, self.a, self.b)
        if abs(d) < self.deadband:  # hysteresis band: don't commit near the line
            return None
        side = 1 if d > 0 else -1
        if st.side == 0:
            st.side, st.last_point = side, p
            return None
        if side == st.side:
            st.last_point = p
            return None
        # side flipped: only a crossing if the path actually passes the segment
        prev = st.last_point or p
        st.side, st.last_point = side, p
        if not segments_intersect(prev, p, self.a, self.b):
            return None
        direction = "in" if side == self.in_sign else "out"
        if self.count_once and t.root_uid in self.counted[direction]:
            return None
        self.counted[direction].add(t.root_uid)
        self.totals[direction] += 1
        return CrossingRecord(self.zone.id, t.uid, t.root_uid, t.cls, t.group, t.subtype, direction, ts)

    def forget(self, uid: int) -> None:
        self.state.pop(uid, None)

    def remap(self, old_root: int, new_root: int) -> None:
        """A track was re-identified as an earlier object after it had already crossed."""
        for direction, roots in self.counted.items():
            if old_root in roots:
                roots.discard(old_root)
                if new_root in roots and self.count_once:
                    self.totals[direction] -= 1  # the same object was counted twice
                roots.add(new_root)


# ============================================================ polygon zones
@dataclass
class _ZoneTrackState:
    inside: bool = False
    inside_streak: int = 0
    outside_streak: int = 0
    first_inside_ts: float | None = None
    enter_ts: float | None = None
    seen_outside: bool = False
    alerted: bool = False
    loiter_alerted: bool = False
    start_point: tuple | None = None


class PolygonRule:
    def __init__(self, zone: ZoneSpec, width: int, height: int):
        self.zone = zone
        cfg = zone.config or {}
        self.poly = to_pixels(zone.points, width, height)
        self.diag = math.hypot(width, height)
        self.min_persistence = int(cfg.get("min_persistence", 2))
        self.min_seconds = float(cfg.get("min_seconds", 0.4))
        self.exit_persistence = int(cfg.get("exit_persistence", 3))
        default_transition = zone.zone_type == "intrusion"
        self.require_transition = bool(cfg.get("require_entry_transition", default_transition))
        self.require_motion = bool(cfg.get("require_motion", False))
        self.motion_min = float(cfg.get("motion_min_frac", 0.01)) * self.diag
        self.min_track_conf = float(cfg.get("min_track_confidence", 0.3))
        self.object_types = cfg.get("object_types") or []
        self.dwell_seconds = float(cfg.get("dwell_seconds", 0) or 0)
        self.crowd_threshold = int(cfg.get("crowd_threshold", 0) or 0)
        self.crowd_seconds = float(cfg.get("crowd_seconds", 5))
        self.anchor = cfg.get("anchor", "bottom_center")
        self.policies = zone.policy_set()
        self.event_type = EVENT_TYPE_FOR_ZONE.get(zone.zone_type)
        self.state: dict[int, _ZoneTrackState] = {}
        self._crowd_since: float | None = None
        self._crowd_alerted = False

    @property
    def occupancy(self) -> int:
        return sum(1 for s in self.state.values() if s.inside)

    def _candidate(self, etype: str, t: TrackView, ts: float, severity: str, title: str,
                   session: str, policy_id=None, meta=None) -> EventCandidate:
        return EventCandidate(event_type=etype, zone_id=self.zone.id, track_uid=t.uid, root_uid=t.root_uid,
                              cls=t.cls, ts=ts, confidence=t.mean_confidence, bbox=t.bbox, severity=severity,
                              title=title, session_key=session, policy_id=policy_id, metadata=meta or {})

    def update(self, t: TrackView, ts: float, local_dt: datetime) -> list[EventCandidate]:
        if self.object_types and not matches_object_types(self.object_types, t.cls):
            return []
        out: list[EventCandidate] = []
        p = t.anchor(self.anchor)
        st = self.state.setdefault(t.uid, _ZoneTrackState(start_point=p))
        if point_in_polygon(p, self.poly):
            st.inside_streak += 1
            st.outside_streak = 0
            if st.first_inside_ts is None:
                st.first_inside_ts = ts
        else:
            st.outside_streak += 1
            st.inside_streak = 0
            st.first_inside_ts = None if not st.inside else st.first_inside_ts
            if not st.inside:
                st.seen_outside = True

        # confirm entry after persistence (filters one-frame box jitter)
        if (not st.inside and st.inside_streak >= self.min_persistence
                and st.first_inside_ts is not None and ts - st.first_inside_ts >= self.min_seconds):
            st.inside, st.enter_ts, st.alerted, st.loiter_alerted = True, st.first_inside_ts, False, False
        # confirm exit
        if st.inside and st.outside_streak >= self.exit_persistence:
            st.inside, st.enter_ts, st.first_inside_ts = False, None, None
            st.seen_outside = True

        if not st.inside:
            return out
        session = f"{self.zone.id}:{t.root_uid}:{int((st.enter_ts or ts) * 1000)}"

        # intrusion / restricted access (re-evaluated while inside so schedules that start later apply)
        if self.event_type and not st.alerted and t.mean_confidence >= self.min_track_conf:
            transition_ok = st.seen_outside or not self.require_transition
            moving_ok = (not self.require_motion or
                         (st.start_point is not None and math.dist(st.start_point, p) >= self.motion_min))
            if transition_ok and moving_ok:
                decision = self.policies.evaluate(t.cls, local_dt)
                if decision.action == "alert":
                    st.alerted = True
                    label = "Intrusion" if self.event_type == "INTRUSION_DETECTED" else "Restricted-area access"
                    out.append(self._candidate(
                        self.event_type, t, ts, decision.severity,
                        f"{label}: {t.cls.replace('_', ' ')} in '{self.zone.name}'", session,
                        decision.policy_id, {"reason": decision.reason, "entered_from_outside": st.seen_outside,
                                             "zone_type": self.zone.zone_type}))

        # loitering (dwell-time extension)
        if self.dwell_seconds > 0 and not st.loiter_alerted and st.enter_ts is not None \
                and ts - st.enter_ts >= self.dwell_seconds:
            st.loiter_alerted = True
            out.append(self._candidate(
                "LOITERING_DETECTED", t, ts, "low",
                f"Loitering: {t.cls.replace('_', ' ')} in '{self.zone.name}' for {int(ts - st.enter_ts)}s",
                session + ":loiter", meta={"dwell_seconds": round(ts - st.enter_ts, 1)}))
        return out

    def crowd_check(self, ts: float, sample: TrackView | None) -> list[EventCandidate]:
        """Zone-level crowd threshold (optional extension)."""
        if self.crowd_threshold <= 0:
            return []
        n = self.occupancy
        if n >= self.crowd_threshold:
            if self._crowd_since is None:
                self._crowd_since = ts
            if not self._crowd_alerted and ts - self._crowd_since >= self.crowd_seconds and sample:
                self._crowd_alerted = True
                c = self._candidate("CROWD_DETECTED", sample, ts, "medium",
                                    f"Crowd: {n} objects in '{self.zone.name}'",
                                    f"{self.zone.id}:crowd:{int(self._crowd_since * 1000)}",
                                    meta={"count": n})
                c.track_uid = c.root_uid = None
                return [c]
        else:
            self._crowd_since, self._crowd_alerted = None, False
        return []

    def forget(self, uid: int) -> None:
        self.state.pop(uid, None)


def build_rules(zones: list[ZoneSpec], width: int, height: int):
    lines, polys = [], []
    for z in zones:
        if not z.enabled:
            continue
        if z.shape == "line":
            lines.append(LineRule(z, width, height))
        else:
            polys.append(PolygonRule(z, width, height))
    return lines, polys


LocalTimeFn = Callable[[float], datetime]
