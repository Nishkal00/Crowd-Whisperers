# Poltergeist: crowd-crush early warning

Crowd Whisperers · Hack-o-Ween · Voodoo Vision (computer vision) track.
Poltergeist predicts crowd crushes, spots incidents and sends the nearest help, using
cameras a venue already has.

## How the parts fit

```
camera / video
   │
   ▼
P1  p1/vision.py     people (YOLOv8), poses (YOLOv8-pose), faces blurred, someone lying still 5 s
   │  people's positions, "person down"
   ▼
P2  p2/              people per m² per zone, trend → minutes to critical, density heatmap
   │  density, prediction
   ▼
P3  p3/              optical flow (surge, scatter) + risk engine → SAFE / WARNING / CRITICAL per zone
   │  picture, zone numbers + level, events          (pipeline.py does this wiring)
   ▼
P4  p4/              control room page, Banshee dispatch (Telegram), PA in English, Hindi, Kannada
```

Every part reads its zones, floor areas and thresholds from **`p4/venue.json`**. Zone
polygons there are 0–1 fractions of the camera image; draw them with **Draw a zone** under
the video in the control room.

## Run it

```bash
pip install -r requirements.txt
python run.py --video clip.mp4
```

Then open **http://localhost:8000**. `run.py` starts the control room, plays the video in
real time through P1–P3 and streams everything to it. Other ways to run it:

```bash
python run.py --camera 0                       # webcam
python run.py --video clip.mp4 --loop          # keep replaying, for a demo stand
python run.py --video clip.mp4 --record out.mp4   # also save what the control room shows
python run.py --video HT21-02/img1/%06d.jpg --venue p4/venue_stadium.json   # stadium footage, 3 gates
```

`p4/venue_stadium.json` is a second venue for the HT21-02 crowd footage (not in the repo).
Pass the same `--venue` to `p4/simulate.py` if you use it there too.

For the scripted 3-minute demo with no footage (fake P1–P3 numbers on the station photo):

```bash
cd p4
python server.py
python simulate.py --reset      # in a second terminal
```

Telegram dispatch needs a bot token in `p4/.env` (see `p4/README.md`). Only one laptop may
run the server with that token at a time.

## Tests

```bash
python -m pytest p1 p2 p3 p4 tests
```

`tests/test_pipeline.py` checks the P1 → P4 wiring with a stand-in for YOLO, so it runs
without a GPU.

## Known limits

- **Packed crowds are undercounted.** YOLO finds about 40 of the hundreds of people in the
  station photo, so densities read far too low there. The deck's plan is to switch to a
  density-map model (CSRNet) when a zone is packed; that isn't built yet.
- **"Person on the tracks"** comes only from `p4/simulate.py` and the SOS page; nothing in
  the video pipeline detects it yet (it would need a tracks area in `venue.json`).
- On an RTX 4060 laptop the pipeline handles about 3.5 frames a second; frames in between
  are skipped, and the surge and fall checks are timed so that is enough.

## Folders

| Folder / file  | Who | What |
|----------------|-----|------|
| `p1/`          | P1  | `vision.py` (detection, pose, face blur, person down), `p1_video.py` (preview), models |
| `p2/`          | P2  | `density.py`, `prediction.py`, `heatmap.py`, `demo.py` (Streamlit: `streamlit run p2/demo.py`) |
| `p3/`          | P3  | `flow.py`, `risk.py`, `zones.py`, synthetic demo (`python -m p3.demo`) |
| `p4/`          | P4  | control-room server, Banshee, PA, `venue.json`, `simulate.py` |
| `pipeline.py`  | all | runs P1 → P2 → P3 on each frame and sends the results to P4 |
| `run.py`       | all | starts the control room and the pipeline together |
| `ppts/`        |     | the decks and the hackathon guide |
