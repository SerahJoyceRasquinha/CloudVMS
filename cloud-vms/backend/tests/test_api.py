"""Integration tests: API <-> database, auth, RBAC, scoping, zones, events, signed media."""
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="Admin12345"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client)


@pytest.fixture(scope="module")
def video_file():
    p = get_settings().uploads_dir / "test_gate.mp4"
    w = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
    for i in range(30):
        img = np.full((240, 320, 3), 40, np.uint8)
        cv2.rectangle(img, (10 + i * 5, 100), (40 + i * 5, 200), (200, 200, 200), -1)
        w.write(img)
    w.release()
    return p.name


def test_health_and_auth(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/cameras").status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    assert "X-Request-ID" in r.headers


def test_me_has_permissions(client, admin):
    me = client.get("/api/auth/me", headers=admin).json()
    assert "admin" in me["roles"] and "cameras:manage" in me["permissions"]


def test_camera_crud_and_credentials_hidden(client, admin, video_file):
    r = client.post("/api/cameras", headers=admin, json={
        "name": "Front gate", "location": "Main entrance", "stream_type": "file",
        "stream_reference": video_file, "enabled": False, "analytics_config": {"inference_fps": 4}})
    assert r.status_code == 201, r.text
    cam = r.json()
    assert cam["analytics_config"]["inference_fps"] == 4 and cam["status"] == "DISABLED"
    # inline credentials are rejected
    r = client.post("/api/cameras", headers=admin, json={
        "name": "bad", "stream_type": "rtsp", "stream_reference": "rtsp://user:secret@10.0.0.5/stream"})
    assert r.status_code == 422
    r = client.post("/api/cameras", headers=admin, json={
        "name": "IP cam", "stream_type": "rtsp", "stream_reference": "rtsp://10.0.0.5:554/stream1",
        "username": "cam", "password": "s3cret!", "enabled": False})
    assert r.status_code == 201
    body = r.text
    assert "s3cret" not in body and r.json()["has_credentials"] is True
    from app.db import session_scope
    from app.models import Camera
    from app.services.camera_service import stream_url
    with session_scope() as db:
        c = db.get(Camera, r.json()["id"])
        assert c.credential.password_enc and "s3cret" not in c.credential.password_enc
        assert stream_url(c) == "rtsp://cam:s3cret%21@10.0.0.5:554/stream1"
    r = client.patch(f"/api/cameras/{cam['id']}", headers=admin, json={"location": "Gate 1"})
    assert r.json()["location"] == "Gate 1"
    assert client.get("/api/cameras/9999", headers=admin).status_code == 404


def test_rbac_and_scoping(client, admin, video_file):
    cams = client.get("/api/cameras", headers=admin).json()
    r = client.post("/api/users", headers=admin, json={"username": "viewer1", "password": "Viewer123",
                                                       "roles": ["viewer"], "camera_scope": [cams[1]["id"]]})
    assert r.status_code == 201, r.text
    v = login(client, "viewer1", "Viewer123")
    visible = client.get("/api/cameras", headers=v).json()
    assert [c["id"] for c in visible] == [cams[1]["id"]]
    assert client.get(f"/api/cameras/{cams[0]['id']}", headers=v).status_code == 404  # hidden, not 403
    r = client.post("/api/cameras", headers=v, json={"name": "x", "stream_type": "file", "stream_reference": video_file})
    assert r.status_code == 403
    assert client.get("/api/users", headers=v).status_code == 403
    # weak passwords are refused
    r = client.post("/api/users", headers=admin, json={"username": "weak", "password": "abcdefgh", "roles": ["viewer"]})
    assert r.status_code == 400


def test_zone_validation(client, admin):
    cam_id = client.get("/api/cameras", headers=admin).json()[0]["id"]
    ok = client.post(f"/api/cameras/{cam_id}/zones", headers=admin, json={
        "name": "Footpath", "zone_type": "restricted", "points": [[0.5, 0.1], [0.9, 0.1], [0.9, 0.9], [0.5, 0.9]],
        "severity": "high", "policies": [{"name": "no vehicles", "object_types": ["vehicle"], "action": "alert"}]})
    assert ok.status_code == 201, ok.text
    assert ok.json()["policies"][0]["object_types"] == ["vehicle"]
    bad = client.post(f"/api/cameras/{cam_id}/zones", headers=admin, json={
        "name": "Bowtie", "zone_type": "intrusion", "points": [[0, 0], [1, 1], [1, 0], [0, 1]]})
    assert bad.status_code == 400 and "self-intersection" in str(bad.json()["error"]["details"])
    line = client.post(f"/api/cameras/{cam_id}/zones", headers=admin, json={
        "name": "Gate line", "zone_type": "line", "points": [[0.1, 0.6], [0.9, 0.6]], "config": {"in_side": "negative"}})
    assert line.status_code == 201 and line.json()["shape"] == "line"
    bad_sched = client.post(f"/api/cameras/{cam_id}/zones", headers=admin, json={
        "name": "x", "zone_type": "intrusion", "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]],
        "policies": [{"object_types": ["person"], "schedule": [{"start": "25:00", "end": "06:00"}]}]})
    assert bad_sched.status_code == 422
    zones = client.get(f"/api/cameras/{cam_id}/zones", headers=admin).json()
    assert len(zones) == 2
    r = client.patch(f"/api/zones/{zones[0]['id']}", headers=admin, json={"enabled": False})
    assert r.json()["version"] == 2 and r.json()["enabled"] is False


def _make_event(cam_id, zone_id=None, ts=None, root=1):
    from app.analytics.datatypes import EventCandidate
    from app.analytics.event_engine import Decision
    from app.db import session_scope
    from app.services.event_service import persist_decision
    ts = ts or time.time()
    c = EventCandidate("RESTRICTED_AREA_ACCESS", zone_id, root, root, "car", ts, 0.8, (1, 2, 3, 4), "high",
                       "Restricted-area access: car", f"{zone_id}:{root}:{int(ts * 1000)}")
    with session_scope() as db:
        ev, new = persist_decision(db, cam_id, Decision("new", f"{cam_id}:x:{c.session_key}", c), "test-model")
        return ev.id, new


def test_events_workflow_and_idempotency(client, admin):
    cam_id = client.get("/api/cameras", headers=admin).json()[0]["id"]
    ts = time.time()
    eid, new = _make_event(cam_id, ts=ts)
    eid2, new2 = _make_event(cam_id, ts=ts)  # same dedup key delivered twice
    assert new and not new2 and eid == eid2
    page = client.get("/api/events", headers=admin, params={"event_type": "RESTRICTED_AREA_ACCESS",
                                                            "camera_id": cam_id}).json()
    assert page["total"] == 1 and page["items"][0]["camera_name"] == "Front gate"
    r = client.post(f"/api/events/{eid}/acknowledge", headers=admin)
    assert r.json()["status"] == "ACKNOWLEDGED" and r.json()["acknowledged_at"].endswith("Z")
    r = client.patch(f"/api/events/{eid}", headers=admin, json={"status": "RESOLVED", "note": "security checked"})
    assert r.json()["status"] == "RESOLVED" and "security checked" in r.json()["notes"]
    r = client.patch(f"/api/events/{eid}", headers=admin, json={"status": "ACKNOWLEDGED"})
    assert r.status_code == 400  # RESOLVED -> ACKNOWLEDGED not allowed
    csv_text = client.get("/api/events/export.csv", headers=admin).text
    assert "RESTRICTED_AREA_ACCESS" in csv_text


def test_evidence_signed_urls(client, admin):
    from app.db import session_scope
    from app.models import Event
    from app.workers.evidence import store_snapshot
    cam_id = client.get("/api/cameras", headers=admin).json()[0]["id"]
    eid, _ = _make_event(cam_id, root=77)
    jpeg = cv2.imencode(".jpg", np.zeros((10, 10, 3), np.uint8))[1].tobytes()
    store_snapshot(eid, cam_id, time.time(), jpeg)
    with session_scope() as db:
        assert db.get(Event, eid).snapshot_object_key
    ev = client.get(f"/api/events/{eid}/evidence", headers=admin).json()
    url = ev[0]["url"]
    assert url.startswith("/api/media/snapshots/")
    r = client.get(url)
    assert r.status_code == 200 and r.content == jpeg
    tampered = url.replace("sig=", "sig=00")
    assert client.get(tampered).status_code == 403
    r = client.get(url, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10
    for bad in ("bytes=abc-", "bytes=-", "bytes=5-x"):  # malformed ranges -> 416, not a 500
        assert client.get(url, headers={"Range": bad}).status_code == 416


def test_storage_key_validation():
    from app.services.storage import InvalidKey, validate_key
    validate_key("evidence/1/2026-09-27/5/clip.mp4")
    for bad in ("../etc/passwd", "evidence/../../x", "other/1.jpg", "evidence//x", "evidence/1/"):
        with pytest.raises(InvalidKey):
            validate_key(bad)


def test_analytics_summary_counts_unique_entries(client, admin):
    from app.core.timeutil import utcnow
    from app.db import session_scope
    from app.models import Crossing
    cam_id = client.get("/api/cameras", headers=admin).json()[0]["id"]
    now = utcnow()
    with session_scope() as db:
        rows = [(1, "person", "person", "in"), (1, "person", "person", "in"),  # same root twice -> 1
                (2, "person", "person", "in"), (2, "person", "person", "out"),
                (3, "vehicle", "two_wheeler", "in"), (4, "vehicle", "car", "in")]
        for root, g, cls, d in rows:
            db.add(Crossing(camera_id=cam_id, zone_id=1, track_uid=root, root_uid=root, object_class=cls,
                            object_group=g, subtype=cls, direction=d, ts=now))
    s = client.get("/api/analytics/summary", headers=admin).json()
    assert s["people"]["entries"] == 2 and s["people"]["exits"] == 1 and s["people"]["on_site_estimate"] == 1
    assert s["vehicles"]["entries"] == 2 and s["vehicles"]["types_in"] == {"two_wheeler": 1, "car": 1}
    assert s["cameras"]["total"] == 2


def test_forced_password_change_is_enforced_by_server(client, admin):
    r = client.post("/api/users", headers=admin, json={"username": "op1", "password": "Operator1",
                                                       "roles": ["operator"]})
    uid = r.json()["id"]
    # an admin-reset password must be replaced before the account can do anything else
    client.patch(f"/api/users/{uid}", headers=admin, json={"password": "Reset1234"})
    h = login(client, "op1", "Reset1234")
    r = client.get("/api/cameras", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "password_change_required"
    assert client.get("/api/auth/me", headers=h).json()["must_change_password"] is True
    r = client.post("/api/auth/change-password", headers=h,
                    json={"current_password": "Reset1234", "new_password": "MyOwn5678"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/cameras", headers=h).status_code == 200


def test_client_ip_only_trusts_configured_proxies(monkeypatch):
    from types import SimpleNamespace
    from app.deps import client_ip
    req = SimpleNamespace(client=SimpleNamespace(host="10.1.1.1"),
                          headers={"x-forwarded-for": "6.6.6.6, 192.168.0.9"})
    assert client_ip(req) == "10.1.1.1"  # spoofed header ignored
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.1.1.1"])
    assert client_ip(req) == "192.168.0.9"  # the address the proxy itself appended


def test_upload_weights_never_overwrites_existing_files(client, admin):
    existing = get_settings().weights_dir / "yolo26n.pt"
    existing.write_bytes(b"original weights")
    for name in ("yolo26n", "coco-yolo26n"):  # same file name / same model name as the seeded default
        r = client.post("/api/models/upload-weights", headers=admin,
                        data={"name": name, "role": "shared", "class_map": '{"person": "person"}'},
                        files={"file": ("new.pt", b"attacker weights", "application/octet-stream")})
        assert r.status_code == 400, r.text
    assert existing.read_bytes() == b"original weights"
    existing.unlink()


def test_upload_size_limit(client, admin, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_video_upload_mb", 0)
    before = set(get_settings().uploads_dir.iterdir())
    r = client.post("/api/cameras/upload-video", headers=admin,
                    files={"file": ("big.mp4", b"x" * 1024, "video/mp4")})
    assert r.status_code == 400 and "limit" in r.json()["error"]["message"]
    assert set(get_settings().uploads_dir.iterdir()) == before  # partial file removed


def test_stale_training_jobs_are_failed_on_startup():
    from app.db import session_scope
    from app.main import bootstrap
    from app.models import Dataset, TrainingJob
    with session_scope() as db:
        ds = Dataset(name="stale-ds", kind="detection", path="x", status="processing")
        db.add(ds)
        db.flush()
        job = TrainingJob(dataset_id=ds.id, base_model="yolo26n.pt", role="shared", params={}, status="running")
        db.add(job)
        db.flush()
        ds_id, job_id = ds.id, job.id
    bootstrap()
    with session_scope() as db:
        assert db.get(TrainingJob, job_id).status == "failed"
        assert db.get(Dataset, ds_id).status == "failed"


def test_supervisor_survives_detector_load_failure():
    from app.workers.supervisor import Supervisor

    def broken():
        raise FileNotFoundError("weights missing")
    sup = Supervisor(detector_factory=broken)
    try:
        sup._try_ensure_detector()  # must not raise
        assert "weights missing" in sup.detector_error and sup.detector is None
        sup._try_ensure_detector()  # within the 60 s back-off: no retry, still no exception
    finally:
        sup.stop()


def test_audit_log_and_logout(client, admin):
    logs = client.get("/api/audit-logs", headers=admin, params={"action": "camera."}).json()
    assert logs["total"] >= 2 and all("password" not in str(i["details"]).lower() or "***" in str(i["details"])
                                      for i in logs["items"])
    h = login(client)
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401  # token revoked


def test_live_frame_long_poll(client, video_file):
    from app.workers.framebus import get_frame_bus
    admin = login(client)  # the module-wide session was signed out by an earlier test
    cam = client.post("/api/cameras", headers=admin, json={
        "name": "Frame poll", "stream_type": "file", "stream_reference": video_file, "enabled": False}).json()
    assert client.get(f"/api/live/{cam['id']}/frame").status_code == 401
    r = client.get(f"/api/live/{cam['id']}/frame", headers=admin, params={"wait": 0})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"  # placeholder
    get_frame_bus().publish(cam["id"], b"fake-jpeg")
    r = client.get(f"/api/live/{cam['id']}/frame", headers=admin, params={"after": 0, "wait": 1})
    ts = float(r.headers["X-Frame-Ts"])
    assert r.content == b"fake-jpeg" and ts > 0
    t0 = time.time()
    r = client.get(f"/api/live/{cam['id']}/frame", headers=admin, params={"after": ts, "wait": 0.3})
    assert r.status_code == 200 and time.time() - t0 >= 0.25  # waited for a newer frame


def test_deleting_camera_erases_its_statistics(client, video_file):
    from app.core.timeutil import utcnow
    from app.db import session_scope
    from app.models import Crossing, Event, EventEvidence, Identity, Track
    from app.services.storage import get_storage
    h = login(client)
    before = client.get("/api/analytics/summary", headers=h).json()
    cam = client.post("/api/cameras", headers=h, json={
        "name": "To delete", "stream_type": "file", "stream_reference": video_file, "enabled": False}).json()
    cid, now = cam["id"], utcnow()
    key = f"evidence/{cid}/2026-01-01/1/clip.mp4"
    get_storage().put_bytes(key, b"clip", "video/mp4")
    with session_scope() as db:
        for root in (901, 902, 903):
            db.add(Identity(camera_id=cid, root_uid=root, object_class="person", object_group="person",
                            first_seen_at=now))
            db.add(Track(camera_id=cid, track_uid=root, root_uid=root, object_class="person", object_group="person",
                         first_seen_at=now, last_seen_at=now))
            db.add(Crossing(camera_id=cid, zone_id=1, track_uid=root, root_uid=root, object_class="person",
                            object_group="person", direction="in", ts=now))
        ev = Event(camera_id=cid, event_type="INTRUSION_DETECTED", event_timestamp=now, title="x",
                   dedup_key=f"{cid}:test-delete")
        db.add(ev)
        db.flush()
        db.add(EventEvidence(event_id=ev.id, kind="clip", camera_id=cid, object_key=key, upload_status="uploaded"))
    mid = client.get("/api/analytics/summary", headers=h).json()
    assert mid["people"]["unique_seen"] == before["people"]["unique_seen"] + 3
    assert mid["people"]["entries"] == before["people"]["entries"] + 3

    r = client.delete(f"/api/cameras/{cid}", headers=h)
    assert r.status_code == 200 and r.json()["removed"]["identities"] == 3 and r.json()["removed"]["events"] == 1
    after = client.get("/api/analytics/summary", headers=h).json()
    for k in ("unique_seen", "entries"):
        assert after["people"][k] == before["people"][k]  # back to where it was without this camera
    assert after["events"]["total"] == before["events"]["total"]
    with session_scope() as db:
        for model in (Identity, Track, Crossing, Event, EventEvidence):
            assert db.query(model).filter(model.camera_id == cid).count() == 0
    for _ in range(50):  # stored media is removed in the background
        if not get_storage().exists(key):
            break
        time.sleep(0.05)
    assert not get_storage().exists(key)


def test_rows_of_deleted_cameras_are_never_counted(client):
    """Counts left behind by a camera deleted by an older version must not show up (nor be
    inherited by a new camera that gets the same id)."""
    from app.core.timeutil import utcnow
    from app.db import session_scope
    from app.models import Crossing, Identity, Track
    from app.services.camera_service import purge_orphaned_data
    h = login(client)
    before = client.get("/api/analytics/summary", headers=h).json()
    ghost, now = 987654, utcnow()
    with session_scope() as db:
        for root in range(5):
            db.add(Identity(camera_id=ghost, root_uid=root, object_class="person", object_group="person",
                            first_seen_at=now))
            db.add(Track(camera_id=ghost, track_uid=root, root_uid=root, object_class="car", object_group="vehicle",
                         first_seen_at=now, last_seen_at=now))
            db.add(Crossing(camera_id=ghost, zone_id=1, track_uid=root, root_uid=root, object_class="person",
                            object_group="person", direction="in", ts=now))
    s = client.get("/api/analytics/summary", headers=h).json()
    assert s["people"] == before["people"] and s["vehicles"]["unique_seen"] == before["vehicles"]["unique_seen"]
    with session_scope() as db:
        removed = purge_orphaned_data(db)
    assert removed == {"tracks": 5, "crossings": 5, "identities": 5}
    with session_scope() as db:
        assert db.query(Identity).filter(Identity.camera_id == ghost).count() == 0
