"""Draws zones in their risk colour and the optical-flow arrows on a camera frame."""
from __future__ import annotations

import cv2
import numpy as np

from .flow import FlowAnalyzer
from .risk import LEVEL_COLORS, ZoneRisk
from .zones import Zone

ARROW_BGR = (235, 180, 90)  # light blue
NO_DATA_BGR = (160, 160, 160)


def hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return b, g, r


def draw_overlay(
    frame: np.ndarray,
    zones: list[Zone],
    risks: dict[str, ZoneRisk] | None = None,
    analyzer: FlowAnalyzer | None = None,
) -> np.ndarray:
    """Return a copy of `frame` with zone outlines, labels and flow arrows drawn on it."""
    risks = risks or {}
    out = frame.copy()
    fill = frame.copy()
    for z in zones:
        r = risks.get(z.id)
        color = hex_to_bgr(LEVEL_COLORS[r.level]) if r else NO_DATA_BGR
        cv2.fillPoly(fill, [np.int32(z.polygon)], color)
    cv2.addWeighted(fill, 0.15, out, 0.85, 0, dst=out)

    if analyzer is not None:
        draw_flow_arrows(out, analyzer)

    for z in zones:
        r = risks.get(z.id)
        color = hex_to_bgr(LEVEL_COLORS[r.level]) if r else NO_DATA_BGR
        pts = np.int32(z.polygon)
        cv2.polylines(out, [pts], True, color, 2, cv2.LINE_AA)
        label = z.name.upper()
        if r is not None:
            if r.density is not None:
                label += f"  {r.density:.1f}/m2"
            label += f"  {r.status}"
        x, y = pts[np.argmin(pts[:, 1] * 10000 + pts[:, 0])]  # top-left corner
        _label(out, label, (int(x) + 4, int(y) + 4), color)
    return out


def draw_flow_arrows(frame: np.ndarray, analyzer: FlowAnalyzer) -> np.ndarray:
    """Optical-flow arrows only, in place. The control room draws its own zone outlines."""
    for x, y, dx, dy in analyzer.arrows():
        # flow is px per frame; stretch it so a brisk walk reads as a clear arrow
        k = min(6.0, 40.0 / max(abs(dx), abs(dy), 1e-6))
        cv2.arrowedLine(frame, (int(x), int(y)), (int(x + dx * k), int(y + dy * k)),
                        ARROW_BGR, 2, cv2.LINE_AA, tipLength=0.3)
    return frame


def draw_banner(frame: np.ndarray, text: str, color_hex: str) -> None:
    """Alert line across the bottom of the frame, in place."""
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, h - 36), (w, h), (30, 30, 30), -1)
    cv2.putText(frame, _ascii(text), (12, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                hex_to_bgr(color_hex), 1, cv2.LINE_AA)


def _label(img: np.ndarray, text: str, org: tuple[int, int], color: tuple[int, int, int]) -> None:
    font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
    (tw, th), base = cv2.getTextSize(text, font, scale, thick)
    x, y = org
    cv2.rectangle(img, (x, y), (x + tw + 8, y + th + base + 6), color, -1)
    cv2.putText(img, text, (x + 4, y + th + 3), font, scale, (25, 25, 25), thick, cv2.LINE_AA)


def _ascii(text: str) -> str:
    # OpenCV's fonts only cover ASCII
    return text.replace("²", "2").replace("→", "->").encode("ascii", "replace").decode()
