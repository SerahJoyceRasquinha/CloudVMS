"""Unit tests: geometry, schedules/policies, line crossing, zone rules, tracker, event dedup."""
from datetime import datetime

import numpy as np
import pytest

from app.analytics.classes import matches_object_types
from app.analytics.datatypes import Detection, TrackView
from app.analytics.event_engine import EngineConfig, EventEngine
from app.analytics.geometry import (is_simple_polygon, point_in_polygon, segments_intersect,
                                    validate_geometry)
from app.analytics.pipeline import AnalyticsConfig, CameraAnalytics, suppress_riders
from app.analytics.policy import PolicySet, PolicySpec, in_schedule, validate_schedule
from app.analytics.rules import LineRule, PolygonRule, ZoneSpec
from app.analytics.tracker import ACTIVE, LOST, ByteTracker, TrackerConfig

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


# ------------------------------------------------------------------ geometry
def test_point_in_polygon():
    assert point_in_polygon((5, 5), SQUARE)
    assert not point_in_polygon((15, 5), SQUARE)
    assert point_in_polygon((10, 5), SQUARE)  # on the edge counts as inside


def test_segments_intersect():
    assert segments_intersect((0, 0), (10, 10), (0, 10), (10, 0))
    assert not segments_intersect((0, 0), (1, 1), (5, 5), (6, 7))


def test_polygon_validation():
    bowtie = [(0, 0), (1, 1), (1, 0), (0, 1)]
    assert not is_simple_polygon(bowtie)
    assert validate_geometry("polygon", [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]) == []
    assert "self-intersection" in " ".join(validate_geometry("polygon", [[0, 0], [1, 1], [1, 0], [0, 1]]))
    assert validate_geometry("polygon", [[0, 0], [1, 1]])  # too few points
    assert validate_geometry("line", [[0.1, 0.5], [0.9, 0.5]]) == []
    assert validate_geometry("line", [[0.1, 0.5], [1.4, 0.5]])  # out of range


# ------------------------------------------------------------------ schedules & policies
def test_schedule_same_day_and_overnight():
    mon_10 = datetime(2026, 9, 28, 10, 0)  # Monday
    mon_23 = datetime(2026, 9, 28, 23, 0)
    tue_03 = datetime(2026, 9, 29, 3, 0)
    tue_10 = datetime(2026, 9, 29, 10, 0)
    day = [{"days": [0], "start": "09:00", "end": "17:00"}]
    night = [{"days": [0], "start": "20:00", "end": "06:00"}]
    assert in_schedule(day, mon_10) and not in_schedule(day, tue_10)
    assert in_schedule(night, mon_23) and in_schedule(night, tue_03) and not in_schedule(night, tue_10)
    assert in_schedule([], tue_10)
    assert validate_schedule([{"start": "25:99", "end": "x"}])


def test_policy_allow_beats_alert_and_default():
    ps = PolicySet("alert", "high", [
        PolicySpec(1, ["vehicle"], [], "alert", name="no vehicles"),
        PolicySpec(2, ["bus"], [], "allow", name="college buses ok"),
    ])
    now = datetime(2026, 9, 28, 12, 0)
    assert ps.evaluate("car", now).action == "alert"
    assert ps.evaluate("bus", now).action == "allow"
    assert ps.evaluate("person", now).action == "alert"  # zone default
    assert matches_object_types(["vehicle"], "two_wheeler")
    assert not matches_object_types(["vehicle"], "person")


# ------------------------------------------------------------------ helpers
def tv(uid, box, cls="person", root=None, conf=0.9, hits=5):
    return TrackView(uid=uid, root_uid=root or uid, cls=cls, group="person" if cls == "person" else "vehicle",
                     subtype=cls, bbox=box, confidence=conf, mean_confidence=conf, state="ACTIVE", hits=hits,
                     first_ts=0, last_ts=0, matched=True)


def box_at(x, y, w=40, h=100):  # (x, y) = feet position
    return (x - w / 2, y - h, x + w / 2, y)


