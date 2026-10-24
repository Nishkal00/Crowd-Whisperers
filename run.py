"""Run the whole of Poltergeist: the control room (P4) plus the vision pipeline (P1-P3).

    python run.py --video clip.mp4        # starts http://localhost:8000 and plays the clip in real time
    python run.py --camera 0              # webcam
    python run.py --video clip.mp4 --loop --record out.mp4
    python run.py --video HT21-02/img1/%06d.jpg --venue p4/venue_stadium.json   # an image sequence

If a control room is already running at --server, it is used instead of starting one.
For the scripted demo without any footage, use `python p4/server.py` and `python p4/simulate.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import cv2

from p4.poltergeist_client import Poltergeist, local_ipv4
from pipeline import Pipeline

ROOT = Path(__file__).parent
VENUE = ROOT / "p4" / "venue.json"


def server_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(local_ipv4(url) + "/api/venue", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def start_server(url: str, venue: Path) -> subprocess.Popen | None:
    if server_up(url):
        print(f"Using the control room already running at {url}")
        return None
    port = url.rsplit(":", 1)[-1].split("/")[0]
    env = {**os.environ, "PORT": port, "VENUE": str(venue.resolve())}
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT / "p4", env=env)
    for _ in range(60):
        if server_up(url):
            return proc
        if proc.poll() is not None:
            raise SystemExit("The control room server stopped while starting; see the error above.")
        time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"The control room didn't start at {url}.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", help="video file")
    src.add_argument("--camera", type=int, help="camera index, e.g. 0 for the webcam")
    ap.add_argument("--venue", default=str(VENUE), help="zones, areas and help posts (default: p4/venue.json)")
    ap.add_argument("--cam-id", default="cam1", help="which camera in venue.json this feed is")
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--no-server", action="store_true", help="don't start or send to the control room")
    ap.add_argument("--loop", action="store_true", help="start the video again when it ends")
    ap.add_argument("--fast", action="store_true", help="process every frame as fast as possible, not in real time")
    ap.add_argument("--record", help="also save the control-room picture to this .mp4")
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--device", help="YOLO device, e.g. cpu or 0 (default: GPU if there is one)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        # "/m²" in alerts; line buffering so the log shows up live when piped to a file
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    cap = cv2.VideoCapture(args.video if args.video else args.camera)
    if not cap.isOpened():
        raise SystemExit(f"Can't open {args.video or f'camera {args.camera}'}.")
    ok, frame = cap.read()
    if not ok:
        raise SystemExit("The video has no frames.")
    size = (frame.shape[1], frame.shape[0])
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if not 1 < fps <= 120:  # image sequences (e.g. HT21-02/img1/%06d.jpg) report nonsense
        fps = 25.0
    venue_path = Path(args.venue)
    venue = json.loads(venue_path.read_text(encoding="utf-8"))

    server = None if args.no_server else start_server(args.server.rstrip("/"), venue_path)
    sender = None if args.no_server else Poltergeist(args.server, camera=args.cam_id)

    from p1.vision import Vision
    print("Loading the models...")
    vision = Vision(device=args.device)
    vision.warmup(frame)
    pipe = Pipeline(venue, size, camera=args.cam_id, vision=vision, sender=sender)
    writer = (cv2.VideoWriter(args.record, cv2.VideoWriter_fourcc(*"mp4v"), fps if args.video else 10, size)
              if args.record else None)
    print(f"Zones: {', '.join(z.name for z in pipe.zones)}   ({size[0]}x{size[1]}, {fps:.0f} fps)")
    if not args.no_server:
        print(f"Control room: {args.server}   (Ctrl+C to stop)")

    start = time.monotonic()
    consumed = 1           # frames read from the file so far, including skipped ones
    processed = 0
    last_report = 0.0
    try:
        while True:
            ts = consumed / fps if args.video else time.monotonic() - start
            if args.max_seconds is not None and ts > args.max_seconds:
                break
            res = pipe.step(frame, ts)
            processed += 1
            if writer is not None:
                writer.write(res.feed)
            for a in res.risk.alerts:
                print(f"[{ts:6.1f}s] {a.level.name:8} {a.zone_name}: {a.message}")
            if time.monotonic() - last_report >= 5:
                last_report = time.monotonic()
                levels = ", ".join(f"{r.zone_name} {r.level.name.lower()}" for r in res.risk.zones.values())
                rate = processed / max(time.monotonic() - start, 1e-6)
                print(f"[{ts:6.1f}s] {res.people} people, {rate:.1f} fps processed | {levels}")

            # next frame: in real time, skip frames we're too slow for and wait when we're early
            if args.video and not args.fast:
                due = int((time.monotonic() - start) * fps)
                while consumed < due - 1 and cap.grab():
                    consumed += 1
                early = consumed / fps - (time.monotonic() - start)
                if early > 0:
                    time.sleep(early)
            ok, frame = cap.read()
            if not ok and args.video and args.loop:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            if not ok:
                break
            consumed += 1
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if writer is not None:
            writer.release()
        if server is not None:
            print("Stopping the control room.")
            server.terminate()


if __name__ == "__main__":
    main()
