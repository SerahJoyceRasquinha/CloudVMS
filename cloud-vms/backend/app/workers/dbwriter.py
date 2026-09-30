"""Batches high-frequency writes (crossings, track summaries, raw detections) to the database."""
from __future__ import annotations

import logging
import queue
import threading

from sqlalchemy import select, update

from ..core.timeutil import from_epoch
from ..db import session_scope
from ..models import Camera, Crossing, Detection, Identity, Track

log = logging.getLogger("vms.dbwriter")


class DbWriter:
    def __init__(self, flush_seconds: float = 1.0):
        self.q: queue.Queue = queue.Queue(maxsize=50000)
        self.flush_seconds = flush_seconds
        self._stop = threading.Event()
        self.dropped = 0
        self._t = threading.Thread(target=self._loop, name="db-writer", daemon=True)
        self._t.start()

    def put(self, obj) -> None:
        try:
            self.q.put_nowait(obj)
        except queue.Full:
            self.dropped += 1

    def crossing(self, camera_id: int, c) -> None:
        self.put(Crossing(camera_id=camera_id, zone_id=c.zone_id, track_uid=c.track_uid, root_uid=c.root_uid,
                          object_class=c.cls, object_group=c.group, subtype=c.subtype or "",
                          direction=c.direction, ts=from_epoch(c.ts)))

    def track(self, camera_id: int, s) -> None:
        if s.hits < 2:
            return
        self.put(Track(camera_id=camera_id, track_uid=s.uid, root_uid=s.root_uid, object_class=s.cls,
                       object_group=s.group, subtype=s.subtype or "", first_seen_at=from_epoch(s.first_ts),
                       last_seen_at=from_epoch(s.last_ts), observations=s.hits,
                       track_confidence=s.mean_confidence, trajectory=s.trajectory))

    def identity(self, camera_id: int, rec) -> None:
        self.put(Identity(camera_id=camera_id, root_uid=rec.root_uid, object_class=rec.cls,
                          object_group=rec.group, subtype=rec.subtype or "", first_seen_at=from_epoch(rec.first_ts)))

    def remap(self, camera_id: int, track_uid: int, root_uid: int) -> None:
        """A track was re-identified as an earlier object: its crossings belong to that object."""
        self.put(("remap", camera_id, track_uid, root_uid))

    def remember(self, camera_id: int, root_uid: int, embedder: str, last_ts: float, protos) -> None:
        """Store an identity's appearance so re-identification survives a restart."""
        from ..analytics.reid import encode_protos
        self.put(("remember", camera_id, root_uid, embedder, from_epoch(last_ts), encode_protos(protos[:6])))

    def detection(self, camera_id: int, ts: float, d, model_version: str) -> None:
        self.put(Detection(camera_id=camera_id, frame_ts=from_epoch(ts), class_name=d.cls,
                           confidence=round(d.confidence, 3), bbox=[round(v, 1) for v in d.bbox],
                           model_version=model_version))

    def _drain(self) -> list:
        items = []
        try:
            while len(items) < 5000:
                items.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return items

    def flush(self) -> int:
        items = self._drain()
        if not items:
            return 0
        try:
            with session_scope() as db:
                # a camera deleted meanwhile: its leftover rows would come back into the statistics
                cams = {it[1] if isinstance(it, tuple) else it.camera_id for it in items}
                alive = set(db.scalars(select(Camera.id).where(Camera.id.in_(cams))))
                items = [it for it in items if (it[1] if isinstance(it, tuple) else it.camera_id) in alive]
                rows = []
                for it in items:  # keep queue order: a remap must see the crossings queued before it
                    if isinstance(it, tuple):
                        db.add_all(rows)
                        rows = []
                        db.flush()
                        if it[0] == "remap":
                            _, cam, track_uid, root_uid = it
                            db.execute(update(Crossing).where(Crossing.camera_id == cam,
                                                              Crossing.track_uid == track_uid)
                                       .values(root_uid=root_uid))
                        elif it[0] == "remember":
                            _, cam, root_uid, embedder, last_seen, appearance = it
                            db.execute(update(Identity).where(Identity.camera_id == cam, Identity.root_uid == root_uid)
                                       .values(embedder=embedder, last_seen_at=last_seen, appearance=appearance))
                    else:
                        rows.append(it)
                db.add_all(rows)
        except Exception:
            log.exception("failed to write %d rows", len(items))
        return len(items)

    def _loop(self) -> None:
        while not self._stop.wait(self.flush_seconds):
            self.flush()
        self.flush()

    def stop(self) -> None:
        self._stop.set()
        self._t.join(timeout=5)