# ------------------------------------------------------------------ line counting
def make_line(**cfg):
    z = ZoneSpec(1, 1, "gate", "line", "line", [[0.0, 0.5], [1.0, 0.5]], config=cfg)
    return LineRule(z, 1000, 1000)


def test_line_counts_once_per_direction_with_hysteresis():
    rule = make_line()
    ys = [300, 400, 490, 505, 495, 510, 600, 700]  # jitter around the line at y=500
    crossings = [rule.update(tv(7, box_at(500, y)), t) for t, y in enumerate(ys)]
    real = [c for c in crossings if c]
    assert len(real) == 1 and real[0].direction == "in"  # moving down = positive side
    # same person walks back out, then in again -> 'out' once, 'in' not counted twice
    for t, y in enumerate([400, 300, 600], start=20):
        c = rule.update(tv(7, box_at(500, y)), t)
    assert rule.totals == {"in": 1, "out": 1}


def test_line_flip_direction_and_segment_extent():
    rule = make_line(in_side="negative")
    rule.update(tv(1, box_at(500, 300)), 0)
    c = rule.update(tv(1, box_at(500, 700)), 1)
    assert c and c.direction == "out"
    short = LineRule(ZoneSpec(2, 1, "short", "line", "line", [[0.4, 0.5], [0.6, 0.5]]), 1000, 1000)
    short.update(tv(3, box_at(100, 300)), 0)
    assert short.update(tv(3, box_at(100, 700)), 1) is None  # passed beside the segment


def test_reid_root_counted_once():
    rule = make_line()
    rule.update(tv(1, box_at(500, 300), root=1), 0)
    assert rule.update(tv(1, box_at(500, 700), root=1), 1)
    rule.update(tv(9, box_at(500, 300), root=1), 5)  # new track, same person (re-identified)
    assert rule.update(tv(9, box_at(500, 700), root=1), 6) is None


# ------------------------------------------------------------------ polygon zones
NOON = datetime(2026, 9, 28, 12, 0)


def restricted_zone(**cfg):
    return ZoneSpec(5, 1, "footpath", "restricted", "polygon",
                    [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]], severity="high", default_action="allow",
                    config=cfg, policies=[PolicySpec(11, ["vehicle"], [], "alert", name="no vehicles")])


def test_restricted_zone_alerts_once_per_session_for_vehicles_only():
    rule = PolygonRule(restricted_zone(), 1000, 1000)
    events = []
    for t, x in enumerate([200, 400, 600, 650, 700, 750]):
        events += rule.update(tv(1, box_at(x, 500), cls="car"), t, NOON)
        events += rule.update(tv(2, box_at(x, 800), cls="person"), t, NOON)
    assert [e.event_type for e in events] == ["RESTRICTED_AREA_ACCESS"]
    assert events[0].severity == "high" and events[0].policy_id == 11


def test_persistence_filters_single_frame_jitter():
    rule = PolygonRule(restricted_zone(min_persistence=3), 1000, 1000)
    ev = []
    for t, x in enumerate([400, 520, 400, 400]):  # one noisy frame inside
        ev += rule.update(tv(1, box_at(x, 500), cls="car"), t, NOON)
    assert ev == []


def test_intrusion_requires_entry_from_outside_and_schedule():
    zone = ZoneSpec(6, 1, "perimeter", "intrusion", "polygon", [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]],
                    default_action="allow",
                    policies=[PolicySpec(1, ["person", "vehicle"],
                                         [{"days": list(range(7)), "start": "20:00", "end": "06:00"}], "alert")])
    night = datetime(2026, 9, 28, 22, 0)
    rule = PolygonRule(zone, 1000, 1000)
    assert sum((rule.update(tv(1, box_at(x, 500)), t, NOON) for t, x in enumerate([300, 600, 700, 800])), []) == []
    rule = PolygonRule(zone, 1000, 1000)
    ev = sum((rule.update(tv(1, box_at(x, 500)), t, night) for t, x in enumerate([300, 600, 700, 800])), [])
    assert len(ev) == 1 and ev[0].event_type == "INTRUSION_DETECTED"
    rule = PolygonRule(zone, 1000, 1000)  # appears already inside: not a boundary crossing
    ev = sum((rule.update(tv(1, box_at(x, 500)), t, night) for t, x in enumerate([600, 700, 800])), [])
    assert ev == []


