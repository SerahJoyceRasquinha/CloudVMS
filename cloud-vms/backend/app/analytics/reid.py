"""Re-identification so each person / vehicle is counted once.

Without re-ID, somebody who walks behind a bus, or leaves the frame and comes
back a minute later, gets a new tracker ID and is counted again. When a new
track has been observed for a moment, its appearance is compared with the
identities this camera has already seen; if it matches, the track inherits
that identity (``root_uid``) and is not counted a second time.

Embedders
* ``osnet`` (default): OSNet x0.25 (Zhou et al., ICCV 2019) trained for person
  re-ID on MSMT17, run as ONNX through OpenCV's DNN module (no extra Python
  packages, ~1 MB of weights, ~15 ms per crop on a laptop CPU). Weights are
  downloaded once to ``data/weights/osnet_x0_25_msmt17.onnx``.
* ``histogram``: HSV colour histograms of the upper and lower half of the box.
  No weights needed, but only reliable for a few seconds at the same spot, so
  with this backend only short-term matching is done.

Matching happens in two tiers:
* short term (``window_seconds``, near where the old track ended): an object
  that was occluded for a moment; a looser threshold is safe here;
* long term (``memory_seconds``, anywhere in the frame): an object that left
  and came back. This needs a confident, unambiguous match (deep embedder only).

Re-ID links appearances within one camera. It does not identify who a person is.
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("vms.reid")

OSNET_FILE = "osnet_x0_25_msmt17.onnx"
OSNET_REPO = "anriha/osnet_x0_25_msmt17"  # MIT-licensed ONNX export of the torchreid weights
_download_lock = threading.Lock()


def _crop(frame: np.ndarray, bbox, min_h: int = 8, min_w: int = 4) -> np.ndarray | None:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    x1, y1 = int(max(0, x1)), int(max(0, y1))
    x2, y2 = int(min(w, x2)), int(min(h, y2))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < min_h or crop.shape[1] < min_w:
        return None
    return crop


class HistogramEmbedder:
    name = "histogram"
    deep = False

    def embed(self, frame: np.ndarray, bbox) -> np.ndarray | None:
        crop = _crop(frame, bbox)
        if crop is None:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        h = hsv.shape[0]
        feats = []
        for part in (hsv[: h // 2], hsv[h // 2:]):
            hist = cv2.calcHist([part], [0, 1, 2], None, [8, 6, 4], [0, 180, 0, 256, 0, 256])
            feats.append(hist.flatten())
        v = np.concatenate(feats).astype(np.float32)
        v = np.sqrt(v)  # Hellinger mapping
        n = np.linalg.norm(v)
        return v / n if n > 0 else None


def osnet_weights_path() -> Path:
    from ..core.config import get_settings
    return get_settings().weights_dir / OSNET_FILE


def ensure_osnet_weights() -> Path:
    """Return the ONNX weights, downloading them once from Hugging Face if missing."""
    path = osnet_weights_path()
    if path.exists() and path.stat().st_size > 100_000:
        return path
    with _download_lock:
        if path.exists() and path.stat().st_size > 100_000:
            return path
        import shutil
        from huggingface_hub import hf_hub_download
        log.info("downloading OSNet re-ID weights (%s) ...", OSNET_REPO)
        src = hf_hub_download(OSNET_REPO, OSNET_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        shutil.copyfile(src, tmp)
        tmp.replace(path)
    return path


class OSNetEmbedder:
    """OSNet x0.25 / MSMT17 person re-ID features (512-d, L2-normalised) through cv2.dnn."""
    name = "osnet"
    deep = True
    _MEAN = np.array([0.485, 0.456, 0.406], np.float32)
    _STD = np.array([0.229, 0.224, 0.225], np.float32)

    def __init__(self, weights: str | Path | None = None):
        path = Path(weights) if weights else ensure_osnet_weights()
        # one Net per camera pipeline: cv2.dnn networks are not thread-safe
        self.net = cv2.dnn.readNetFromONNX(str(path))

    def _blob(self, crop: np.ndarray) -> np.ndarray:
        img = cv2.resize(crop, (128, 256), interpolation=cv2.INTER_LINEAR)
        img = img[:, :, ::-1].astype(np.float32) / 255.0  # BGR -> RGB
        img = (img - self._MEAN) / self._STD
        return img.transpose(2, 0, 1)[None]

    def embed(self, frame: np.ndarray, bbox) -> np.ndarray | None:
        crop = _crop(frame, bbox, min_h=16, min_w=8)
        if crop is None:
            return None
        self.net.setInput(self._blob(crop))
        f = self.net.forward().reshape(-1).astype(np.float32)
        n = np.linalg.norm(f)
        return f / n if n > 0 else None


def make_embedder(backend: str, device: str = "cpu"):
    if backend == "osnet":
        try:
            return OSNetEmbedder()
        except Exception as exc:  # no internet on first run, corrupt file, ...
            log.warning("OSNet re-ID unavailable (%s); falling back to colour histograms", exc)
    return HistogramEmbedder()


@dataclass
class Identity:
    root_uid: int
    group: str
    last_ts: float
    end_xy: tuple[float, float]
    protos: list = field(default_factory=list)  # a few appearance prototypes (different poses)
    active: bool = True

    def similarity(self, emb: np.ndarray) -> float:
        return max(float(np.dot(p, emb)) for p in self.protos) if self.protos else -1.0


class ReIdentifier:
    """Per-camera gallery of identities seen so far."""

    def __init__(self, embedder, window_seconds: float = 8.0, threshold: float = 0.8,
                 max_distance_frac: float = 0.35, memory_seconds: float = 900.0,
                 long_threshold: float | None = None, max_identities: int = 3000, protos_per_identity: int = 6):
        self.embedder = embedder
        self.window = window_seconds
        self.threshold = threshold
        self.max_distance_frac = max_distance_frac
        # long-term matching is only trustworthy with a deep re-ID embedder
        self.memory = memory_seconds if getattr(embedder, "deep", False) else 0.0
        self.long_threshold = long_threshold if long_threshold is not None else max(threshold, 0.75)
        self.max_identities = max_identities
        self.protos_per_identity = protos_per_identity
        self.identities: dict[int, Identity] = {}

    # ------------------------------------------------------------ gallery upkeep
    def observe(self, root_uid: int, group: str, ts: float, bbox, emb: np.ndarray | None) -> None:
        """Add an appearance sample of a (decided) identity while it is being tracked."""
        ident = self.identities.get(root_uid)
        if ident is None:
            ident = self.identities[root_uid] = Identity(root_uid, group, ts, _foot(bbox))
            self._evict()
        ident.last_ts, ident.end_xy, ident.active = ts, _foot(bbox), True
        if emb is None:
            return
        if not ident.protos:
            ident.protos.append(emb)
        elif ident.similarity(emb) < 0.92:  # keep diverse views (front, side, back ...)
            ident.protos.append(emb)
            if len(ident.protos) > self.protos_per_identity:
                ident.protos.pop(1)  # keep the first view, drop the oldest of the rest
        else:  # refresh the closest prototype slowly
            i = int(np.argmax([float(np.dot(p, emb)) for p in ident.protos]))
            ident.protos[i] = _normalise(0.8 * ident.protos[i] + 0.2 * emb)

    def release(self, root_uid: int, ts: float, bbox) -> None:
        """The last track of this identity ended: it becomes a re-identification candidate."""
        ident = self.identities.get(root_uid)
        if ident is not None:
            ident.active, ident.last_ts, ident.end_xy = False, ts, _foot(bbox)

    def _evict(self) -> None:
        if len(self.identities) <= self.max_identities:
            return
        idle = sorted((i for i in self.identities.values() if not i.active), key=lambda i: i.last_ts)
        for i in idle[: len(self.identities) - self.max_identities]:
            self.identities.pop(i.root_uid, None)

    def snapshot(self, root_uid: int) -> tuple[float, list[np.ndarray]] | None:
        ident = self.identities.get(root_uid)
        return (ident.last_ts, list(ident.protos)) if ident is not None and ident.protos else None

    def restore(self, root_uid: int, group: str, last_ts: float, protos: list[np.ndarray]) -> None:
        """Re-load an identity remembered before a restart (long-term matching only: position unknown)."""
        if protos and root_uid not in self.identities:
            self.identities[root_uid] = Identity(root_uid, group, last_ts, (-1e9, -1e9), protos, active=False)

    def prune(self, now: float) -> None:
        horizon = max(self.window, self.memory)
        for uid in [u for u, i in self.identities.items() if not i.active and now - i.last_ts > horizon]:
            self.identities.pop(uid, None)

    # ------------------------------------------------------------ matching
    def match(self, group: str, start_ts: float, start_xy: tuple[float, float], emb, frame_diag: float,
              exclude_roots: set[int] | None = None) -> int | None:
        """Best earlier identity for a new track, or None if it is somebody new.
        ``exclude_roots`` are identities visible right now (one object can't be in two places)."""
        if emb is None:
            return None
        long_best, long_sim = None, self.long_threshold
        short_best, short_sim = None, self.threshold
        for ident in self.identities.values():
            if ident.group != group or not ident.protos:
                continue
            if exclude_roots and ident.root_uid in exclude_roots:
                continue
            gap = start_ts - ident.last_ts
            if gap < -1.0:
                continue
            sim = ident.similarity(emb)
            if gap <= self.window and math.dist(start_xy, ident.end_xy) <= self.max_distance_frac * frame_diag:
                if sim > short_sim:
                    short_best, short_sim = ident.root_uid, sim
            elif gap <= self.memory and sim > long_sim:
                long_best, long_sim = ident.root_uid, sim
        return short_best if short_best is not None else long_best


def encode_protos(protos: list[np.ndarray]) -> str:
    """Compact text form of appearance prototypes for the database (float16, ~1 KB per prototype)."""
    import base64
    arr = np.stack(protos).astype(np.float16)
    return f"{arr.shape[0]}x{arr.shape[1]}:" + base64.b64encode(arr.tobytes()).decode("ascii")


def decode_protos(text: str) -> list[np.ndarray]:
    import base64
    shape, _, data = text.partition(":")
    n, d = (int(v) for v in shape.split("x"))
    arr = np.frombuffer(base64.b64decode(data), dtype=np.float16).reshape(n, d).astype(np.float32)
    return [_normalise(v) for v in arr]


def _foot(bbox) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2, y2)


def _normalise(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v
