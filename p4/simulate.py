"""Fake P1-P3 so the control room can be demoed and tested on its own.

    python simulate.py                  # 3-minute scenario on the demo station photo, loops
    python simulate.py --speed 2        # same scenario, twice as fast
    python simulate.py --video clip.mp4 # use your own footage for the picture (numbers stay simulated)
    python simulate.py --video HT21-02/img1/%06d.jpg --venue venue_stadium.json   # an image sequence works too

With your own venue.json, the scenario's three curves are applied to your first three zones.

What happens (at --speed 1):
    0:00  everything calm
    0:35  Platform 1 reaches Warning, the page predicts minutes to critical
    1:06  Platform 1 surges, then turns Critical  -> crush-risk alert, Banshee dispatches security
    1:32  someone walks across the tracks        -> alert to RPF
    2:00  Platform 2 reaches Warning, the PA panel offers a redirect announcement
    2:30  things calm down, the crush alert clears by itself
"""
import argparse
import os
import random
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import views
from poltergeist_client import Poltergeist

HERE = os.path.dirname(os.path.abspath(__file__))
LOOP = 180

DENSITY = {
    "platform_1": [(0, 2.6), (35, 4.2), (70, 6.5), (125, 6.8), (150, 4.0), (180, 2.6)],
    "platform_2": [(0, 2.0), (80, 3.4), (120, 4.6), (160, 3.4), (180, 2.0)],
    "footbridge": [(0, 0.7), (90, 1.2), (130, 1.6), (180, 0.7)],
}
SURGE = {"platform_1": (66, 100)}
EVENTS = [(92, 98, "on_tracks", "platform_1", [0.568, 0.42, 0.036, 0.1])]


def curve(points, t):
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def heat_lut():
    stops = [(0.0, (0, 0, 0, 0)), (0.25, (46, 191, 115, 0)), (0.4, (46, 191, 115, 60)),
             (0.6, (255, 194, 61, 95)), (0.8, (255, 140, 31, 115)), (1.0, (255, 64, 51, 135))]
    lut = np.zeros((256, 4), np.uint8)
    for i in range(256):
        x = i / 255
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if a <= x <= b:
                f = (x - a) / (b - a)
                lut[i] = [round(ca[k] + (cb[k] - ca[k]) * f) for k in range(4)]
                break
    return lut


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--video", help="optional video file to show instead of the demo photo")
    ap.add_argument("--reset", action="store_true", help="clear old alerts before starting")
    ap.add_argument("--venue", default=os.path.join(HERE, "venue.json"), help="use the same file as the server")
    args = ap.parse_args()

    import json
    venue = json.load(open(args.venue, encoding="utf-8"))
    zone_ids = list(venue["zones"])
    demo_venue = all(z in venue["zones"] for z in DENSITY)
    # scenario curve -> your zone (the busiest curve goes to your first zone)
    mapping = {k: k for k in DENSITY} if demo_venue else dict(zip(DENSITY, zone_ids))
    if not demo_venue:
        print("Using the scenario on your zones:", ", ".join(f"{v} <- {k}" for k, v in mapping.items()))
    pg = Poltergeist(args.server, camera="cam1")
    if args.reset:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(args.server + "/api/reset", data=b"{}", method="POST"))

    base = Image.open(os.path.join(HERE, "demo", "platform_overview.jpg")).convert("RGBA")
    W, H = base.size
    tex = np.asarray(Image.open(os.path.join(HERE, "demo", "station_texture.png")).convert("L"), np.float32) / 255
    th, tw = tex.shape
    masks = {}
    for zid in (DENSITY if demo_venue and not args.video else []):
        m = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(m).polygon([(x * tw, y * th) for x, y in venue["zones"][zid]["polygon"]], fill=255)
        masks[zid] = np.asarray(m, np.float32) / 255
    lut = heat_lut()
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    except OSError:
        try:
            font = ImageFont.truetype("arialbd.ttf", 18)
        except OSError:
            font = ImageFont.load_default()

    cap = None
    if args.video:
        import cv2
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            raise SystemExit(f"Couldn't open {args.video}. For an image folder use a pattern like HT21-02/img1/%06d.jpg")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        if fps <= 1 or fps > 120:
            fps = 25

    cam_clients = {}  # one sender per derived camera, e.g. cam2 and cam3
    print(f"Simulating P1-P3 -> {args.server}  (Ctrl+C to stop)")
    start = time.time()
    last_zone_push = 0
    while True:
        t = ((time.time() - start) * args.speed) % LOOP
        dens = {z: max(0.0, curve(p, t) + random.uniform(-0.08, 0.08)) for z, p in DENSITY.items()}

        # ---- picture (what P1 + P2 would send) ----
        if cap is not None:
            # play at real speed: skip the frames that fall between our 5 updates a second
            for _ in range(max(0, round(fps * 0.2 * args.speed) - 1)):
                cap.grab()
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = cv2.VideoCapture(args.video)   # loop the footage
                continue
            pg.frame(frame)
        else:
            heat = np.zeros((th, tw), np.float32)
            for zid, m in masks.items():
                heat = np.maximum(heat, m * (0.35 + 0.65 * tex) * np.clip((dens[zid] - 1.5) / 5.0, 0, 1))
            rgba = Image.fromarray(lut[(np.clip(heat, 0, 1) * 255).astype(np.uint8)], "RGBA").resize((W, H), Image.BILINEAR)
            img = Image.alpha_composite(base, rgba).convert("RGB")
            d = ImageDraw.Draw(img, "RGBA")
            stamp = time.strftime("%H:%M:%S") + "   SIMULATED FEED"
            d.rounded_rectangle((12, 12, 24 + d.textlength(stamp, font=font), 42), 6, fill=(0, 0, 0, 140))
            d.text((18, 16), stamp, fill=(253, 249, 224), font=font)
            import io
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=72)
            pg.frame(buf.getvalue())
            # close-up views: the same picture, cut out in perspective for each derived camera
            for cam, cfg in venue.get("cameras", {}).items():
                if cfg.get("from") and cfg.get("quad"):
                    crop = img.transform((960, 540), Image.PERSPECTIVE,
                                         views.pil_coeffs(cfg["quad"], img.size, (960, 540)), Image.BILINEAR)
                    buf = io.BytesIO()
                    crop.convert("RGB").save(buf, "JPEG", quality=72)
                    cam_clients.setdefault(cam, Poltergeist(args.server, camera=cam)).frame(buf.getvalue())

        # ---- numbers (what P2 + P3 would send), twice a second ----
        if time.time() - last_zone_push >= 0.5:
            last_zone_push = time.time()
            payload = {}
            for zid, d_ in dens.items():
                if zid not in mapping:
                    continue
                s0, s1 = SURGE.get(zid, (-1, -1))
                payload[mapping[zid]] = {"density": round(d_, 2), "surge": s0 <= t < s1}
            pg.zones(payload)
            for t0, t1, etype, zone, bbox in EVENTS:
                if t0 <= t < t1 and zone in mapping:
                    if demo_venue and not args.video:
                        pg.event(etype, zone=zone, bbox=bbox, confidence=0.8)
                    else:  # the tracks event only makes sense on the demo photo
                        pg.event("person_down", zone=mapping[zone], confidence=0.8)
        time.sleep(0.2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
