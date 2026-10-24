"""Risk engine: one level per zone (SAFE / WARNING / CRITICAL), plus alerts for dispatch.

Each tick it takes what the rest of the pipeline knows about every zone:
  density              P2  people per m²
  minutes_to_critical  P2  inflow-based prediction
  flow                 P3  FlowAnalyzer output (surge / scatter)
  incidents            P1  e.g. {"type": "person_down", "zone_id": "platform_1"}
and hands P4 each zone's level and status, and an alert whenever a zone gets worse
or something new happens in it.

Levels follow Prof. G. Keith Still's guidance: under 4 /m² is safe, 4 to 6 is a
warning, 6+ (or a surge) is critical. Every threshold is tunable per venue.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .flow import ZoneFlow
from .zones import Zone


class Level(IntEnum):
    SAFE = 0
    WARNING = 1
    CRITICAL = 2


LEVEL_COLORS = {Level.SAFE: "#55CC88", Level.WARNING: "#F5C542", Level.CRITICAL: "#F0524F"}

# Which kinds of help post suit each cause, best first. Same kinds and order as the
# dispatch rules in P4's engine.py, which does the actual dispatch in the full system.
RESPONDERS = {
    "density": ("security", "police", "rpf"),
    "eta": ("security", "police", "rpf"),
    "surge": ("security", "police", "rpf"),
    "scatter": ("security", "police"),
    "person_down": ("medical",),
    "on_tracks": ("rpf", "police", "security"),
}
DEFAULT_RESPONDERS = ("security", "police")

# Flow events and incidents alert on their own; density and eta alert only when the level rises.
_QUIET_KINDS = {"density", "eta"}


@dataclass
class RiskConfig:
    warning_density: float = 4.0    # people per m²
    critical_density: float = 6.0
    hysteresis: float = 0.3         # density must drop this far below a threshold to leave a level
    confirm_s: float = 2.0          # a density or eta reading must hold this long to raise the level
    hold_s: float = 10.0            # a level is kept this long after its cause clears
    eta_warn_min: float = 10.0      # "critical in N min" makes a zone WARNING within this horizon
    surge_min_density: float = 3.0  # below this a surge is people walking fast: WARNING, not CRITICAL
    incident_ttl_s: float = 30.0    # an incident stays active this long after it was last reported

    @classmethod
    def from_venue(cls, venue: dict, **overrides) -> "RiskConfig":
        """Take the density thresholds from venue.json, so every part uses the same ones."""
        th = venue.get("thresholds", {})
        return cls(warning_density=float(th.get("warning", cls.warning_density)),
                   critical_density=float(th.get("critical", cls.critical_density)), **overrides)


@dataclass
class ZoneRisk:
    zone_id: str
    zone_name: str
    level: Level
    status: str                 # what the dashboard badge says, e.g. "CRITICAL IN 6 MIN"
    density: float | None
    minutes_to_critical: float | None
    reasons: list[str]          # human-readable, e.g. ["6.4 /m²", "crowd surging"]
    kinds: list[str]            # machine-readable causes, e.g. ["density", "surge"]
    flow: ZoneFlow | None = None

    @property
    def color(self) -> str:
        return LEVEL_COLORS[self.level]

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "level": self.level.name,
            "color": self.color,
            "status": self.status,
            "density": self.density,
            "minutes_to_critical": self.minutes_to_critical,
            "reasons": self.reasons,
            "kinds": self.kinds,
            "flow": self.flow.to_dict() if self.flow else None,
        }


@dataclass
class Alert:
    id: str
    ts: float
    zone_id: str
    zone_name: str
    level: Level
    kinds: list[str]
    message: str                # e.g. "6.4 /m², crowd surging, person down"
    responders: list[str]       # kinds of help post, best first: "security", "police", "rpf", "medical"
    redirect_to: dict | None    # calmer zone to send the crowd to, for gate advice and the PA
    location: Any = None        # the zone's location from the zones file
    snapshot: str | None = None  # photo from the incident, if P1 sent one

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "level": self.level.name,
            "kinds": self.kinds,
            "message": self.message,
            "responders": self.responders,
            "redirect_to": self.redirect_to,
            "location": self.location,
            "snapshot": self.snapshot,
        }


@dataclass
class RiskUpdate:
    ts: float
    zones: dict[str, ZoneRisk]
    alerts: list[Alert]

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "zones": {zid: r.to_dict() for zid, r in self.zones.items()},
            "alerts": [a.to_dict() for a in self.alerts],
        }


@dataclass
class _ZoneState:
    level: Level = Level.SAFE
    up_since: float | None = None    # raw level above current since, awaiting confirm_s
    down_since: float | None = None  # raw level below current since, awaiting hold_s
    incidents: dict = field(default_factory=dict)  # key -> (incident, last reported)
    alerted: set = field(default_factory=set)      # causes already sent in an alert


class RiskEngine:
    def __init__(self, zones: list[Zone], config: RiskConfig | None = None):
        self.zones = {z.id: z for z in zones}
        self.cfg = config or RiskConfig()
        self._state = {z.id: _ZoneState() for z in zones}
        self._alert_count = 0

    def update(
        self,
        ts: float,
        density: dict[str, float] | None = None,
        minutes_to_critical: dict[str, float] | None = None,
        flow: dict[str, ZoneFlow] | None = None,
        incidents: list[dict] | None = None,
    ) -> RiskUpdate:
        """Advance to time `ts` (seconds). Missing zones in any input simply mean "no data"."""
        density, etas, flow = density or {}, minutes_to_critical or {}, flow or {}
        self._track_incidents(ts, incidents or [])

        risks: dict[str, ZoneRisk] = {}
        causes: dict[str, dict[str, str]] = {}
        rose = set()
        for zid, zone in self.zones.items():
            st = self._state[zid]
            d, eta, fl = density.get(zid), etas.get(zid), flow.get(zid)
            raw, reasons, keys, urgent = self._assess(st, d, eta, fl)
            before = st.level
            self._step(st, raw, urgent, ts)
            if st.level > before:
                rose.add(zid)
            risks[zid] = ZoneRisk(zid, zone.name, st.level, self._status(st.level, eta), d, eta,
                                  reasons, list(dict.fromkeys(keys.values())), fl)
            causes[zid] = keys

        alerts = []
        for zid, keys in causes.items():
            st = self._state[zid]
            if st.level == Level.SAFE:
                st.alerted.clear()
                continue
            st.alerted &= set(keys)  # a cause that ended may alert again if it comes back
            loud = {k for k, kind in keys.items() if kind not in _QUIET_KINDS}
            if zid in rose or loud - st.alerted:
                alerts.append(self._alert(ts, zid, risks, keys))
                st.alerted |= set(keys)
        return RiskUpdate(ts, risks, alerts)

    def _track_incidents(self, ts: float, incidents: list[dict]) -> None:
        ttl = self.cfg.incident_ttl_s
        for st in self._state.values():
            st.incidents = {k: v for k, v in st.incidents.items() if ts - v[1] <= ttl}
        for inc in incidents:
            st = self._state.get(inc.get("zone_id"))
            if st is None:
                continue  # outside every configured zone
            key = str(inc.get("id") or f"{inc.get('type', 'incident')}@{inc['zone_id']}")
            st.incidents[key] = (inc, ts)

    def _assess(self, st: _ZoneState, d, eta, fl) -> tuple[Level, list[str], dict[str, str], bool]:
        """Level this tick's readings call for, why, and whether to skip confirm_s."""
        cfg = self.cfg
        level, reasons, keys, urgent = Level.SAFE, [], {}, False

        if d is not None:
            reasons.append(f"{d:.1f} /m²")
            critical = cfg.critical_density - (cfg.hysteresis if st.level >= Level.CRITICAL else 0)
            warning = cfg.warning_density - (cfg.hysteresis if st.level >= Level.WARNING else 0)
            if d >= critical:
                level = Level.CRITICAL
            elif d >= warning:
                level = Level.WARNING
            if level:
                keys["density"] = "density"

        if fl is not None and fl.surge:
            dense = d is None or d >= cfg.surge_min_density
            level = max(level, Level.CRITICAL if dense else Level.WARNING)
            reasons.append("crowd surging")
            keys["surge"], urgent = "surge", True
        if fl is not None and fl.scatter:
            level = Level.CRITICAL
            reasons.append("people scattering")
            keys["scatter"], urgent = "scatter", True

        for key, (inc, _) in st.incidents.items():
            kind = str(inc.get("type", "incident"))
            # someone down in a packed zone risks being trampled
            dense = d is not None and d >= cfg.warning_density
            level = max(level, Level.CRITICAL if dense else Level.WARNING)
            reasons.append(inc.get("label") or kind.replace("_", " "))
            keys[key], urgent = kind, True

        if eta is not None and eta <= cfg.eta_warn_min and level < Level.CRITICAL:
            level = max(level, Level.WARNING)
            reasons.append(f"critical in {_minutes(eta)} min")
            keys["eta"] = "eta"

        return level, list(dict.fromkeys(reasons)), keys, urgent

    def _step(self, st: _ZoneState, raw: Level, urgent: bool, ts: float) -> None:
        """Rise after confirm_s (at once for flow events and incidents); fall only after hold_s."""
        if raw > st.level:
            st.down_since = None
            if st.up_since is None:
                st.up_since = ts
            if urgent or ts - st.up_since >= self.cfg.confirm_s:
                st.level, st.up_since = raw, None
        elif raw < st.level:
            st.up_since = None
            if st.down_since is None:
                st.down_since = ts
            if ts - st.down_since >= self.cfg.hold_s:
                st.level, st.down_since = raw, None
        else:
            st.up_since = st.down_since = None

    def _status(self, level: Level, eta: float | None) -> str:
        if level == Level.WARNING and eta is not None and eta <= self.cfg.eta_warn_min:
            return f"CRITICAL IN {_minutes(eta)} MIN"
        return level.name

    def _alert(self, ts: float, zid: str, risks: dict[str, ZoneRisk], keys: dict[str, str]) -> Alert:
        zone, risk, st = self.zones[zid], risks[zid], self._state[zid]
        responders: list[str] = []
        for kind in keys.values():
            for who in RESPONDERS.get(kind, DEFAULT_RESPONDERS):
                if who not in responders:
                    responders.append(who)
        snapshot = next((inc["snapshot"] for inc, _ in st.incidents.values() if inc.get("snapshot")), None)
        self._alert_count += 1
        return Alert(
            id=f"A{self._alert_count:04d}",
            ts=ts,
            zone_id=zid,
            zone_name=zone.name,
            level=risk.level,
            kinds=risk.kinds,
            message=", ".join(risk.reasons) or risk.status.lower(),
            responders=responders,
            redirect_to=self._redirect(zone, risks),
            location=zone.location,
            snapshot=snapshot,
        )

    def _redirect(self, zone: Zone, risks: dict[str, ZoneRisk]) -> dict | None:
        """Least crowded of the zone's exits (or any zone, if none are set) that is calmer than it."""
        here = risks[zone.id].level
        candidates = zone.exits or [zid for zid in self.zones if zid != zone.id]
        best = min(
            (risks[c] for c in candidates if c in risks and risks[c].level < here),
            key=lambda r: (r.level, r.density if r.density is not None else math.inf),
            default=None,
        )
        return {"zone_id": best.zone_id, "zone_name": best.zone_name} if best else None


def _minutes(m: float) -> int:
    return max(1, math.ceil(m))
