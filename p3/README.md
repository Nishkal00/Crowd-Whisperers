# P3: optical flow + risk engine

P3's part of **Poltergeist** (Crowd Whisperers, Voodoo Vision track). It covers step 4
("Detect") and the risk levels from the Proposed Solution slide:

- **Optical flow** (`flow.py`): Farneback flow per zone catches **surges** (most of a zone
  suddenly moving fast one way) and **scatter** (a burst of fast motion in all directions,
  or outward from one spot). Each zone is judged against its own normal speed, learned in
  its first 3 s of measurements and then adapted slowly.
- **Risk engine** (`risk.py`): combines density and prediction (P2), flow (P3) and incidents
  (P1) into one level per zone.

| Level    | When                                                                 |
|----------|----------------------------------------------------------------------|
| SAFE     | under 4 /m²                                                          |
| WARNING  | 4 to 6 /m², P2 predicts critical within 10 min, a surge in a sparse zone, or an incident |
| CRITICAL | 6+ /m², a surge in a zone at 3+ /m², people scattering, or an incident in a 4+ /m² zone |

Thresholds follow Prof. G. Keith Still's guidance. The two density thresholds come from
`thresholds` in `p4/venue.json` (`RiskConfig.from_venue`); the rest are in `RiskConfig` and
`FlowConfig`. Levels rise after a reading holds for 2 s (at once for surges, scatter and
incidents) and drop only after 10 s, so the dashboard doesn't flicker.

## In the full system

`pipeline.py` at the repo root runs P3 on every frame. P3's level for each zone, and its
surge flag, go to the control room (P4), and a scatter goes as a `scatter` event. P4 then
opens the alerts, picks the help post and plays the PA, so P3's own `alerts` and
`redirect_to` are only used when P3 runs on its own (below).

## Run it on its own

From the repo root:

```bash
python -m p3.demo
```

That plays a synthetic station with the three zones from `p4/venue.json`: Platform 1 surges
towards the footbridge, someone goes down there, and people scatter on Platform 2. It writes
to `p3/out/`:

- `demo.mp4`: the camera view with zones in their risk colour and flow arrows
- `alerts.jsonl`: one alert per line
- `state.json`: every zone's latest level, rewritten each tick

On real footage (zones come from `venue.json`, drawn with the control room's **Draw a zone**):

```bash
python -m p3.demo --video clip.mp4 --zones p4/venue.json --density platform_1=6.4 --every 2
```

Tests: `python -m pytest p3`

## Using it from code

```python
from p3 import FlowAnalyzer, RiskConfig, RiskEngine, draw_flow_arrows, load_zones

zones = load_zones("p4/venue.json", frame_size=(w, h))   # polygons there are 0-1 fractions
flow = FlowAnalyzer(zones, frame_size=(w, h))
risk = RiskEngine(zones, RiskConfig.from_venue(venue))

for frame, ts in camera:                           # ts in seconds
    flows = flow.update(frame, ts)                 # {zone_id: ZoneFlow}
    update = risk.update(
        ts,
        density={"platform_1": 6.4},               # P2: people per m²
        minutes_to_critical={"platform_2": 6.0},   # P2: prediction
        flow=flows,                                # P3
        incidents=[{"type": "person_down", "zone_id": "platform_1",
                    "snapshot": "snap.jpg"}],      # P1: report every tick while it lasts
    )
    update.zones["platform_1"].level               # Level.SAFE / WARNING / CRITICAL
    draw_flow_arrows(frame, flow)                  # arrows only; P4 draws the zones
```

**Incidents** are dicts with `type` and `zone_id`; `id`, `snapshot` and `label` are
optional. Without an `id`, one incident of each type per zone is tracked. An incident stays
active for 30 s after it was last reported.

**Alerts** (`update.alerts`) are raised when a zone's level rises, or when a new surge,
scatter or incident appears in it, never repeatedly for the same cause:

```json
{"id": "A0004", "ts": 12.0, "zone_id": "platform_1", "zone_name": "Platform 1",
 "level": "CRITICAL", "kinds": ["density", "surge", "person_down"],
 "message": "6.4 /m², crowd surging, person down",
 "responders": ["security", "police", "rpf", "medical"],
 "redirect_to": {"zone_id": "footbridge", "zone_name": "Footbridge exit"},
 "location": [[10, 34], [140, 34], [140, 44], [10, 44]], "snapshot": null}
```

`responders` are kinds of help post, best first, matching P4's dispatch rules. `redirect_to`
is the least crowded of the zone's `redirect_to` zones that is calmer than it, or `null`.

## Files

| File           | What it does                                                    |
|----------------|-----------------------------------------------------------------|
| `flow.py`      | Optical flow per zone, surge and scatter detection, arrows      |
| `risk.py`      | Levels, hysteresis, alerts, responders, redirect                |
| `zones.py`     | Zone polygons, read from `p4/venue.json`                        |
| `overlay.py`   | Draws flow arrows (and, for the demo, zones and labels)         |
| `synthetic.py` | Moving-dot crowd for demos and testing without footage          |
| `demo.py`      | Runs P3 alone on a video or the synthetic scene                 |
