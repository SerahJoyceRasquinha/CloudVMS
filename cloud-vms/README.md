# Gatehouse: Cloud-Based Video Management System with Automated Incident Detection and Scalable Video Analytics

A complete video management system for a campus gate: register cameras (or upload recorded
footage), watch them live, record continuously, and let AI detect and track **people** and
**vehicles**. The system counts **unique entries and exits** and raises **intrusion** and
**restricted-area access** incidents with video evidence. Everything is searchable from a web
dashboard with role-based access. It runs on one Windows laptop, and the same code scales out
to AWS (EC2 + RDS + S3).

```
cameras ─► ingestion ─► frame sampling ─► shared detector ─► tracker ─► zone/line rules ─► event engine
   │                                        (people +             (unique IDs,          (dedup, evidence,
   └─► FFmpeg recorder ─► object storage      Indian vehicles)      re-ID)                 alerts)
                                                                                           │
                                               React dashboard ◄── FastAPI ◄── PostgreSQL/SQLite
```

## Quick start (Windows)

1. Extract the whole zip (right-click → *Extract All*).
2. Double-click **`RUN_VMS.bat`**. That's all. On every launch it:
   * runs **`install_prerequisites.bat`**, which **checks each prerequisite first and installs only
     what is missing**: Python 3.12 (if no Python 3.10–3.12 is found), the Microsoft Visual C++
     runtime (PyTorch needs it), Google Chrome, Node.js (only if `frontend\dist` is missing), the
     `.venv` environment, then — through `scripts\check_setup.py` — the Python packages, the YOLO26
     weights, the optional IISc UVH-26 model, the demo video and `.env`. Installs use `winget`, or
     the official installers if `winget` is unavailable. When everything is present this takes a
     few seconds and installs nothing;
   * starts the server in a window called *Cloud VMS server* (close it to stop the system);
   * waits until the server answers and opens the dashboard in a new Google Chrome tab
     (<http://localhost:8000>), or your default browser if Chrome can't be installed.

   The first run needs internet and downloads about 1 GB (mostly PyTorch). Windows may ask for
   permission when installing the Visual C++ runtime or Chrome. The first admin password is shown
   in the launcher and saved in `data\initial_admin_password.txt`.

Linux/macOS: `./run_vms.sh` (same checks via `install_prerequisites.sh`; press Ctrl+C to stop).

### First 10 minutes

1. **Cameras → Add camera → Recorded video** and upload a college gate video (or pick
   `demo_gate.mp4`). For a real camera choose *IP camera (RTSP)*. Put the username and password
   in their own fields, not in the URL.
2. **Zones & rules**: choose *Counting line*, click two points across the gate path, and check that
   the arrow points *into* campus (use *Flip entry direction* if it doesn't). Entries and exits
   now count on the **Gate overview**.
3. Add a **Restricted area** (e.g. footpath: *no vehicles*, or staff-only: *people*) and an
   **Intrusion area** (perimeter, armed 20:00–06:00).
4. Watch **Live view**. Each incident appears as a toast and in **Incidents**, with a snapshot and
   a short clip (a few seconds before and after). Acknowledge, investigate or resolve it with notes.

## What is implemented

| Requirement | Where |
|---|---|
| Camera registration & management, health, encrypted credentials | Cameras page · `api/cameras.py` |
| Live streaming | Live view (annotated frames, long-poll) · optional raw HLS/WebRTC through MediaMTX |
| Recording, cloud storage, playback | FFmpeg segmenter → local disk or **S3**; Recordings page |
| Person detection | YOLO26 (COCO); CrowdHuman fine-tuning path |
| Vehicle detection (Indian classes) | IISc **UVH-26** YOLOv11 model (auto-rickshaw, two-wheeler, tempo …) or COCO fallback |
| Unique counts of people / vehicles entering and leaving | ByteTrack-style tracker + OSNet appearance re-ID (a person who leaves and returns counts once) + counting lines |
| Intrusion detection | Polygon + schedule + entry-from-outside rule (perimeter-intrusion definition) |
| Restricted-area access | Per-zone allow/alert policies by object type and time |
| Event management, searchable metadata, alerts | Incidents page, filters, CSV export, live toasts (SSE), webhook |
| Role-based access control | admin / operator / viewer / zone_manager + per-user camera scope, audit log |
| Scalable processing | bounded queues, cross-camera batching, worker groups, separate worker processes |
| Resource monitoring | System health page (fps, latency, dropped frames, CPU/RAM/GPU) |
| Performance & detection evaluation | `ml/evaluation`: mAP, event precision/recall, time-to-alert, 1→10 camera benchmark |
| Cloud deployment | `docker-compose.yml` (Postgres + MinIO + Redis + MediaMTX), `infrastructure/aws` |
| Add your own (college) dataset later | Models & data page → upload labelled zip → fine-tune → activate; `ml/college` |

## Datasets and models (one per requirement)

| Part | Pretrained model | Dataset for fine-tuning / evaluation |
|---|---|---|
| People | YOLO26n/s (COCO) | CrowdHuman |
| Vehicles | UVH-26 YOLOv11-S (Bengaluru CCTV) | UVH-26, Kaggle *Traffic Vehicles Object Detection* |
| Intrusion | rules on tracks | UCF-Crime (Burglary, Stealing, Vandalism), Kaggle *Burglary & Vandalism* |
| Restricted access | rules on tracks | VIRAT ground videos |
| Your campus | your fine-tuned model | college gate footage |

See [`ml/README.md`](ml/README.md) for download and conversion commands.

## Measured on the development machine

These numbers come from a 2-core cloud VM with no GPU, running YOLO26n at 640 px on the synthetic
demo video at 5 analysed frames per second per camera. Re-measure on your own hardware with
`ml\evaluation\benchmark_scaling.py`.

| Cameras | Analysed fps (target) | Mean / p95 latency | Keeps up |
|---|---|---|---|
| 1 | 5.0 (5) | 74 / 87 ms | yes |
| 2 | 10.0 (10) | 69 / 119 ms | yes |
| 5 | 19.0 (25) | 508 / 611 ms | no: CPU-bound, stale frames are dropped so latency stays bounded |
| 10 | 13.8 (50) | 600 / 712 ms | no |

On the demo video with known ground truth, the pipeline counts 3 people in and 1 out, raises
exactly 1 restricted-area incident (0.5 s after the person entered), and produces no false alarms.
This is covered by the tests. The demo is synthetic, so it is no substitute for evaluation on
real college footage.

## Project layout

```
backend/app/            FastAPI app
  analytics/            detector, tracker, re-ID, geometry, policies, rules, event engine (pure Python)
  workers/              sources, inference service, camera pipelines, recorder, evidence, supervisor
  api/  services/       REST API, storage (local/S3), stats, training, retention
backend/tests/          31 tests: unit, API integration, end-to-end pipeline, real-model smoke test
frontend/               React + TypeScript dashboard (built copy in frontend/dist)
ml/                     dataset converters, college import, training, evaluation, benchmark
config/models.yaml      detector catalogue and class mappings
infrastructure/         Dockerfile, MediaMTX config, AWS guide + IAM policy
docs/architecture.md    design, module map, definitions, permission matrix, limitations
scripts/                demo video generator
```

## Development

```bat
.venv\Scripts\activate
cd backend && python -m pytest                    & rem all tests
python -m uvicorn app.main:app --reload            & rem API on :8000
cd frontend && npm install && npm run dev          & rem UI on :5173 (proxies /api)
```

API documentation: <http://localhost:8000/api/docs>

## Limitations

Read `docs/architecture.md` → *Known limitations*. In short: counting accuracy depends on the camera
angle, re-ID works within one camera (default memory 30 minutes) and can merge look-alikes, the on-site estimate drifts, and
the benchmark numbers only hold for the machine they were measured on.
