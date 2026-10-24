"""Zone state, risk levels, the fallback prediction and the rules that open incidents.

P1-P3 send zone numbers and events to the server; everything that decides
"is this an incident?" lives here so the rules are in one place.
"""
import itertools
import math
import time
from collections import deque

# What each incident type is called, which help posts can take it, and which PA message fits.
RULES = {
    "crush_risk":  {"label": "Crush risk",          "kinds": ["security", "police", "rpf"], "pa": "crowd"},
    "surge":       {"label": "Crowd surge",         "kinds": ["security", "police", "rpf"], "pa": "crowd"},
    "person_down": {"label": "Person down",         "kinds": ["medical"],                   "pa": "make_way"},
    "scatter":     {"label": "People scattering",   "kinds": ["security", "police"],        "pa": "calm"},
    "on_tracks":   {"label": "Person on the tracks", "kinds": ["rpf", "police", "security"], "pa": "tracks"},
    "sos":         {"label": "SOS from the crowd",  "kinds": ["security", "medical", "police", "rpf"], "pa": None},
    "manual":      {"label": "Manual alert",        "kinds": ["security", "police", "rpf", "medical"], "pa": None},
}
SOS_KINDS = {"medical": ["medical"], "crowding": ["security", "police", "rpf"]}
CROWD_TYPES = ("crush_risk", "surge")
OPEN_STATUSES = ("new", "dispatching", "accepted", "unanswered")