def test_loitering_dwell():
    z = ZoneSpec(7, 1, "atm", "monitoring", "polygon", [[0, 0], [1, 0], [1, 1], [0, 1]],
                 config={"dwell_seconds": 10})
    rule = PolygonRule(z, 1000, 1000)
    ev = sum((rule.update(tv(1, box_at(500, 500)), t, NOON) for t in range(0, 15)), [])
    assert [e.event_type for e in ev] == ["LOITERING_DETECTED"]


# ------------------------------------------------------------------ tracker
def det(x, y, conf=0.9, cls="person"):
    return Detection(box_at(x, y), conf, cls, cls)


def test_tracker_keeps_id_and_bridges_low_confidence():
    tr = ByteTracker(TrackerConfig(min_hits=2))
    ids = []
    for i in range(8):
        conf = 0.2 if i in (4, 5) else 0.9  # partially occluded -> weak boxes
        out = tr.update([det(100 + i * 10, 500, conf)], i * 0.2)
        ids.append([t.uid for t in out.tracks])
    assert ids[0] == [] and ids[1] == [1]  # confirmed after 2 hits
    assert all(x == [1] for x in ids[1:])  # same id through the weak detections


def test_tracker_lost_then_terminated_and_group_gating():
    tr = ByteTracker(TrackerConfig(min_hits=1, max_lost_seconds=1.0))
    tr.update([det(100, 500)], 0.0)
    out = tr.update([det(102, 500, cls="car")], 0.2)  # a car at the same spot must not continue the person
    uids = {t.uid for t in out.tracks}
    assert 1 not in uids and len(uids) == 1
    assert tr.get(1).state == LOST
    out = tr.update([], 1.5)
    assert 1 in [t.uid for t in out.terminated]


def test_tracker_low_fps_distance_fallback():
    tr = ByteTracker(TrackerConfig(min_hits=1))
    tr.update([det(100, 500)], 0.0)
    out = tr.update([det(145, 500)], 1.0)  # moved more than a box width between samples
    assert [t.uid for t in out.tracks] == [1]


# ------------------------------------------------------------------ event engine
def test_event_engine_dedup_cooldown_merge():
    from app.analytics.datatypes import EventCandidate

    def cand(root, ts, session):
        return EventCandidate("RESTRICTED_AREA_ACCESS", 5, root, root, "car", ts, 0.9, None, "high", "x", session)

    eng = EventEngine(1, EngineConfig(cooldown_seconds=30, merge_window_seconds=5))
    assert eng.decide(cand(1, 0, "5:1:0")).kind == "new"
    assert eng.decide(cand(1, 1, "5:1:0")).kind == "drop"  # same session
    assert eng.decide(cand(1, 10, "5:1:10000")).kind == "drop"  # re-entry within cooldown
    assert eng.decide(cand(2, 2, "5:2:2000")).kind == "merge"  # second car inside merge window
    assert eng.decide(cand(3, 20, "5:3:20000")).kind == "new"
    assert eng.decide(cand(1, 45, "5:1:45000")).kind == "new"  # cooldown over


# ------------------------------------------------------------------ pipeline
def test_rider_suppression():
    bike = Detection((100, 300, 200, 500), 0.9, "two_wheeler", "motorcycle")
    rider = Detection((110, 200, 190, 480), 0.9, "person", "person")
    walker = Detection((400, 200, 450, 500), 0.9, "person", "person")
    kept = suppress_riders([bike, rider, walker], 0.3)
    assert rider not in kept and walker in kept and bike in kept


