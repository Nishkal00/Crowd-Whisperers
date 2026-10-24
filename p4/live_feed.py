"""Play a crowd video as a live camera feed, with the true count for each zone in every frame.

    python live_feed.py                                       # HT21-02 on the station forecourt (cam4)
    python live_feed.py --venue venue_stadium.json --camera cam1   # the same footage on the stadium's three gates
    python live_feed.py --speed 2                             # twice real time

The footage is MOTChallenge HT21. Every person's head position in every frame comes from the
ground-truth file, so each zone's count is the number of heads inside its outline in that frame.
Zone areas are assumptions in the venue file, so the density each zone shows is only as good as those.
"""
import argparse
import bisect
import collections
import configparser
import json
import os
import time

import cv2

from poltergeist_client import Poltergeist

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SEQUENCE = os.path.join(ROOT, "HT21", "HT21", "train", "HT21-02")


def read_heads(gt_path, width, height):
    heads = collections.defaultdict(list)
    with open(gt_path, encoding="utf-8") as f:
        for line in f:
            p = line.split(",")
            if len(p) >= 6:
                x, y, w, h = (float(v) for v in p[2:6])
                heads[int(p[0])].append(((x + w / 2) / width, (y + h / 2) / height))
    return heads


def inside(point, polygon):
    """Ray casting: is the point inside the polygon (0-1 fractions)?"""
    x, y = point
    hit = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sequence", default=DEFAULT_SEQUENCE, help="an HT21 sequence folder")
    ap.add_argument("--venue", default=os.path.join(HERE, "venue.json"), help="the venue whose zones to count")
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--camera", default="cam4", help="the camera in the venue this footage is")
    ap.add_argument("--zone", help="count only this zone (default: every zone of that camera)")
    ap.add_argument("--speed", type=float, default=1.0, help="1 = the footage's own frame rate")
    ap.add_argument("--width", type=int, default=960, help="send frames at this width to keep it light")
    ap.add_argument("--no-loop", action="store_true")
    args = ap.parse_args()

    ini = configparser.ConfigParser()
    ini.read(os.path.join(args.sequence, "seqinfo.ini"))
    fps = float(ini["Sequence"]["frameRate"])
    length = int(ini["Sequence"]["seqLength"])
    width, height = int(ini["Sequence"]["imWidth"]), int(ini["Sequence"]["imHeight"])
    # seqinfo.ini names the folder, but the HT21 download puts the frames in img1
    img_dir = next((os.path.join(args.sequence, d) for d in (ini["Sequence"].get("imDir", "img"), "img1")
                    if os.path.isdir(os.path.join(args.sequence, d))), None)
    if img_dir is None:
        raise SystemExit(f"No frame folder in {args.sequence}")
    heads = read_heads(os.path.join(args.sequence, "gt", "gt.txt"), width, height)
    # the download can be missing some frames: play the ones there are, skipping the gaps
    available = sorted(int(f[:-4]) for f in os.listdir(img_dir) if f.endswith(".jpg") and f[:-4].isdigit())
    if not available:
        raise SystemExit(f"No frames in {img_dir}")

    venue = json.load(open(args.venue, encoding="utf-8"))
    zones = {zid: z for zid, z in venue["zones"].items()
             if z.get("camera") == args.camera and z.get("polygon") and (not args.zone or zid == args.zone)}
    if not zones:
        raise SystemExit(f"No zones for camera {args.camera} in {args.venue}")
    print(f"Live feed: {os.path.basename(args.sequence)} -> {args.camera}, counting "
          + ", ".join(f"{zid} ({z.get('area_m2')} m², estimated)" for zid, z in zones.items())
          + f". {fps:.0f} fps footage at {args.speed}x")

    pg = Poltergeist(args.server, camera=args.camera)
    start = time.monotonic()
    sent = 0
    try:
        while True:
            # the footage plays at its own rate; the frame shown is the one due right now
            due = 1 + int((time.monotonic() - start) * fps * args.speed)
            i = bisect.bisect_left(available, due)
            if i == len(available) or available[i] > length:
                if args.no_loop:
                    break
                start = time.monotonic()
                continue
            frame_no = available[i]
            img = cv2.imread(os.path.join(img_dir, f"{frame_no:06d}.jpg"))
            if img is None:
                raise SystemExit(f"Can't read frame {frame_no} in {img_dir}")
            if img.shape[1] > args.width:
                img = cv2.resize(img, (args.width, int(img.shape[0] * args.width / img.shape[1])),
                                 interpolation=cv2.INTER_AREA)
            counts = {zid: sum(inside(p, z["polygon"]) for p in heads.get(frame_no, []))
                      for zid, z in zones.items()}
            pg.frame(img)
            pg.zones({zid: {"count": n} for zid, n in counts.items()})
            sent += 1
            if sent % 100 == 0:
                print(f"frame {frame_no}/{length}: " + ", ".join(f"{zid} {n}" for zid, n in counts.items()))
            time.sleep(0.15)  # about 6 updates a second, the most the client sends
    except KeyboardInterrupt:
        pass
    print("stopped")


if __name__ == "__main__":
    main()