def centroid(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return [sum(xs) / len(xs), sum(ys) / len(ys)]


class Engine:
    def __init__(self, venue, on_incident=None):
        self.venue = venue
        th = venue.get("thresholds", {})
        self.warn = float(th.get("warning", 4.0))
        self.crit = float(th.get("critical", 6.0))
        self.critical_hold = float(venue.get("critical_hold_s", 3))
        self.clear_hold = float(venue.get("clear_hold_s", 10))
        self.event_cooldown = float(venue.get("event_cooldown_s", 30))
        self.stale_after = 8.0
        self.on_incident = on_incident
        self.zones = {zid: self._blank(zid) for zid in venue["zones"]}
        self.incidents = {}
        self.snapshots = {}
        self.frames = {}          # camera -> {"seq", "jpeg", "ts", "size"}
        self.unknown_zones = set()
        self._ids = itertools.count(1)

    # ---------- zones ----------
    def _blank(self, zid):
        return {"id": zid, "count": None, "density": None, "level": "nodata", "surge": False,
                "minutes_to_critical": None, "trend": "steady", "updated": None,
                "history": deque(maxlen=150), "crit_since": None, "calm_since": None,
                "level_from_pipeline": None, "mtc_from_pipeline": None, "mtc_sent": False}

    def level_for(self, density):
        if density is None:
            return "nodata"
        if density >= self.crit:
            return "critical"
        if density >= self.warn:
            return "warning"
        return "safe"

    def ingest_zones(self, data):
        now = time.time()
        for zid, m in (data or {}).items():
            if zid not in self.zones:
                self.unknown_zones.add(zid)
                continue
            z = self.zones[zid]
            area = self.venue["zones"][zid].get("area_m2")
            density = m.get("density")
            count = m.get("count")
            if density is None and count is not None and area:
                density = float(count) / float(area)
            if count is not None:
                z["count"] = int(round(float(count)))
            elif density is not None and area:
                z["count"] = int(round(float(density) * float(area)))
            if density is not None:
                z["density"] = round(float(density), 2)
                z["history"].append((now, float(density)))
            z["surge"] = bool(m.get("surge", False))
            lvl = m.get("level")
            z["level_from_pipeline"] = lvl if lvl in ("safe", "warning", "critical") else None
            mtc = m.get("minutes_to_critical")
            z["mtc_from_pipeline"] = float(mtc) if isinstance(mtc, (int, float)) else None
            # P2 sends null when there's no rising trend; that's an answer, not missing data
            z["mtc_sent"] = "minutes_to_critical" in m
            z["updated"] = now
        self.evaluate(now)

    def _trend(self, z, now):
        """Slope of density over the last 30 s, in people/m² per minute."""
        pts = [(t, d) for t, d in z["history"] if now - t <= 30]
        if len(pts) < 5 or pts[-1][0] - pts[0][0] < 5:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        md = sum(d for _, d in pts) / n
        var = sum((t - mt) ** 2 for t, _ in pts)
        if var == 0:
            return None
        slope_s = sum((t - mt) * (d - md) for t, d in pts) / var
        return slope_s * 60.0

    def evaluate(self, now=None):
        now = now or time.time()
        for zid, z in self.zones.items():
            if z["updated"] is None:
                continue
            if now - z["updated"] > self.stale_after:
                z["level"] = "stale"
                continue
            z["level"] = z["level_from_pipeline"] or self.level_for(z["density"])
            per_min = self._trend(z, now)
            if per_min is None or abs(per_min) < 0.15:
                z["trend"] = "steady"
            else:
                z["trend"] = "rising" if per_min > 0 else "falling"
            if z["mtc_sent"]:
                z["minutes_to_critical"] = z["mtc_from_pipeline"]
            elif (per_min and per_min > 0.15 and z["density"] is not None
                  and self.warn * 0.75 <= z["density"] < self.crit):  # only predict once it's getting busy
                mins = (self.crit - z["density"]) / per_min
                z["minutes_to_critical"] = round(mins, 1) if mins <= 30 else None
            else:
                z["minutes_to_critical"] = None

            # A crowd alert is about density or a surge. P3's level can also be critical because
            # of a scatter or a fall, but those arrive as their own events, so they don't count here.
            packed = z["density"] is not None and z["density"] >= self.crit
            hot = packed or z["surge"]
            crowd = self._open_crowd_incident(zid)
            if hot:
                z["calm_since"] = None
                z["crit_since"] = z["crit_since"] or now
                if crowd is None and now - z["crit_since"] >= self.critical_hold:
                    kind = "crush_risk" if packed else "surge"
                    self.open_incident(kind, zid, source="camera")
                elif crowd is not None:
                    if crowd["type"] == "surge" and packed:
                        crowd["type"] = "crush_risk"
                        crowd["label"] = RULES["crush_risk"]["label"]
                        self._log(crowd, "note", "Density passed the critical level")
                    crowd["density"] = z["density"]
                    crowd["surge"] = z["surge"] or crowd.get("surge", False)
            elif crowd is not None and z["level_from_pipeline"] == "critical":
                z["crit_since"] = None
                z["calm_since"] = None  # the risk engine still says critical: keep the alert open
            else:
                z["crit_since"] = None
                if crowd is not None:
                    z["calm_since"] = z["calm_since"] or now
                    if now - z["calm_since"] >= self.clear_hold:
                        crowd["status"] = "cleared"
                        crowd["closed_at"] = now
                        self._log(crowd, "cleared", f"Back below critical for {int(self.clear_hold)} s")

    def _open_crowd_incident(self, zid):
        for inc in self.incidents.values():
            if inc["zone"] == zid and inc["type"] in CROWD_TYPES and inc["status"] in OPEN_STATUSES:
                return inc
        return None

    # ---------- events and incidents ----------
    def ingest_event(self, etype, zone, bbox=None, confidence=None, note=None, sos_kind=None, source="camera"):
        now = time.time()
        if zone not in self.zones:
            self.unknown_zones.add(zone)
            return None
        for inc in self.incidents.values():
            if etype == "sos" or inc["type"] != etype or inc["zone"] != zone:
                continue
            # the same thing seen again: update the open alert instead of opening another
            if inc["status"] in OPEN_STATUSES:
                inc["last_seen"] = now
                if bbox:
                    inc["bbox"] = bbox
                return inc
            # just closed by a human: ignore repeat sightings for a short while
            if inc["closed_at"] and now - inc["closed_at"] < self.event_cooldown:
                return inc
        return self.open_incident(etype, zone, bbox=bbox, confidence=confidence, note=note,
                                  sos_kind=sos_kind, source=source)

    def open_incident(self, etype, zone, bbox=None, confidence=None, note=None, sos_kind=None, source="camera"):
        now = time.time()
        rule = RULES.get(etype, {"label": etype.replace("_", " ").capitalize(),
                                 "kinds": ["security", "medical", "police", "rpf"], "pa": None})
        kinds = SOS_KINDS.get(sos_kind, rule["kinds"]) if etype == "sos" else rule["kinds"]
        zcfg = self.venue["zones"][zone]
        z = self.zones[zone]
        inc = {
            "id": next(self._ids), "type": etype, "label": rule["label"], "zone": zone,
            "zone_name": zcfg.get("name", zone), "camera": zcfg.get("camera"),
            "opened_at": now, "last_seen": now, "closed_at": None, "status": "new",
            "bbox": bbox, "confidence": confidence, "note": note, "sos_kind": sos_kind,
            "density": z["density"], "surge": z["surge"], "kinds": kinds, "pa": rule["pa"],
            "chain": [], "step": -1, "current_post": None, "sent_at": None,
            "accepted_by": None, "accepted_at": None, "timeline": [],
            "redirect": self.redirect_for(zone), "has_snapshot": False,
        }
        frame = self.frames.get(zcfg.get("camera"))
        if frame:
            self.snapshots[inc["id"]] = frame["jpeg"]
            inc["has_snapshot"] = True
        how = {"camera": f"Detected on {zcfg.get('camera', 'camera')}",
               "sos": "Reported from the SOS page",
               "dashboard": "Raised from the control room"}.get(source, "Detected")
        self._log(inc, "detected", how)
        self.incidents[inc["id"]] = inc
        self._trim()
        if self.on_incident:
            self.on_incident(inc)
        return inc

    def redirect_for(self, zone):
        """The least crowded neighbouring zone or exit to send people to."""
        best = None
        for target in self.venue["zones"][zone].get("redirect_to", []):
            if isinstance(target, dict):  # an exit that has no camera
                cand = {"id": None, "name": target.get("name"), "names": target.get("names", {}), "density": 0}
            elif target in self.zones:
                zt = self.zones[target]
                if zt["level"] == "critical":
                    continue
                cand = {"id": target, "name": self.venue["zones"][target].get("name", target),
                        "names": self.venue["zones"][target].get("names", {}),
                        "density": zt["density"] if zt["density"] is not None else 0}
            else:
                continue
            if best is None or cand["density"] < best["density"]:
                best = cand
        return best

    def _log(self, inc, kind, text):
        inc["timeline"].append({"t": time.time(), "kind": kind, "text": text})

    def _trim(self, keep=40):
        closed = sorted((i for i in self.incidents.values() if i["status"] not in OPEN_STATUSES),
                        key=lambda i: i["opened_at"])
        while len(self.incidents) > keep and closed:
            old = closed.pop(0)
            self.incidents.pop(old["id"], None)
            self.snapshots.pop(old["id"], None)

    def reset(self):
        self.incidents.clear()
        self.snapshots.clear()
        for zid in self.zones:
            self.zones[zid] = self._blank(zid)
        self.unknown_zones.clear()

    # ---------- what the page needs ----------
    def stats(self):
        times = [i["accepted_at"] - i["opened_at"] for i in self.incidents.values() if i["accepted_at"]]
        return {
            "open": sum(1 for i in self.incidents.values() if i["status"] in OPEN_STATUSES),
            "avg_response_s": round(sum(times) / len(times)) if times else None,
            "responses": len(times),
        }

    def zone_view(self):
        out = []
        for zid, z in self.zones.items():
            out.append({k: v for k, v in z.items() if k not in ("history", "crit_since", "calm_since",
                                                                 "level_from_pipeline", "mtc_from_pipeline",
                                                                 "mtc_sent")})
        return out

    def incident_view(self):
        incs = sorted(self.incidents.values(),
                      key=lambda i: (i["status"] not in OPEN_STATUSES, -i["opened_at"]))
        return incs[:12]
