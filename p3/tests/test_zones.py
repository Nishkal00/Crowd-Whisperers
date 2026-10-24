import json
from pathlib import Path

import pytest

from p3.risk import RiskConfig
from p3.zones import load_zones

VENUE = Path(__file__).resolve().parents[2] / "p4" / "venue.json"


def test_venue_zones_scale_to_the_frame():
    zones = {z.id: z for z in load_zones(VENUE, (1000, 500))}
    assert set(zones) == {"platform_1", "platform_2", "footbridge"}
    p1 = zones["platform_1"]
    assert p1.name == "Platform 1" and p1.area_m2 == 180
    assert p1.exits == ["footbridge"]
    assert p1.polygon[0] == pytest.approx((0.0, 381.25))   # [0.0, 0.7625] of 1000 x 500
    assert p1.mask((1000, 500)).sum() > 0


def test_venue_needs_a_frame_size():
    with pytest.raises(ValueError):
        load_zones(VENUE)


def test_only_the_cameras_zones(tmp_path):
    venue = json.loads(VENUE.read_text(encoding="utf-8"))
    venue["zones"]["platform_2"]["camera"] = "cam2"
    path = tmp_path / "venue.json"
    path.write_text(json.dumps(venue), encoding="utf-8")
    assert [z.id for z in load_zones(path, (100, 100), camera="cam2")] == ["platform_2"]


def test_thresholds_come_from_the_venue():
    cfg = RiskConfig.from_venue({"thresholds": {"warning": 3.5, "critical": 5.0}}, hold_s=4)
    assert (cfg.warning_density, cfg.critical_density, cfg.hold_s) == (3.5, 5.0, 4)
    assert RiskConfig.from_venue({}).critical_density == 6.0
