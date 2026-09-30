"""End-to-end: camera -> ingestion -> (fake) detection -> tracking -> zones -> incident -> evidence -> API.

Uses a deterministic fake detector so the test doesn't need model weights;
test_real_model.py covers the real YOLO path.
"""
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.analytics.datatypes import Detection
from app.analytics.detector import FakeDetector
from app.core.config import get_settings

FPS, SECONDS, W, H = 10, 12, 640, 480


def person_box(t: float):
    """A person walks top->bottom on the right half of the image over 8 seconds."""
    if t > 8:
        return None
    y = 60 + t * 50  # feet y from 60 to 460
    return (440.0, y - 120, 490.0, y)


def make_video(path):
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(FPS * SECONDS):
        img = np.full((H, W, 3), 60, np.uint8)
        b = person_box(i / FPS)
        if b:
            cv2.rectangle(img, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (230, 230, 230), -1)
        w.write(img)
    w.release()


@pytest.fixture(scope="module")
def env():
    from app.main import app
    from app.workers import supervisor as sup_mod
    video = get_settings().uploads_dir / "e2e_gate.mp4"
    make_video(video)
    t0 = {}

    def script(i, ts, frame):
        t0.setdefault("t", ts)
        b = person_box(ts - t0["t"])
        return [Detection(b, 0.9, "person", "person", "fake")] if b else []

    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": "admin", "password": "Admin12345"})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        sup = sup_mod.Supervisor(detector_factory=lambda: FakeDetector(script))
        sup_mod._supervisor = sup
        yield client, h, video.name, sup
        sup.stop()
        sup_mod._supervisor = None


def wait_for(fn, timeout=40, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def test_full_pipeline(env):
    client, h, video, sup = env
    cam = client.post("/api/cameras", headers=h, json={
        "name": "E2E gate", "stream_type": "file", "stream_reference": video, "enabled": False,
        "analytics_config": {"inference_fps": 5, "realtime": False, "loop": False, "reid_enabled": True,
                             "evidence_pre_seconds": 2, "evidence_post_seconds": 2, "evidence_fps": 5,
                             "tracker": {"min_hits": 2}}}).json()
    cid = cam["id"]
    line = client.post(f"/api/cameras/{cid}/zones", headers=h, json={
        "name": "Gate line", "zone_type": "line", "points": [[0.5, 0.5], [1.0, 0.5]]}).json()
    zone = client.post(f"/api/cameras/{cid}/zones", headers=h, json={
        "name": "Staff only", "zone_type": "restricted", "points": [[0.6, 0.7], [1.0, 0.7], [1.0, 1.0], [0.6, 1.0]],
        "severity": "high", "default_action": "allow",
        "policies": [{"name": "no visitors", "object_types": ["person"], "action": "alert"}]}).json()
    assert line["id"] and zone["id"]
    client.post(f"/api/cameras/{cid}/stream/start", headers=h)

    # drive the supervisor manually (deterministic instead of its background loop)
    sup.reconcile()
    assert cid in sup.workers
    ev = wait_for(lambda: client.get("/api/events", headers=h, params={"camera_id": cid}).json()["items"])
    assert ev, "no incident was created"
    assert len(ev) == 1  # one person, one zone session -> exactly one incident
    e = ev[0]
    assert e["event_type"] == "RESTRICTED_AREA_ACCESS" and e["severity"] == "high" and e["zone_name"] == "Staff only"

    def evidence_ready():
        items = client.get(f"/api/events/{e['id']}/evidence", headers=h).json()
        kinds = {i["kind"]: i for i in items if i["upload_status"] == "uploaded"}
        return kinds if {"snapshot", "clip"} <= set(kinds) else None
    kinds = wait_for(evidence_ready, timeout=60)
    assert kinds, "evidence was not produced"
    clip = client.get(kinds["clip"]["url"])
    assert clip.status_code == 200 and clip.content[4:8] == b"ftyp"  # a real MP4
    assert kinds["clip"]["duration_s"] > 1

    assert sup.workers[cid].metrics.frames_dropped == 0  # offline analysis waits instead of dropping frames
    sup.persist_health()
    sup.db_writer.flush()
    s = wait_for(lambda: (lambda d: d if d["people"]["entries"] >= 1 else None)(
        client.get("/api/analytics/summary", headers=h, params={"camera_id": cid}).json()), timeout=20)
    assert s and s["people"]["entries"] == 1 and s["people"]["exits"] == 0
    health = client.get(f"/api/cameras/{cid}/health", headers=h).json()
    assert health["series"] and health["series"][-1]["inference_fps"] >= 0

    # stop the stream -> worker stops and camera shows DISABLED
    client.post(f"/api/cameras/{cid}/stream/stop", headers=h)
    sup.reconcile()
    assert cid not in sup.workers
    sup.db_writer.flush()
    from app.db import session_scope
    from app.models import Track
    with session_scope() as db:
        tracks = db.query(Track).filter(Track.camera_id == cid).all()
        assert len({t.root_uid for t in tracks}) == 1


def test_recording_segments(env):
    client, h, video, sup = env
    cam = client.post("/api/cameras", headers=h, json={
        "name": "Recorder test", "stream_type": "file", "stream_reference": video, "enabled": True,
        "recording_enabled": True, "analytics_enabled": False,
        "recording_config": {"segment_seconds": 3, "fps": 5, "max_height": 240}}).json()
    cid = cam["id"]
    sup.reconcile()
    assert cid in sup.recorders

    def ingested_segments():
        sup.reconcile()  # ingests finished segments, as the supervisor loop does every few seconds
        return client.get(f"/api/cameras/{cid}/recordings", headers=h).json()["items"]
    segs = wait_for(ingested_segments, timeout=40, every=1) or []
    assert len(segs) >= 1 and all(s["status"] in ("complete", "incomplete") for s in segs)
    play = client.get(f"/api/recordings/{segs[0]['id']}/playback", headers=h).json()
    data = client.get(play["url"]).content
    assert len(data) > 1000
    client.post(f"/api/cameras/{cid}/stream/stop", headers=h)
    sup.reconcile()
    assert cid not in sup.recorders
    # live preview frames were published for the analytics-off camera
    assert sup.bus.latest(cid) is None  # cleared on stop
