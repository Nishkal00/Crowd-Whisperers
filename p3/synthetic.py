"""Synthetic crowd video: dots standing in for people, scripted to surge or scatter.

Lets P3 be demoed and tested before real footage and the other modules are ready.
"""
from __future__ import annotations

import cv2
import numpy as np

from .zones import Zone


class SyntheticCrowd:
    def __init__(self, zones: list[Zone], size: tuple[int, int] = (960, 540),
                 px_per_person: float = 550, seed: int = 0):
        self.size = size
        w, h = size
        self.rng = np.random.default_rng(seed)
        noise = cv2.GaussianBlur(self.rng.normal(70, 25, (h, w)).astype(np.float32), (0, 0), 3)
        self.background = cv2.cvtColor(np.clip(noise, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
        self.modes: dict[str, tuple[str, object]] = {}
        self._masks: dict[str, np.ndarray] = {}
        self._spots: dict[str, np.ndarray] = {}  # every pixel inside the zone, for (re)spawning
        self._people: dict[str, dict[str, np.ndarray]] = {}
        for z in zones:
            mask = z.mask(size)
            ys, xs = np.nonzero(mask)
            spots = np.column_stack([xs, ys]).astype(np.float32)
            if not len(spots):
                continue
            n = max(1, int(len(spots) / px_per_person))
            self._masks[z.id], self._spots[z.id] = mask, spots
            self._people[z.id] = {
                "pos": spots[self.rng.integers(len(spots), size=n)].copy(),
                "vel": np.zeros((n, 2), np.float32),
                "color": [tuple(int(v) for v in c) for c in self.rng.integers(110, 230, (n, 3))],
                "radius": self.rng.uniform(5, 8, n),
            }
            self.modes[z.id] = ("calm", None)

    def calm(self, zone_id: str) -> None:
        self.modes[zone_id] = ("calm", None)

    def surge(self, zone_id: str, velocity: tuple[float, float]) -> None:
        """Everyone in the zone pushes the same way, `velocity` in px per frame."""
        self.modes[zone_id] = ("surge", np.asarray(velocity, np.float32))

    def scatter(self, zone_id: str, origin: tuple[float, float], speed: float) -> None:
        """Everyone runs away from `origin` at `speed` px per frame."""
        self.modes[zone_id] = ("scatter", (np.asarray(origin, np.float32), speed))

    def step(self) -> np.ndarray:
        """Move everyone one frame and return the rendered BGR frame."""
        frame = self.background.copy()
        w, h = self.size
        for zid, p in self._people.items():
            mode, arg = self.modes[zid]
            n = len(p["pos"])
            if mode == "surge":
                target = np.broadcast_to(arg, (n, 2))
            elif mode == "scatter":
                origin, speed = arg
                away = p["pos"] - origin
                target = away / (np.linalg.norm(away, axis=1, keepdims=True) + 1e-6) * speed
            else:
                target = self.rng.normal(0, 1.5, (n, 2))
            p["vel"] += 0.25 * (target - p["vel"]).astype(np.float32)  # ease in, no instant turns
            p["pos"] += p["vel"]

            # anyone who walks out of the zone reappears somewhere inside it
            xi, yi = p["pos"][:, 0].astype(int), p["pos"][:, 1].astype(int)
            inside = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
            inside[inside] = self._masks[zid][yi[inside], xi[inside]]
            gone = ~inside
            if gone.any():
                spots = self._spots[zid]
                p["pos"][gone] = spots[self.rng.integers(len(spots), size=int(gone.sum()))]
                if mode == "calm":
                    p["vel"][gone] = 0

            for (x, y), c, r in zip(p["pos"], p["color"], p["radius"]):
                cx, cy = int(x * 16), int(y * 16)  # 4-bit sub-pixel positions, for smooth motion
                cv2.circle(frame, (cx, cy), int(r * 16), c, -1, cv2.LINE_AA, 4)
                cv2.circle(frame, (cx, cy), int(r * 7), (c[0] // 2, c[1] // 2, c[2] // 2), -1, cv2.LINE_AA, 4)
        return frame
