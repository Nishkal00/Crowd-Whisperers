# Poltergeist P4: control room, Banshee dispatch, PA voice

The control-room web page, the dispatch to help posts (Telegram), and the announcements
in English, Hindi and Kannada. P1–P3 send their results here, and this decides what's an
alert, who gets it, and what the crowd hears.

## Run it (5 minutes)

```bash
pip install -r requirements.txt
python server.py          # open http://localhost:8000
python simulate.py        # second terminal: fake P1–P3 so you can see everything working
```

`python simulate.py --speed 3` runs the 3-minute scenario faster. `--reset` clears old alerts.
On Windows, click **Allow** if the firewall asks; phones need that for the SOS page.

## For P1–P3: how to send data

Copy `poltergeist_client.py` next to your code. It uses only the standard library and sends
from background threads, so your loop never waits on the network.

```python
from poltergeist_client import Poltergeist
pg = Poltergeist("http://localhost:8000")          # camera="cam1" by default

pg.frame(frame)                                    # annotated BGR frame, as often as you like (sent at ~6 fps)
pg.zones({"platform_1": {"count": 312}})           # P1 can start with counts only
pg.zones({"platform_1": {"density": 6.4, "surge": True, "minutes_to_critical": 6}})
pg.event("person_down", zone="platform_1", bbox=[x, y, w, h])   # bbox as 0–1 fractions of the frame
```

| Field | Who sends it | If missing |
|---|---|---|
| `count` or `density` | P1 / P2 | needed (one of them). Density = count ÷ `area_m2` from venue.json |
| `surge` | P3 (optical flow) | treated as no surge |
| `minutes_to_critical` | P2 | P4 works it out from the last 30 s of density |
| `level` (`safe`/`warning`/`critical`) | P3 (risk engine) | P4 uses 4 and 6 people/m² from venue.json |

Event types: `person_down`, `scatter`, `on_tracks`, `sos`. The same event seen again just
updates the open alert, so it's fine to send it every frame.

Zone names must match `venue.json`. If you send one that isn't there, the page tells you.

## The control room page

- **Summary strip** (top): people counted, zones by level, the most crowded zone, the next zone
  to turn critical, and how many staff and people are subscribed on Telegram.
- **Camera**: the live picture, with zones outlined and the crowd heat shown on it.
- **Live heatmap**: each zone's density drawn over the venue layout, green (safe), amber (warning),
  red (critical). Zones with no recent data are grey.
- **Send on Telegram**: pick a zone and a message, then send it to:
  - **staff**: an alert with Accept / Can't go, dispatched like any other alert, with an optional note;
  - **people in that zone**: a crowd update in their own language.

  The page shows how many people and staff will get it and asks you to confirm first. Sending needs
  the bot to be connected. Staff alerts appear in the Alerts panel, and crowd updates are only sent to
  people who subscribed for that zone.

## Set up your own venue (`venue.json`)

- **zones**: one per area the camera sees.
  - `polygon`: the area on the camera image, as 0–1 fractions. Click **Draw a zone** under the video, click around the area, then **Copy points** and paste.
  - `area_m2`: real floor area, so counts become people/m². Pace it out or estimate.
  - `map`: the zone's rectangle on the venue map, in metres.
  - `redirect_to`: where to send people when it's crowded. Use zone ids, or `{"name": "Gate 4", "names": {"hi": "...", "kn": "..."}}` for exits without a camera.
  - `names`: Hindi and Kannada names used in announcements.
- **posts**: help posts with `kind` (`medical`, `security`, `police`, `rpf`), `pos` on the map in metres, and `telegram_chat_id`.
- `escalate_after_s`: how long a post has to accept before it moves on (60). Try 20 for the demo.

## Telegram (Banshee)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Copy `.env.example` to `.env` and paste the token after `TELEGRAM_BOT_TOKEN=`.
3. Optional: add `STAFF_CODE=1234` (any code) to `.env`, so only people with the code get staff alerts.
4. Start `python server.py`. When someone messages the bot, it asks **"Who are you?"**:
   - **Staff:** with a code set, they send `/staff 1234`. Staff get every dispatch alert with the
     snapshot and **Accept / Can't go**. The first to accept takes it, and everyone else's copy changes to
     "Taken by Security desk A (name)". If nobody accepts in time, it moves to the next-nearest post.
   - **At the event:** they pick a language (English, Hindi, Kannada) and the zone they're in. They
     get only crowd instructions for their zone, in their language, plus an all-clear when it's over.
     `/where` when they move, `/lang` to change language, `/stop` to leave.
