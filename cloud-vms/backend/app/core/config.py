"""Application settings.

Everything configurable lives here and is read from environment variables
(or a `.env` file at the project root). Nothing environment-specific is
hard-coded in business logic.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> project root is three levels up from app/
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"), env_prefix="VMS_", extra="ignore"
    )

    app_name: str = "Cloud VMS"
    environment: Literal["development", "production", "test"] = "development"

    # ---- paths -----------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"
    models_config: Path = PROJECT_ROOT / "config" / "models.yaml"
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"

    # ---- security --------------------------------------------------------
    secret_key: str = ""  # generated and persisted to data/secret.key if empty
    jwt_ttl_minutes: int = 8 * 60
    stream_token_ttl_seconds: int = 300  # short-lived tokens for MJPEG / SSE
    media_url_ttl_seconds: int = 900  # signed evidence / playback links
    admin_username: str = "admin"
    admin_password: str = ""  # random if empty; written to data/initial_admin_password.txt
    login_rate_limit_per_minute: int = 10
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    # only honour X-Forwarded-For from these proxy addresses (e.g. ["127.0.0.1"] behind nginx)
    trusted_proxies: list[str] = Field(default_factory=list)

    # ---- upload limits (MB) -----------------------------------------------
    max_video_upload_mb: int = 4096
    max_dataset_upload_mb: int = 4096
    max_dataset_unzipped_mb: int = 16384  # guards against zip bombs
    max_weights_upload_mb: int = 1024

    # ---- database --------------------------------------------------------
    database_url: str = ""  # default: sqlite file in data_dir

    # ---- object storage --------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str = ""
    s3_region: str = "ap-south-1"
    s3_endpoint_url: str = ""  # e.g. http://localhost:9000 for MinIO
    s3_prefix: str = ""

    # ---- workers ---------------------------------------------------------
    embedded_workers: bool = True  # run camera workers inside the API process
    worker_group: str = "default"
    redis_url: str = ""  # enables cross-process live preview frames
    supervisor_interval_seconds: float = 3.0
    health_interval_seconds: float = 5.0
    camera_offline_after_seconds: float = 15.0
    device: str = "auto"  # auto | cpu | cuda | cuda:0 | mps
    inference_batch_size: int = 4
    inference_threads: int = 1

    # ---- time -----------------------------------------------------------
    timezone: str = "Asia/Kolkata"  # used for schedules and hourly statistics

    # ---- retention (days) ------------------------------------------------
    retention_recordings_days: int = 7
    retention_evidence_days: int = 90
    retention_tracks_days: int = 30
    retention_health_days: int = 7
    store_raw_detections: bool = False

    # ---- notifications ---------------------------------------------------
    notify_webhook_url: str = ""
    notify_min_severity: Literal["low", "medium", "high", "critical"] = "medium"

    # ---- helpers ---------------------------------------------------------
    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def weights_dir(self) -> Path:
        return self.data_dir / "weights"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'vms.db').as_posix()}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.storage_dir, self.uploads_dir, self.weights_dir,
                  self.tmp_dir, self.datasets_dir):
            d.mkdir(parents=True, exist_ok=True)

    def resolved_secret(self) -> str:
        if self.secret_key:
            return self.secret_key
        self.ensure_dirs()
        f = self.data_dir / "secret.key"
        if not f.exists():
            f.write_text(secrets.token_urlsafe(48))
        return f.read_text().strip()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
