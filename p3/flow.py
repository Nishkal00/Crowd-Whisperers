"""Farneback optical flow per zone: catches crowd surges and panicked scatter.

A surge is most of a zone suddenly moving fast in one direction (a push towards
stairs or a gate). A scatter is a sudden burst of fast motion in many directions,
or outward from one spot (people running from something). Both are judged against
each zone's own normal speed, learned during a short warm-up and then adapted slowly.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import cv2
import numpy as np

from .zones import Zone


@dataclass
class FlowConfig:
    proc_width: int = 480           # frames are downscaled to this width before flow
    min_flow_px: float = 0.4        # per-frame motion below this (processed px) counts as still
    warmup_s: float = 3.0           # learn each zone's normal speed before flagging anything
    baseline_tau_s: float = 30.0    # how slowly the normal speed adapts afterwards
    min_baseline: float = 2.0       # px/s floor, so a near-still zone doesn't trip on any motion
    surge_ratio: float = 2.0        # surge: speed at least this many times the zone's normal...
    surge_coherence: float = 0.6    # ...with the movers mostly heading the same way...
    surge_moving_frac: float = 0.25  # ...and a good share of the zone moving
    surge_persist_s: float = 1.0
    scatter_ratio: float = 2.5      # scatter: a bigger spike in speed...
    scatter_coherence: float = 0.4  # ...with no common direction...
    scatter_radial: float = 0.5     # ...or heading outward from one spot
    scatter_moving_frac: float = 0.2
    scatter_persist_s: float = 0.5
    clear_s: float = 2.0            # an event ends once its signs have been gone this long


@dataclass
class ZoneFlow:
    zone_id: str
    speed: float          # mean motion over the zone, px/s at processing scale
    baseline: float       # the zone's normal speed, px/s
    coherence: float      # 1 = movers all heading the same way, 0 = no common direction
    direction_deg: float  # image coords: 0 = right, 90 = down
    radial: float         # +1 = moving straight out from the centre of motion, -1 = straight in
    moving_frac: float    # share of the zone's pixels that are moving
    surge: bool = False
    scatter: bool = False

    @property
    def ratio(self) -> float:
        return self.speed / max(self.baseline, 1e-6)

    def to_dict(self) -> dict:
        d = {k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(self).items()}
        d["ratio"] = round(self.ratio, 2)
        return d


class _Trigger:
    """Debounces one signal: on after `persist_s` of true, off after `clear_s` of false."""

    def __init__(self, persist_s: float, clear_s: float):
        self.persist_s, self.clear_s = persist_s, clear_s
        self.active = False
        self._since: float | None = None

    def update(self, cond: bool, ts: float) -> bool:
        if cond == self.active:
            self._since = None
            return self.active
        if self._since is None:
            self._since = ts
        if ts - self._since >= (self.persist_s if cond else self.clear_s):
            self.active, self._since = cond, None
        return self.active


class _ZoneState:
    def __init__(self, cfg: FlowConfig):
        self.baseline: float | None = None
        self.samples = 0
        self.started: float | None = None  # first measurement; warm-up runs from here
        self.surge = _Trigger(cfg.surge_persist_s, cfg.clear_s)
        self.scatter = _Trigger(cfg.scatter_persist_s, cfg.clear_s)


class FlowAnalyzer:
    """Feed frames in order with their timestamps; get per-zone motion stats and events."""

    def __init__(self, zones: list[Zone], frame_size: tuple[int, int], config: FlowConfig | None = None):
        self.cfg = config or FlowConfig()
        self.zones = zones
        w, h = frame_size
        self.scale = min(1.0, self.cfg.proc_width / w)
        self.proc_size = (max(1, round(w * self.scale)), max(1, round(h * self.scale)))
        masks = {z.id: z.mask(self.proc_size, self.scale) for z in zones}
        self._pixels = {zid: np.nonzero(m) for zid, m in masks.items()}
        self._inside = np.logical_or.reduce(list(masks.values())) if masks else None
        self._state = {z.id: _ZoneState(self.cfg) for z in zones}
        self._prev: np.ndarray | None = None
        self._prev_ts = 0.0
        self.flow: np.ndarray | None = None  # last flow field, processing scale, px/frame

    def update(self, frame: np.ndarray, ts: float) -> dict[str, ZoneFlow]:
        """Process one frame taken at `ts` seconds. Returns {} for the very first frame."""
        small = cv2.resize(frame, self.proc_size, interpolation=cv2.INTER_AREA)
        gray = small if small.ndim == 2 else cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self._prev is None:
            self._prev, self._prev_ts = gray, ts
            return {}

        dt = ts - self._prev_ts if ts > self._prev_ts else 1 / 30
        self.flow = cv2.calcOpticalFlowFarneback(self._prev, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        self._prev, self._prev_ts = gray, ts
        return {
            z.id: self._zone_flow(z.id, dt, ts)
            for z in self.zones
            if len(self._pixels[z.id][0])
        }

    def _zone_flow(self, zid: str, dt: float, ts: float) -> ZoneFlow:
        cfg, st = self.cfg, self._state[zid]
        if st.started is None:
            st.started = ts
        # timed from the first measurement, so a slow first frame can't skip the warm-up
        warm = ts - st.started >= cfg.warmup_s
        ys, xs = self._pixels[zid]
        fx, fy = self.flow[ys, xs, 0], self.flow[ys, xs, 1]
        mag = np.hypot(fx, fy)
        speed = float(mag.mean()) / dt
        moving = mag > cfg.min_flow_px
        moving_frac = float(moving.mean())

        coherence = direction = radial = 0.0
        if moving.any():
            mx, my, total = fx[moving], fy[moving], float(mag[moving].sum())
            sx, sy = float(mx.sum()), float(my.sum())
            coherence = math.hypot(sx, sy) / total
            direction = math.degrees(math.atan2(sy, sx)) % 360
            # outward component of each mover's motion, measured from the centre of the movers
            rx, ry = xs[moving] - xs[moving].mean(), ys[moving] - ys[moving].mean()
            rn = np.hypot(rx, ry)
            far = rn > 1
            radial = float(((mx * rx + my * ry)[far] / rn[far]).sum()) / total

        if not warm:
            st.samples += 1
            st.baseline = speed if st.baseline is None else st.baseline + (speed - st.baseline) / st.samples
            st.surge.update(False, ts)
            st.scatter.update(False, ts)
            return ZoneFlow(zid, speed, st.baseline, coherence, direction, radial, moving_frac)

        ratio = speed / max(st.baseline, cfg.min_baseline)
        surge_now = (ratio >= cfg.surge_ratio and coherence >= cfg.surge_coherence
                     and moving_frac >= cfg.surge_moving_frac)
        scatter_now = (ratio >= cfg.scatter_ratio and moving_frac >= cfg.scatter_moving_frac
                       and (coherence <= cfg.scatter_coherence or radial >= cfg.scatter_radial))
        surge = st.surge.update(surge_now, ts)
        scatter = st.scatter.update(scatter_now, ts)
        # keep events from becoming the new normal
        if not (surge_now or scatter_now or surge or scatter):
            st.baseline += (1 - math.exp(-dt / cfg.baseline_tau_s)) * (speed - st.baseline)
        return ZoneFlow(zid, speed, st.baseline, coherence, direction, radial, moving_frac, surge, scatter)

    def arrows(self, step: int = 24, min_px: float = 0.8) -> list[tuple[float, float, float, float]]:
        """Mean flow on a grid inside the zones, as (x, y, dx, dy) in original-frame px per frame."""
        if self.flow is None or self._inside is None:
            return []
        h, w = self.flow.shape[:2]
        ny, nx = h // step, w // step
        cells = self.flow[:ny * step, :nx * step].reshape(ny, step, nx, step, 2).mean(axis=(1, 3))
        cy, cx = np.arange(ny) * step + step // 2, np.arange(nx) * step + step // 2
        keep = self._inside[cy[:, None], cx[None, :]] & (np.hypot(cells[..., 0], cells[..., 1]) >= min_px)
        s = 1 / self.scale
        return [(cx[j] * s, cy[i] * s, float(cells[i, j, 0]) * s, float(cells[i, j, 1]) * s)
                for i, j in zip(*np.nonzero(keep))]
