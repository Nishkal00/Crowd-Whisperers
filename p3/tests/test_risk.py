from p3.flow import ZoneFlow
from p3.risk import Level, RiskConfig, RiskEngine
from p3.zones import Zone

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


def zones():
    return [
        Zone("p1", "Platform 1", SQUARE, exits=["fb"]),
        Zone("p2", "Platform 2", SQUARE, exits=["fb"]),
        Zone("fb", "Footbridge", SQUARE),
    ]


def flow(zid="p1", surge=False, scatter=False):
    return ZoneFlow(zid, speed=10, baseline=2, coherence=0.9, direction_deg=0, radial=0,
                    moving_frac=0.8, surge=surge, scatter=scatter)


def test_density_levels_need_confirming():
    eng = RiskEngine(zones(), RiskConfig(confirm_s=2))
    d = {"p1": 6.5, "p2": 4.5, "fb": 1.0}
    u = eng.update(0, density=d)
    assert u.zones["p1"].level == Level.SAFE and not u.alerts
    u = eng.update(2, density=d)
    assert [u.zones[z].level for z in ("p1", "p2", "fb")] == [Level.CRITICAL, Level.WARNING, Level.SAFE]
    by_zone = {a.zone_id: a for a in u.alerts}
    assert set(by_zone) == {"p1", "p2"}
    assert by_zone["p1"].message == "6.5 /m²"
    assert by_zone["p1"].responders == ["security", "police", "rpf"]
    assert by_zone["p1"].redirect_to == {"zone_id": "fb", "zone_name": "Footbridge"}


def test_one_reading_spike_is_ignored():
    eng = RiskEngine(zones(), RiskConfig(confirm_s=2))
    eng.update(0, density={"p1": 7.0})
    u = eng.update(1, density={"p1": 3.0})
    u = eng.update(3, density={"p1": 3.0})
    assert u.zones["p1"].level == Level.SAFE


def test_steps_down_only_after_hold_and_hysteresis():
    eng = RiskEngine(zones(), RiskConfig(confirm_s=0, hold_s=10))
    assert eng.update(0, density={"p1": 6.5}).zones["p1"].level == Level.CRITICAL
    assert eng.update(1, density={"p1": 5.8}).zones["p1"].level == Level.CRITICAL  # within hysteresis
    assert eng.update(2, density={"p1": 5.0}).zones["p1"].level == Level.CRITICAL  # held
    assert eng.update(12, density={"p1": 5.0}).zones["p1"].level == Level.WARNING


def test_prediction_shows_minutes_to_critical():
    eng = RiskEngine(zones(), RiskConfig(confirm_s=0))
    u = eng.update(0, density={"p2": 3.0}, minutes_to_critical={"p2": 5.2})
    r = u.zones["p2"]
    assert r.level == Level.WARNING
    assert r.status == "CRITICAL IN 6 MIN"
    far = eng.update(1, density={"p1": 3.0}, minutes_to_critical={"p1": 25}).zones["p1"]
    assert far.level == Level.SAFE


def test_surge_is_critical_only_in_a_dense_zone():
    eng = RiskEngine(zones(), RiskConfig(confirm_s=5))
    u = eng.update(0, density={"p1": 4.5, "p2": 1.0},
                   flow={"p1": flow("p1", surge=True), "p2": flow("p2", surge=True)})
    assert u.zones["p1"].level == Level.CRITICAL  # no confirm wait for flow events
    assert u.zones["p1"].reasons == ["4.5 /m²", "crowd surging"]
    assert u.zones["p2"].level == Level.WARNING


def test_scatter_is_critical_and_goes_to_security():
    eng = RiskEngine(zones())
    u = eng.update(0, density={"p2": 2.0}, flow={"p2": flow("p2", scatter=True)})
    (a,) = u.alerts
    assert a.level == Level.CRITICAL
    assert a.responders == ["security", "police"]


def test_incident_alerts_medical_once_then_new_causes_alert_again():
    eng = RiskEngine(zones())
    inc = [{"type": "person_down", "zone_id": "p1", "snapshot": "down.jpg"}]
    (a,) = eng.update(0, density={"p1": 2.0}, incidents=inc).alerts
    assert (a.level, a.responders, a.snapshot, a.message) == (Level.WARNING, ["medical"], "down.jpg",
                                                              "2.0 /m², person down")
    assert eng.update(1, density={"p1": 2.0}, incidents=inc).alerts == []
    (b,) = eng.update(2, density={"p1": 4.5}, incidents=inc, flow={"p1": flow(surge=True)}).alerts
    assert b.level == Level.CRITICAL
    assert set(b.kinds) == {"density", "surge", "person_down"}
    assert b.responders == ["security", "police", "rpf", "medical"]


def test_incident_expires_after_ttl():
    eng = RiskEngine(zones(), RiskConfig(incident_ttl_s=5, hold_s=0))
    eng.update(0, density={"p1": 1.0}, incidents=[{"type": "person_down", "zone_id": "p1"}])
    assert eng.update(4, density={"p1": 1.0}).zones["p1"].level == Level.WARNING
    assert eng.update(6, density={"p1": 1.0}).zones["p1"].level == Level.SAFE


def test_no_repeat_alerts_while_critical():
    eng = RiskEngine(zones(), RiskConfig(confirm_s=0))
    counts = [len(eng.update(t, density={"p1": 7.0}).alerts) for t in range(5)]
    assert counts == [1, 0, 0, 0, 0]


def test_redirect_skips_a_busy_exit():
    zs = [Zone("p1", "Platform 1", SQUARE, exits=["p2", "fb"]), Zone("p2", "Platform 2", SQUARE),
          Zone("fb", "Footbridge", SQUARE)]
    eng = RiskEngine(zs, RiskConfig(confirm_s=0))
    (a, *_) = eng.update(0, density={"p1": 7.0, "p2": 6.5, "fb": 1.0}).alerts
    assert a.zone_id == "p1" and a.redirect_to["zone_id"] == "fb"

    eng = RiskEngine(zs, RiskConfig(confirm_s=0))
    (a, *_) = eng.update(0, density={"p1": 7.0, "p2": 6.5, "fb": 6.2}).alerts
    assert a.redirect_to is None


def test_update_serialises_to_json():
    import json

    eng = RiskEngine(zones(), RiskConfig(confirm_s=0))
    u = eng.update(0, density={"p1": 6.4}, flow={"p1": flow(surge=True)})
    data = json.loads(json.dumps(u.to_dict()))
    assert data["zones"]["p1"]["level"] == "CRITICAL"
    assert data["zones"]["p1"]["flow"]["surge"] is True
    assert data["alerts"][0]["redirect_to"]["zone_id"] == "fb"