def test_pipeline_end_to_end_without_frames():
    zones = [ZoneSpec(1, 1, "gate", "line", "line", [[0.0, 0.5], [1.0, 0.5]]), restricted_zone()]
    cam = CameraAnalytics(1, AnalyticsConfig(reid_enabled=False, tracker={"min_hits": 2}), zones,
                          local_time=lambda ts: NOON)
    crossings, decisions = [], []
    for i in range(12):
        y = 300 + i * 40
        r = cam.process(i * 0.2, None, [det(300, y), det(600 + i * 5, 300, cls="car")], frame_size=(1000, 1000))
        crossings += r.crossings
        decisions += r.decisions
    assert len(crossings) == 1 and crossings[0].group == "person" and crossings[0].direction == "in"
    assert [d.kind for d in decisions] == ["new"]
    assert decisions[0].candidate.event_type == "RESTRICTED_AREA_ACCESS"
    assert len(cam.flush()) == 2


class _ColourEmbedder:
    """Stand-in for OSNet: the mean colour of the box is the 'appearance'."""
    name, deep = "colour", True

    def embed(self, frame, bbox):
        x1, y1, x2, y2 = (int(v) for v in bbox)
        v = frame[y1:y2, x1:x2].reshape(-1, 3).mean(axis=0).astype(np.float32) + 1.0
        return v / np.linalg.norm(v)


def _scene(people):
    img = np.zeros((1000, 1000, 3), np.uint8)
    for (x, y), colour in people:
        img[y - 150:y, x - 40:x + 40] = colour
    return img


def test_person_leaving_and_returning_is_counted_once():
    cam = CameraAnalytics(1, AnalyticsConfig(tracker={"min_hits": 2}), [], local_time=lambda ts: NOON)
    cam.reid.embedder = _ColourEmbedder()
    cam.reid.memory = 900
    red, blue = (0, 0, 220), (220, 60, 0)
    identities = []

    def run(t0, x0, colour, frames=10):
        for i in range(frames):
            pos = (x0 + i * 10, 500)
            r = cam.process(t0 + i * 0.2, _scene([(pos, colour)]), [det(pos[0], pos[1])])
            identities.extend(r.identities)

    run(0, 200, red)
    for i in range(20):  # nobody in view for a while: the track ends
        identities.extend(cam.process(2 + i * 0.5, _scene([]), []).identities)
    run(60, 700, red)  # same person, a minute later, somewhere else in the frame
    for i in range(20):
        identities.extend(cam.process(62 + i * 0.5, _scene([]), []).identities)
    run(120, 400, blue)  # somebody else
    assert [i.group for i in identities] == ["person", "person"]
    assert len({i.root_uid for i in identities}) == 2


def test_track_ids_do_not_repeat_across_restarts():
    a = CameraAnalytics(1, AnalyticsConfig(reid_enabled=False), [])
    b = CameraAnalytics(1, AnalyticsConfig(reid_enabled=False), [])
    a.process(0, None, [det(100, 500)], frame_size=(1000, 1000))
    b.process(0, None, [det(100, 500)], frame_size=(1000, 1000))
    assert a.tracker.tracks[0].uid != b.tracker.tracks[0].uid


def test_reid_memory_survives_restart():
    from app.analytics.reid import ReIdentifier, decode_protos, encode_protos
    rng = np.random.default_rng(0)
    a, b = (v / np.linalg.norm(v) for v in rng.normal(size=(2, 512)).astype(np.float32))
    stored = encode_protos([a])
    fresh = ReIdentifier(_ColourEmbedder(), memory_seconds=900)
    fresh.restore(42, "person", 100.0, decode_protos(stored))
    assert fresh.match("person", 400.0, (500, 500), a, 1400) == 42  # same appearance after restart
    assert fresh.match("person", 400.0, (500, 500), b, 1400) is None  # somebody else
    assert fresh.match("vehicle", 400.0, (500, 500), a, 1400) is None  # never across groups


if __name__ == "__main__":
    pytest.main([__file__])
