# Architecture

## Pipeline

```
 CAMERAS (RTSP / HTTP / webcam / uploaded video replayed in real time)
    │
    ├──► Recorder (FFmpeg, one process per camera) ── H.264 fragmented-MP4 segments ──► object storage
    │         separate from AI; segments cut short by a crash stay playable ("incomplete")
    │
    └──► Capture thread (per camera)
            │  reconnect with back-off · health · thumbnails
            ├──► rolling JPEG ring buffer (pre-event evidence)
            └──► frame sampling at inference FPS (configurable, e.g. 5 of 25 fps)
                    │
                    ▼
          Inference service (shared by all cameras)
            bounded queue per camera (drops the stalest frame = backpressure)
            round-robin batching across cameras
            Detector: 1 shared model  or  person model + Indian-vehicle model
                    │  detections in canonical classes
                    ▼
          Analytics thread (per camera)
            rider suppression → ByteTrack-style tracker → OSNet re-ID (identities)
                    │  tracks (ground point = bottom-centre of box)
            ┌───────┴──────────────────────────────┐
            │ Line rules: unique in/out crossings  │ Polygon rules: intrusion, restricted access,
            │   (hysteresis, once per root track)  │   loitering, crowd (persistence, schedules,
            │                                      │   policies)
            └───────┬──────────────────────────────┘
                    ▼
          Event engine: session dedup · cooldown · merge window · severity · idempotent dedup_key
                    │
         ┌──────────┼─────────────────┬─────────────────────┐
         ▼          ▼                 ▼                     ▼
   events table  snapshot (JPEG)  evidence clip         notifications
                 → storage        (pre + post frames,   (webhook, SSE to
                                   H.264) → storage      dashboard)
```

The web API never processes video. It only edits **desired state** in the database (cameras
enabled, zones, analytics settings, active models). A **supervisor** reconciles that state every
few seconds: it starts/stops/hot-reloads camera pipelines and recorders for its `worker_group`,
persists health, raises `CAMERA_OFFLINE` / `RECORDING_FAILURE` incidents and runs retention.
The supervisor runs inside the API process on a laptop, or as separate worker processes
(`python -m app.workers.run --group ...`) on other machines.

## Code map (spec module → code)

| Spec module | Code |
|---|---|
| 1 Camera management | `api/cameras.py`, `services/camera_service.py`, `models.Camera`, `CameraCredential` (encrypted) |
| 2 Video ingestion | `workers/sources.py`, `workers/camera_worker.py` (capture loop) |
| 3 Frame sampling | `camera_worker.py` sampling + `workers/inference.py` (bounded queues, batching) |
| 4 Shared detection | `analytics/detector.py`, `analytics/classes.py`, `config/models.yaml`, `services/model_registry.py` |
| 5 Tracking | `analytics/tracker.py`, `analytics/reid.py` |
| 6 Zones & lines | `api/zones.py`, `analytics/geometry.py`, `analytics/policy.py`, frontend `pages/Zones.tsx` |
| 7–10 Person / vehicle analytics, intrusion, restricted access | `analytics/rules.py`, `analytics/pipeline.py` |
| 11 Event engine | `analytics/event_engine.py`, `services/event_service.py` |
| 12 Evidence | `workers/evidence.py` |
| 13 Recording & storage | `workers/recorder.py`, `services/storage.py`, `services/retention.py` |
| 14 Database | `models.py` (all core tables + indexes) |
| 15 API | `api/*.py` — interactive docs at `/api/docs` |
| 16 RBAC | `core/permissions.py`, `deps.py`, per-user camera scope |
| 17 Frontend | `frontend/src/pages/*` |
| 18 Async processing | `workers/supervisor.py`, `workers/dbwriter.py`, `workers/run.py` |
| 19 Cloud | `docker-compose.yml`, `infrastructure/` |
| 20–21 Training, specialised models | `ml/` |
| 22 False-alarm control | persistence, entry-transition, hysteresis, cooldown, merge (rules + engine) |
| 23 Observability | JSON logs with request IDs, `camera_health`, System health page |
| 24–25 Scalability & evaluation | `ml/evaluation/*`, `perf_runs` table |
| 26 Tests | `backend/tests` (unit, integration, end-to-end) |

