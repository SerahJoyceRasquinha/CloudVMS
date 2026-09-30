"""Model registry, detector profile, datasets (incl. college data) and training jobs."""
from __future__ import annotations

import re
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.classes import CANONICAL_CLASSES, normalize_label
from ..core.config import get_settings
from ..core.errors import bad_request, not_found
from ..db import get_db
from ..deps import client_ip, require
from ..models import Dataset, ModelVersion, TrainingJob, User
from ..schemas import DatasetOut, ModelIn, ModelOut, TrainingIn, TrainingOut
from ..services.audit import audit, bump_models_version, get_setting, set_setting
from ..services.model_registry import activate, model_is_available
from ..services.storage import UploadTooLarge, save_upload
from ..services.training import import_dataset_async, start_training

router = APIRouter(tags=["models"])


def _model_out(m: ModelVersion) -> ModelOut:
    out = ModelOut.model_validate(m)
    out.available = model_is_available(m)
    return out


@router.get("/models")
def list_models(user: User = Depends(require("system:view")), db: Session = Depends(get_db)):
    return {"profile": get_setting(db, "detector_profile"),
            "canonical_classes": sorted(CANONICAL_CLASSES),
            "models": [_model_out(m) for m in db.scalars(select(ModelVersion).order_by(ModelVersion.role,
                                                                                         ModelVersion.id))]}


@router.post("/models", response_model=ModelOut, status_code=201)
def register_model(body: ModelIn, request: Request, user: User = Depends(require("models:manage")),
                   db: Session = Depends(get_db)):
    if db.scalar(select(ModelVersion).where(ModelVersion.name == body.name)):
        raise bad_request("A model with this name already exists")
    bad = [v for v in body.class_map.values() if normalize_label(v) not in CANONICAL_CLASSES]
    if bad:
        raise bad_request(f"class_map targets must be canonical classes ({', '.join(sorted(CANONICAL_CLASSES))}); "
                          f"got {bad}")
    m = ModelVersion(name=body.name, role=body.role, weights=body.weights,
                     class_map={k: normalize_label(v) for k, v in body.class_map.items()},
                     source="registered", dataset=body.dataset, notes=body.notes)
    db.add(m)
    db.flush()
    audit(db, user, "model.register", "model", m.id, {"name": m.name, "weights": m.weights}, client_ip(request))
    return _model_out(m)


@router.post("/models/{model_id}/activate", response_model=ModelOut)
def activate_model(model_id: int, request: Request, user: User = Depends(require("models:manage")),
                   db: Session = Depends(get_db)):
    m = db.get(ModelVersion, model_id)
    if m is None:
        raise not_found("Model")
    if not model_is_available(m):
        raise bad_request(f"Weights not found: {m.weights}. Download or upload them first.")
    activate(db, m)
    bump_models_version(db)  # workers reload the detector on their next tick
    audit(db, user, "model.activate", "model", m.id, {"name": m.name, "role": m.role}, client_ip(request))
    return _model_out(m)


@router.put("/models/profile")
def set_profile(profile: str, request: Request, user: User = Depends(require("settings:manage")),
                db: Session = Depends(get_db)):
    if profile not in ("shared", "specialized"):
        raise bad_request("profile must be 'shared' or 'specialized'")
    set_setting(db, "detector_profile", profile)
    bump_models_version(db)
    audit(db, user, "settings.detector_profile", "settings", "detector_profile", {"profile": profile},
          client_ip(request))
    return {"profile": profile}


@router.post("/models/upload-weights", response_model=ModelOut, status_code=201)
async def upload_weights(request: Request, file: UploadFile = File(...), name: str = Form(...),
                         role: str = Form(...), class_map: str = Form("{}"),
                         user: User = Depends(require("models:manage")), db: Session = Depends(get_db)):
    """Upload .pt weights trained elsewhere (Kaggle / Colab / EC2) and register them."""
    import json
    if not (file.filename or "").endswith(".pt"):
        raise bad_request("Upload an Ultralytics .pt file")
    s = get_settings()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")[:80]
    if not safe:
        raise bad_request("Give the model a name")
    if db.scalar(select(ModelVersion).where(ModelVersion.name == safe)):
        raise bad_request("A model with this name already exists")
    dst = s.weights_dir / f"{safe}.pt"
    if dst.exists():  # never replace weights another model (e.g. the active yolo26n.pt) may be using
        raise bad_request(f"A weights file named {dst.name} already exists; choose another name")
    # write to a temporary file first so a failed upload never leaves a half-written file behind
    tmp = s.tmp_dir / f"upload_{uuid.uuid4().hex}.pt"
    try:
        save_upload(file.file, tmp, s.max_weights_upload_mb)
    except UploadTooLarge as exc:
        raise bad_request(str(exc))
    try:
        cmap = json.loads(class_map) if class_map.strip() else {}
        if not cmap:
            from ultralytics import YOLO
            names = YOLO(str(tmp)).names
            cmap = {str(n): normalize_label(n) for n in names.values() if normalize_label(n) in CANONICAL_CLASSES}
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise bad_request(f"Could not read the weights / class map: {exc}")
    if not cmap:
        tmp.unlink(missing_ok=True)
        raise bad_request("None of the model's labels match canonical classes; provide a class_map")
    shutil.move(str(tmp), dst)
    try:
        return register_model(ModelIn(name=safe, role=role, weights=str(dst), class_map=cmap,
                                      notes="uploaded weights"), request, user, db)
    except Exception:
        dst.unlink(missing_ok=True)  # safe: we checked above that dst did not exist before
        raise


