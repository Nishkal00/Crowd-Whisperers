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


def test_send_endpoint_refuses_bad_requests_without_sending():
    # Only the refusals: nothing here reaches Telegram.
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)  # no "with": the lifespan (Telegram polling) doesn't start
    assert client.post("/api/send", json={"zone": "nope", "template": "crowd", "staff": True}).status_code == 404
    assert client.post("/api/send", json={"zone": "platform_1", "template": "x", "staff": True}).status_code == 400
    assert client.post("/api/send", json={"zone": "platform_1", "template": "crowd"}).status_code == 400


def test_subscriber_counts_by_zone():
    from banshee import Banshee
    b = Banshee(engine(), VENUE, token=None, log=lambda *_: None)
    b.subscribers = {"1": {"role": "staff"}, "2": {"role": "public", "zone": "platform_1"},
                     "3": {"role": "public", "zone": "platform_1"}, "4": {"role": "public", "zone": "footbridge"}}
    assert b.counts() == {"staff": 1, "public": 3, "public_by_zone": {"platform_1": 2, "footbridge": 1}}


def test_staff_alert_from_telegram_opens_a_dashboard_incident():
    import asyncio
    from banshee import Banshee
    e = engine()
    b = Banshee(e, VENUE, token=None, log=lambda *_: None)
    b._save_subscribers = lambda: None          # never touch the real subscribers.json
    b.subscribers = {"1": {"role": "staff", "name": "Ann"}, "2": {"role": "public", "zone": "platform_1"}}
    sent = []

    async def fake_send(cid, text, buttons=None):
        sent.append((cid, text))
    b._send = fake_send

    def update(cid, text):
        return {"message": {"chat": {"id": int(cid), "first_name": "Ann"}, "text": text}}

    asyncio.run(b._handle(update("1", "/alert platform_1 gate jammed")))
    (inc,) = e.incidents.values()
    assert inc["type"] == "manual" and inc["zone"] == "platform_1"
    assert inc["note"] == "Ann: gate jammed"
    assert any(e_["text"] == "Raised by staff on Telegram" for e_ in inc["timeline"])
    assert "dashboard" in sent[-1][1]

    asyncio.run(b._handle(update("1", "/alert nowhere help")))
    assert len(e.incidents) == 1 and "Which zone" in sent[-1][1]

    asyncio.run(b._handle(update("2", "/alert platform_1 help")))
    assert len(e.incidents) == 1            # the public subscriber can't raise alerts


def test_automatic_alerts_switch_off_camera_alerts_only():
    e = engine()
    e.auto_alerts = False
    e.ingest_zones({"platform_1": {"density": 6.5, "level": "critical"}})
    assert e.incidents == {}                                     # the camera's crush reading raises nothing
    assert e.ingest_event("scatter", "platform_1") is None       # camera events are ignored too
    assert e.incidents == {}
    e.open_incident("manual", "platform_1", note="staff call", source="dashboard")
    assert [i["type"] for i in e.incidents.values()] == ["manual"]  # people can still raise alerts
    e.auto_alerts = True
    e.ingest_zones({"platform_2": {"density": 6.5, "level": "critical"}})
    assert "crush_risk" in [i["type"] for i in e.incidents.values()]
