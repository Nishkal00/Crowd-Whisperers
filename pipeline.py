"""Poltergeist: P1 -> P2 -> P3 -> P4 on one camera.

    P1  people, poses, blurred faces, person down       p1/vision.py
    P2  people per m², time to critical, heatmap        p2/
    P3  optical flow (surge, scatter), risk levels      p3/
    P4  control room, Banshee dispatch, PA voice        p4/ (a web server)

Each frame goes through P1-P3 here. P4 gets the picture (faces blurred, density heatmap,
flow arrows), every zone's numbers and risk level twice a second, and events as they
happen. Zones, areas and thresholds all come from p4/venue.json.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from p1.vision import Vision
from p2.density import calculate_density
from p2.heatmap import people_heatmap
from p2.prediction import CrowdPredictor
from p3 import FlowAnalyzer, RiskConfig, RiskEngine, RiskUpdate, draw_flow_arrows, zones_from_venue


@dataclass
class StepResult:
    feed: np.ndarray                # what the control room shows
    risk: RiskUpdate                # P3's level for every zone, and its alerts
    counts: dict[str, int]          # people standing in each zone this frame
    density: dict[str, float]       # people per m², smoothed
    minutes_to_critical: dict[str, float | None]
    incidents: list[dict] = field(default_factory=list)
    people: int = 0


class Pipeline:
    def __init__(self, venue: dict, frame_size: tuple[int, int], camera: str = "cam1",
                 vision: Vision | None = None, sender=None, send_every_s: float = 0.5,
                 density_tau_s: float = 1.5):
        self.venue, self.camera, self.size = venue, camera, frame_size
        self.zones = zones_from_venue(venue, frame_size, camera)
        if not self.zones:
            raise ValueError(f"venue.json has no zones with a polygon for camera {camera!r}")
        self._masks = {z.id: z.mask(frame_size) for z in self.zones}
        self._in_any_zone = np.logical_or.reduce(list(self._masks.values()))

        critical = float(venue.get("thresholds", {}).get("critical", 6.0))
        self.vision = vision or Vision()
        self.predictor = CrowdPredictor(history_seconds=60, critical_density=critical)
        self.flow = FlowAnalyzer(self.zones, frame_size)
        self.risk = RiskEngine(self.zones, RiskConfig.from_venue(venue))
        self.sender = sender                  # p4.poltergeist_client.Poltergeist, or None
        self.send_every_s = send_every_s
        self.density_tau_s = density_tau_s    # smooths out the odd missed detection

        self._density: dict[str, float] = {}
        self._eta: dict[str, float | None] = {}
        self._last_ts: float | None = None
        self._last_send = self._last_predict = -math.inf

    def zone_at(self, x: float, y: float) -> str | None:
        w, h = self.size
        xi, yi = min(max(int(x), 0), w - 1), min(max(int(y), 0), h - 1)
        return next((zid for zid, m in self._masks.items() if m[yi, xi]), None)

    def step(self, frame: np.ndarray, ts: float) -> StepResult:
        """Run one frame taken at `ts` seconds through P1-P3 and send the results to P4."""
        w, h = self.size

        # P1: people and incidents, each placed in the zone they're standing in
        seen = self.vision.process(frame, ts)
        counts = {z.id: 0 for z in self.zones}
        for p in seen.people:
            zid = self.zone_at(*p.foot)
            if zid:
                counts[zid] += 1
        incidents = []
        for d in seen.down:
            x1, y1, x2, y2 = d.box
            zid = self.zone_at((x1 + x2) / 2, (y1 + y2) / 2)
            if zid:
                incidents.append({"id": f"down-{d.id}", "type": "person_down", "zone_id": zid,
                                  "bbox": d.bbox_fraction(w, h), "confidence": round(d.conf, 2),
                                  "duration_s": round(ts - d.since, 1)})

        # P2: people per m² (smoothed) and the trend towards critical
        a = 1.0 if self._last_ts is None else 1 - math.exp(-max(ts - self._last_ts, 0) / self.density_tau_s)
        for z in self.zones:
            if z.area_m2:
                d = calculate_density(counts[z.id], z.area_m2)
                prev = self._density.get(z.id)
                self._density[z.id] = d if prev is None else prev + a * (d - prev)
        if ts - self._last_predict >= 1.0:  # a trend point a second is plenty
            self._last_predict = ts
            for zid, d in self._density.items():
                self._eta[zid] = self.predictor.update(zid, d, ts)["minutes_to_critical"]
        self._last_ts = ts

        # P3: motion, then one risk level per zone from everything above
        flows = self.flow.update(frame, ts)
        risk = self.risk.update(ts, density=dict(self._density),
                                minutes_to_critical={k: v for k, v in self._eta.items() if v is not None},
                                flow=flows, incidents=incidents)

        # the control room's picture: blurred faces, heatmap inside the zones, flow arrows
        feed = people_heatmap(seen.frame, [p.box for p in seen.people], mask=self._in_any_zone)
        draw_flow_arrows(feed, self.flow)

        if self.sender is not None:
            self.sender.frame(feed)
            if ts - self._last_send >= self.send_every_s:
                self._last_send = ts
                self._send(risk, counts, flows, incidents)

        return StepResult(feed, risk, counts, dict(self._density), dict(self._eta), incidents, len(seen.people))

    def _send(self, risk: RiskUpdate, counts: dict, flows: dict, incidents: list) -> None:
        zones = {}
        for z in self.zones:
            r, fl = risk.zones[z.id], flows.get(z.id)
            m = {"surge": bool(fl and fl.surge), "level": r.level.name.lower(),
                 "minutes_to_critical": self._eta.get(z.id)}
            if z.id in self._density:
                m["density"] = round(self._density[z.id], 2)  # P4 works the count out from the area
            else:
                m["count"] = counts[z.id]
            zones[z.id] = m
        self.sender.zones(zones)
        # P4 keeps one alert open per event and zone, so repeating these is fine
        for zid, fl in flows.items():
            if fl.scatter:
                self.sender.event("scatter", zone=zid, confidence=round(min(1.0, fl.ratio / 5), 2))
        for inc in incidents:
            self.sender.event(inc["type"], zone=inc["zone_id"], bbox=inc["bbox"], confidence=inc["confidence"])