# ------------------------------------------------------------------ datasets
@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(user: User = Depends(require("models:manage")), db: Session = Depends(get_db)):
    return db.scalars(select(Dataset).order_by(Dataset.id.desc())).all()


@router.post("/datasets/upload", response_model=DatasetOut, status_code=201)
async def upload_dataset(request: Request, file: UploadFile = File(...), name: str = Form(...),
                         user: User = Depends(require("models:manage")), db: Session = Depends(get_db)):
    """Upload a labelled dataset (.zip of images + YOLO labels, e.g. exported from CVAT / Roboflow / Label Studio)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:60]
    if not safe:
        raise bad_request("Give the dataset a name")
    if db.scalar(select(Dataset).where(Dataset.name == safe)):
        raise bad_request("A dataset with this name already exists")
    if not (file.filename or "").lower().endswith(".zip"):
        raise bad_request("Upload a .zip file")
    s = get_settings()
    root = s.datasets_dir / safe
    raw = root / "_raw"
    raw.mkdir(parents=True, exist_ok=True)
    zpath = root / "upload.zip"
    try:
        save_upload(file.file, zpath, s.max_dataset_upload_mb)
        with zipfile.ZipFile(zpath) as z:
            for member in z.namelist():  # zip-slip protection
                target = (raw / member).resolve()
                if raw.resolve() not in target.parents and target != raw.resolve():
                    raise bad_request("Zip contains unsafe paths")
            unzipped = sum(i.file_size for i in z.infolist())  # zip-bomb protection
            if unzipped > s.max_dataset_unzipped_mb * 1024 * 1024:
                raise bad_request(f"Zip expands to more than {s.max_dataset_unzipped_mb} MB")
            z.extractall(raw)
    except UploadTooLarge as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise bad_request(str(exc))
    except zipfile.BadZipFile:
        shutil.rmtree(root, ignore_errors=True)
        raise bad_request("Not a valid zip file")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    zpath.unlink(missing_ok=True)
    ds = Dataset(name=safe, kind="detection", path=str(root), status="processing")
    db.add(ds)
    db.flush()
    audit(db, user, "dataset.upload", "dataset", ds.id, {"name": safe}, client_ip(request))
    import_dataset_async(ds.id, raw)
    return ds


@router.post("/datasets/register", response_model=DatasetOut, status_code=201)
def register_dataset(name: str, data_yaml: str, request: Request, user: User = Depends(require("models:manage")),
                     db: Session = Depends(get_db)):
    """Register a dataset already prepared on the server (e.g. by ml/datasets/prepare_*.py)."""
    p = Path(data_yaml)
    if not p.exists() or p.suffix not in (".yaml", ".yml"):
        raise bad_request("data_yaml must be the path of an existing YOLO data.yaml")
    ds = Dataset(name=name, kind="detection", path=str(p), status="ready", stats={"data_yaml": str(p)})
    db.add(ds)
    db.flush()
    audit(db, user, "dataset.register", "dataset", ds.id, {"name": name, "path": str(p)}, client_ip(request))
    return ds


# ------------------------------------------------------------------ training
@router.get("/training-jobs", response_model=list[TrainingOut])
def list_jobs(user: User = Depends(require("models:manage")), db: Session = Depends(get_db)):
    return db.scalars(select(TrainingJob).order_by(TrainingJob.id.desc()).limit(50)).all()


@router.post("/training-jobs", response_model=TrainingOut, status_code=201)
def create_job(body: TrainingIn, request: Request, user: User = Depends(require("models:manage")),
               db: Session = Depends(get_db)):
    ds = db.get(Dataset, body.dataset_id)
    if ds is None or ds.status != "ready":
        raise bad_request("Dataset not found or not ready")
    if db.scalar(select(TrainingJob).where(TrainingJob.status.in_(["queued", "running"]))):
        raise bad_request("A training job is already running; wait for it to finish")
    job = TrainingJob(dataset_id=ds.id, base_model=body.base_model, role=body.role,
                      params={"epochs": body.epochs, "imgsz": body.imgsz, "batch": body.batch, "device": body.device})
    db.add(job)
    db.flush()
    audit(db, user, "training.start", "training_job", job.id, body.model_dump(), client_ip(request))
    db.commit()
    start_training(job.id)
    return job


@router.get("/training-jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: int, user: User = Depends(require("models:manage")), db: Session = Depends(get_db)):
    job = db.get(TrainingJob, job_id)
    if job is None:
        raise not_found("Training job")
    p = Path(job.log_path) if job.log_path else None
    return p.read_text(errors="ignore")[-20000:] if p and p.exists() else "(no output yet)"
