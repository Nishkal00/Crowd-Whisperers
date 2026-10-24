"""Zones: areas of the camera view, drawn once with their real floor area.

The team's one zones file is P4's venue.json (polygons as 0-1 fractions of the frame);
load_zones() also reads a plain list of zones with pixel polygons.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass
class Zone:
    id: str
    name: str
    polygon: list[tuple[float, float]]  # pixel coords in the original camera frame
    area_m2: float | None = None        # real floor area, so P2 can turn counts into people per m²
    exits: list[str] = field(default_factory=list)  # zone ids the crowd can be sent to from here
    location: Any = None                # passed through to dispatch (e.g. map x/y of the zone)

    def mask(self, size: tuple[int, int], scale: float = 1.0) -> np.ndarray:
        """Boolean mask of this zone on a (width, height) image, polygon scaled by `scale`."""
        w, h = size
        m = np.zeros((h, w), np.uint8)
        pts = np.round(np.asarray(self.polygon, np.float32) * scale).astype(np.int32)
        cv2.fillPoly(m, [pts], 1)
        return m.astype(bool)


def load_zones(path: str | Path, frame_size: tuple[int, int] | None = None,
               camera: str | None = None) -> list[Zone]:
    """Read zones from venue.json (needs `frame_size`), or from a list of zones in pixels."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("zones"), dict):
        if frame_size is None:
            raise ValueError("venue.json polygons are fractions of the frame: pass frame_size")
        return zones_from_venue(data, frame_size, camera)
    items = data["zones"] if isinstance(data, dict) else data
    return [
        Zone(
            id=str(z["id"]),
            name=z.get("name", z["id"]),
            polygon=[tuple(p) for p in z["polygon"]],
            area_m2=z.get("area_m2"),
            exits=list(z.get("exits", [])),
            location=z.get("location"),
        )
        for z in items
    ]


def zones_from_venue(venue: dict, frame_size: tuple[int, int], camera: str | None = None) -> list[Zone]:
    """Zones from P4's venue.json for one camera, polygons scaled to `frame_size` pixels."""
    w, h = frame_size
    return [
        Zone(
            id=zid,
            name=z.get("name", zid),
            polygon=[(x * w, y * h) for x, y in z["polygon"]],
            area_m2=z.get("area_m2"),
            # exits without a camera are {"name": ...}; only zones we can see can be compared
            exits=[t for t in z.get("redirect_to", []) if isinstance(t, str)],
            location=z.get("map"),
        )
        for zid, z in venue["zones"].items()
        if z.get("polygon") and (camera is None or z.get("camera", camera) == camera)
    ]


def whole_frame(width: int, height: int) -> Zone:
    """One zone covering the full frame, for footage nobody has drawn zones on yet."""
    return Zone("frame", "Whole frame", [(0, 0), (width, 0), (width, height), (0, height)])