## Definitions used by the rules

**Intrusion** (after Lohani et al., Sensors 2022): an object of a non-authorised class whose ground
point is inside a protected polygon, during the protected time, having entered from outside
(configurable), observed for at least *N* consecutive analysed frames.

**Restricted-area access**: an object inside a restricted polygon whose class/time combination is
not allowed by the zone's policies. Difference from intrusion is a business rule: restricted
zones are about *who may be where and when* (e.g. no vehicles on the footpath, staff-only area);
they don't require the object to have crossed in from outside.

**Policy evaluation** (`analytics/policy.py`): matching *allow* rules win, then *alert* rules,
then the zone's default. Schedules support overnight windows.

**Unique counting**: one count per identity per direction per line, and one "seen" count per
identity. When a track is confirmed, a few OSNet x0.25 (MSMT17) appearance embeddings are collected
(`analytics/reid.py`, ONNX run by OpenCV DNN, ~15 ms per crop on a laptop CPU) and compared with the
identities the camera already knows:
short-term (≤ 8 s gap, near where the object was lost, similarity ≥ 0.65) for occlusions, and
long-term (anywhere in the frame, up to `reid_memory_seconds`, default 30 min, similarity ≥ 0.75) for
somebody who left and came back. A match inherits the earlier identity and is not counted again.
Identities (with their appearance, float16) are stored in the `identities` table the moment they
are decided, so counts update live and the memory survives a restart. People are never matched
across cameras. On the college gate video, replaying the same 5 minutes a second time added 1
person (40 -> 41), where the previous colour-histogram re-ID counted everyone again (47 -> 94).

**Rider suppression**: a person box whose feet lie on a two-wheeler/bicycle box is a rider, not a
pedestrian (UVH-26 labels include the rider in the two-wheeler box).

## Permission matrix

| Permission | admin | operator | zone_manager (add-on) | viewer |
|---|---|---|---|---|
| users:manage, audit:view, settings:manage, models:manage | ✓ | | | |
| cameras:manage | ✓ | | | |
| cameras:view, live:view, recordings:view, zones:view, events:view, analytics:view | ✓ | ✓ | (cameras, zones) | ✓ |
| streams:control, events:update, system:view | ✓ | ✓ | | |
| zones:manage | ✓ | | ✓ | |

Users can additionally be limited to specific cameras (`camera_scope`); cameras outside the scope
return 404, not 403, so their existence isn't revealed.

## Storage keys

```
recordings/{camera_id}/{YYYY-MM-DD}/{YYYYmmdd-HHMMSS}.mp4
evidence/{camera_id}/{YYYY-MM-DD}/{event_id}/clip.mp4
snapshots/{camera_id}/{YYYY-MM-DD}/{event_id}/snapshot.jpg
thumbnails/{camera_id}/latest.jpg
```

Keys are generated by the backend and validated against a whitelist pattern; the database stores
keys and metadata (size, duration, SHA-256, retention date), never video bytes.

## Known limitations (state these honestly in the report)

* Counting accuracy depends on camera angle: a line where people cross clearly works far better
  than one where crowds overlap. Measure it on your footage (`eval_events.py` with manual counts).
* Appearance re-ID can merge two people wearing very similar clothes (e.g. uniforms), which
  under-counts; raise `reid_long_threshold` or lower `reid_memory_seconds` if that happens.
  OSNet is trained on people; vehicles are matched with the same features, which works less well.
* The on-site estimate drifts when people leave through gates without cameras.
* SQLite is fine for one machine; use PostgreSQL for several workers.
* Live preview shows the analysed frames, fetched one at a time by the dashboard (`/api/live/{id}/frame`,
  long-poll; an MJPEG endpoint also exists for other clients) — smooth enough at 5–8 fps; for full-frame-rate raw
  video use MediaMTX (HLS/WebRTC) and fill in the camera's raw stream URL.
* Timestamps of uploaded video files are the replay time (they behave like a live camera).
