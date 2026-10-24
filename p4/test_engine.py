"""How the control room reacts to what the pipeline sends."""
import json
import threading
import time
from pathlib import Path

from p4.engine import Engine

VENUE = json.loads((Path(__file__).parent / "venue.json").read_text(encoding="utf-8"))


def engine():
    return Engine({**VENUE, "critical_hold_s": 0})


def types(e):
    return sorted(i["type"] for i in e.incidents.values())


def test_pipeline_saying_no_prediction_is_respected():
    e = engine()
    for d in (4.0, 4.4, 4.8, 5.2, 5.6, 5.9):  # rising fast enough for P4's own fallback to guess
        for zid in e.zones:
            e.zones[zid]["history"].append((0, 0))
        e.ingest_zones({"platform_1": {"density": d, "minutes_to_critical": None}})
    assert e.zones["platform_1"]["minutes_to_critical"] is None


def test_pipeline_prediction_is_shown():
    e = engine()
    e.ingest_zones({"platform_1": {"density": 4.5, "minutes_to_critical": 6.0}})
    assert e.zones["platform_1"]["minutes_to_critical"] == 6.0


def test_critical_level_from_a_scatter_is_not_a_crush():
    e = engine()
    e.ingest_zones({"platform_2": {"density": 2.0, "level": "critical", "surge": False}})
    assert e.zones["platform_2"]["level"] == "critical"   # shown in the risk engine's colour
    assert types(e) == []                                 # the scatter itself comes as an event
    e.ingest_event("scatter", "platform_2")
    assert types(e) == ["scatter"]


def test_dense_zone_opens_a_crush_risk_and_a_surge_is_labelled_as_one():
    e = engine()
    e.ingest_zones({"platform_1": {"density": 6.4, "level": "critical"},
                    "platform_2": {"density": 4.5, "level": "critical", "surge": True}})
    assert types(e) == ["crush_risk", "surge"]


def test_crowd_alert_stays_open_while_the_risk_engine_says_critical():
    e = engine()
    e.ingest_zones({"platform_1": {"density": 6.4, "level": "critical"}})
    (inc,) = e.incidents.values()
    e.clear_hold = 0
    e.ingest_zones({"platform_1": {"density": 5.8, "level": "critical"}})  # P3's hysteresis
    assert inc["status"] == "new"
    e.ingest_zones({"platform_1": {"density": 5.0, "level": "warning"}})
    e.evaluate()
    assert inc["status"] == "cleared"


def test_client_avoids_the_slow_localhost_lookup():
    from p4.poltergeist_client import local_ipv4
    assert local_ipv4("http://localhost:8000") == "http://127.0.0.1:8000"
    assert local_ipv4("http://localhost") == "http://127.0.0.1"
    assert local_ipv4("http://192.168.1.5:8000") == "http://192.168.1.5:8000"


def test_client_sends_only_the_latest_zone_numbers():
    from p4.poltergeist_client import Poltergeist

    sent, release = [], threading.Event()

    def slow_server(path, body, content_type):
        sent.append(json.loads(body)["zones"])
        release.wait(2)

    def wait_for(n):
        for _ in range(200):
            if len(sent) >= n:
                return
            time.sleep(0.01)

    pg = Poltergeist("http://localhost:1")
    pg._post = slow_server
    pg.zones({"platform_1": {"density": 1.0}})
    wait_for(1)                                        # stuck sending this one...
    pg.zones({"platform_1": {"density": 2.0}})         # ...while two more updates arrive
    pg.zones({"platform_1": {"density": 3.0}, "platform_2": {"level": "safe"}})
    release.set()
    wait_for(2)
    assert sent == [{"platform_1": {"density": 1.0}},
                    {"platform_1": {"density": 3.0}, "platform_2": {"level": "safe"}}]
