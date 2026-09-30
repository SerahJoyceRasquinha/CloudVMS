"""Draw zones, lines, tracks and a small HUD onto frames for live preview and snapshots."""
from __future__ import annotations

import cv2
import numpy as np

from .geometry import to_pixels
from .rules import ZoneSpec
from .datatypes import TrackView

ZONE_COLORS = {  # BGR
    "intrusion": (60, 60, 230),
    "restricted": (40, 140, 255),
    "authorized": (90, 200, 90),
    "monitoring": (230, 180, 60),
    "counting": (200, 120, 220),
    "line": (0, 220, 255),
}
GROUP_COLORS = {"person": (80, 220, 80), "vehicle": (255, 160, 40)}


def _label(img, text, org, color, scale=0.5):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x, y = int(org[0]), int(org[1])
    cv2.rectangle(img, (x, y - th - 6), (x + tw + 6, y), color, -1)
    cv2.putText(img, text, (x + 3, y - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, (20, 20, 20), 1, cv2.LINE_AA)


def draw_zones(img: np.ndarray, zones: list[ZoneSpec], occupancy: dict | None = None,
               line_totals: dict | None = None) -> None:
    h, w = img.shape[:2]
    overlay = img.copy()
    for z in zones:
        if not z.enabled:
            continue
        pts = np.array(to_pixels(z.points, w, h), dtype=np.int32)
        color = ZONE_COLORS.get("line" if z.shape == "line" else z.zone_type, (200, 200, 200))
        if z.shape == "line":
            a, b = tuple(pts[0]), tuple(pts[1])
            cv2.line(img, a, b, color, 3, cv2.LINE_AA)
            # arrow showing the "in" direction (perpendicular to the line)
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            dx, dy = b[0] - a[0], b[1] - a[1]
            n = max(1.0, (dx * dx + dy * dy) ** 0.5)
            sign = -1 if str((z.config or {}).get("in_side", "positive")) in ("negative", "right") else 1
            nx, ny = -dy / n * sign, dx / n * sign  # normal pointing to the "in" side
            cv2.arrowedLine(img, (int(mx), int(my)), (int(mx + nx * 40), int(my + ny * 40)), color, 2,
                            tipLength=0.35)
            t = (line_totals or {}).get(z.id, {})
            _label(img, f"{z.name}  in {t.get('in', 0)} / out {t.get('out', 0)}", (a[0], a[1] - 4), color)
        else:
            cv2.fillPoly(overlay, [pts], color)
            cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)
            occ = (occupancy or {}).get(z.id)
            _label(img, z.name + (f" ({occ})" if occ else ""), tuple(pts[0]), color)
    cv2.addWeighted(overlay, 0.18, img, 0.82, 0, dst=img)


def draw_tracks(img: np.ndarray, tracks: list[TrackView], highlight: set[int] | None = None) -> None:
    for t in tracks:
        x1, y1, x2, y2 = (int(v) for v in t.bbox)
        color = (40, 40, 255) if highlight and t.uid in highlight else GROUP_COLORS.get(t.group, (200, 200, 200))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        name = t.cls.replace("_", "-")
        _label(img, f"#{t.root_uid % 10000} {name} {t.confidence:.2f}", (x1, y1), color, 0.45)  # ids are long
        ax, ay = t.anchor()
        cv2.circle(img, (int(ax), int(ay)), 3, color, -1)


def draw_hud(img: np.ndarray, lines: list[str]) -> None:
    y = 22
    for s in lines:
        cv2.putText(img, s, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, s, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        y += 22


def resize_to_width(img: np.ndarray, width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w <= width:
        return img
    return cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)


def encode_jpeg(img: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""
