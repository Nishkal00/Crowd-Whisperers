"""The P1 -> P2 -> P3 -> P4 wiring, with a stand-in for YOLO so no model is needed."""
import json
from pathlib import Path

import numpy as np

from p1.vision import Down, Person, VisionResult
from pipeline import Pipeline

VENUE = json.loads((Path(__file__).resolve().parents[1] / "p4" / "venue.json").read_text(encoding="utf-8"))
W, H = 640, 360


class FakeVision:
    """Puts `n` people standing in Platform 1, and optionally one person down there."""

    def __init__(self):
        self.n, self.down = 0, False

    def process(self, frame, ts):
        # feet spread over x 10-150, y 300-355: inside Platform 1 at 640 x 360
        feet = [(10 + i % 140, 300 + (i // 140) % 55) for i in range(self.n)]
        people = [Person((x - 4, y - 30, x + 4, y), 0.8) for x, y in feet]
        down = [Down(7, (40.0, 300.0, 90.0, 320.0), since=0.0, conf=0.7)] if self.down else []
        return VisionResult(people, frame.copy(), down)


class FakeSender:
    def __init__(self):
        self.frames, self.zone_msgs, self.events = 0, [], []

    def frame(self, f):
        self.frames += 1

    def zones(self, z):
        self.zone_msgs.append(z)

    def event(self, type, zone, bbox=None, confidence=None, note=None):
        self.events.append((type, zone, bbox))


def run(vision, seconds, sender=None, fps=10):
    pipe = Pipeline(VENUE, (W, H), vision=vision, sender=sender)
    frame = np.full((H, W, 3), 90, np.uint8)
    res = None
    for i in range(int(seconds * fps)):
        res = pipe.step(frame, i / fps)
    return pipe, res


def test_counts_become_density_and_levels():
    v = FakeVision()
    v.n = 1170  # 6.5 people per m² on Platform 1's 180 m²
    pipe, res = run(v, 4)
    assert pipe.zone_at(80, 300) == "platform_1"
    assert res.counts["platform_1"] == 1170
    assert abs(res.density["platform_1"] - 6.5) < 0.01
    assert res.risk.zones["platform_1"].level.name == "CRITICAL"
    assert res.risk.zones["platform_2"].level.name == "SAFE"


def test_p4_gets_frames_zone_numbers_and_events():
    v, s = FakeVision(), FakeSender()
    v.n, v.down = 400, True
    run(v, 3, sender=s)
    assert s.frames == 30
    assert 5 <= len(s.zone_msgs) <= 7  # twice a second
    last = s.zone_msgs[-1]
    assert set(last) == {"platform_1", "platform_2", "footbridge"}
    assert last["platform_1"]["density"] == round(400 / 180, 2)
    assert last["platform_1"]["level"] == "warning"  # someone down, zone not packed
    assert "count" not in last["platform_1"]           # P4 derives it from density and area
    assert "minutes_to_critical" in last["platform_1"]
    t, zone, bbox = s.events[-1]
    assert (t, zone) == ("person_down", "platform_1")
    assert bbox == [round(40 / W, 4), round(300 / H, 4), round(50 / W, 4), round(20 / H, 4)]


def test_no_prediction_until_there_is_a_trend():
    v = FakeVision()
    v.n = 300
    _, res = run(v, 5)
    assert res.minutes_to_critical["platform_1"] is None