5. Print **http://localhost:8000/qr**. Each zone has two codes: SOS, and "crowd updates on Telegram".
   Scanning the second opens the bot with that zone already set.

Everyone is saved in `subscribers.json`, so restarts keep them. Only run **one** `server.py` per
bot token; two copies fight over button taps.

Optional: put a `telegram_chat_id` on a post in `venue.json` (for example a Telegram group for
the medical team). That post's alerts then go only to that chat.

## PA voice

The page builds the announcement for the current alert, or for the most crowded zone,
and sends people to the least crowded neighbour. **Play announcement** plays a short wail,
then English, Hindi and Kannada. With **Turn on sound** pressed, new alerts play automatically.

Audio comes from Google's voice (gTTS), which needs internet and is cached in `pa_cache/`.
Without internet, the page uses the computer's own voice; Windows usually has English and
Hindi but not Kannada. **Have a Hindi and a Kannada speaker check the texts in `pa.py`.**

## SOS QR codes

Open **http://localhost:8000/qr** and print it. People scan the code in their zone, tap what's
wrong, and the alert goes through the same dispatch. Their page then shows who's coming.
Phones must be on the same Wi-Fi as the laptop. If campus Wi-Fi blocks that, use a phone
hotspot and set `PUBLIC_URL` in `.env` to the laptop's address on it.

## Real footage: CroHD / HT21-02 (stadium exit at night)

One fixed camera, Full-HD, 25 fps, 2 min 13 s, about 220 people in view, with head annotations.
From the CroHD dataset (Sundararaman et al., CVPR 2021), hosted by MOTChallenge for non-commercial use. Credit it on screen.

- Quick: open https://motchallenge.net/vis/HT21-02 and save the "Raw Sequence" video.
- Full (frames + ground truth, about 4 GB): the train set from https://motchallenge.net/data/Head_Tracking_21/.

```bash
set VENUE=venue_stadium.json          # Windows cmd   (PowerShell: $env:VENUE="venue_stadium.json")
python server.py
python simulate.py --venue venue_stadium.json --video HT21-02.webm       # or HT21-02/img1/%06d.jpg
```

`venue_stadium.json` splits the frame into Gate A, B and C. Use **Draw a zone** to trace the real
areas, and set `area_m2` to the real floor area of each. When P1–P3 run on this video, their real
counts replace the simulated ones. The simulator only shows the picture with scripted numbers.

Real footage of ~220 people in view usually works out to 2–4 people/m². That means Safe or
Warning, which is honest. To show a Critical alert, use the simulator's scripted numbers and say so.

## Demo script (about 2 minutes)

1. Run the server and `python simulate.py --reset`. Press **Turn on sound**.
2. **0:35** Platform 1 turns amber and shows "critical in N min". This is the prediction.
3. **1:06** It surges and turns red. The alert card pulses, the PA plays in three languages, and the Telegram alert arrives with a photo.
4. Tap **Accept** on the phone. The card and map update, and the average response time appears.
5. **1:32** A person on the tracks goes to railway police first.
6. Scan the QR code with a judge's phone and tap "Someone needs medical help". The medical post gets it.

## Files

| File | What it does |
|---|---|
| `server.py` | web server: receives data, streams the page, video, SOS, QR |
| `engine.py` | zone levels, prediction, rules that open and clear alerts |
| `banshee.py` | picks posts, Telegram buttons, escalation |
| `pa.py` | announcement texts and the voice cache |
| `static/` | the control-room page and the SOS page |
| `simulate.py` | fake P1–P3 scenario on the demo photo |
| `poltergeist_client.py` | the helper P1–P3 import |
| `venue.json` | zones, map and help posts |

Demo photo: "Mumbai morning rush" by Rakesh Krishna Kumar, CC BY-SA 2.0, via Wikimedia
Commons, with faces blurred. Fonts: Overpass and Noto Sans (SIL Open Font License).
