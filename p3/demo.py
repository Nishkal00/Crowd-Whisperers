"""Run P3 on a video (or the built-in synthetic scene) and write an annotated clip and alert log.

  python -m p3.demo                     # synthetic station: surge, person down, scatter
  python -m p3.demo --video clip.mp4 --zones p4/venue.json --density platform_1=6.4

Zones come from P4's venue.json, the same file the control room uses.

Outputs go to p3/out/: demo.mp4, alerts.jsonl (one alert per line) and state.json
(the latest level of every zone, rewritten each tick, for the dashboard to poll).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2

from .flow import FlowAnalyzer
from .overlay import draw_banner, draw_overlay
from .risk import LEVEL_COLORS, RiskConfig, RiskEngine
from .synthetic import SyntheticCrowd
from .zones import load_zones, whole_frame

HERE = Path(__file__).parent
VENUE = HERE.parent / "p4" / "venue.json"
FPS = 10.0
SYNTHETIC_SIZE = (960, 540)
SYNTHETIC_SECONDS = 22.0


def scripted_inputs(t: float) -> tuple[dict, dict, list]:
    """What P2 (density, prediction) and P1 (incidents) would report at `t` in the synthetic scene."""
    density = {
        "platform_1": 4.2 + min(max(t - 6, 0) / 4, 1) * 2.2,  # 4.2 -> 6.4 between 6 s and 10 s
        "platform_2": 3.6 + min(t / 14, 1) * 1.0,             # 3.6 -> 4.6 over 14 s
        "footbridge": 0.8,
    }
    eta = {"platform_2": max(14 - t / 2, 1)} if t >= 4 else {}
    incidents = [{"type": "person_down", "zone_id": "platform_1", "duration_s": round(t - 12, 1)}] if t >= 12 else []
    return density, eta, incidents


def scripted_motion(sim: SyntheticCrowd, t: float) -> None:
    w, h = SYNTHETIC_SIZE
    if t >= 7:
        sim.surge("platform_1", (4.3, -4.3))  # Platform 1 pushes up the platform towards the footbridge
    if t >= 17:
        sim.scatter("platform_2", (0.88 * w, 0.4 * h), 6)  # something on Platform 2 sends people running


def parse_density(values: list[str] | None, zone_ids: list[str]) -> dict[str, float]:
    """`--density 5.2` sets every zone; `--density platform_1=6.4` sets one."""
    out: dict[str, float] = {}
    for v in values or []:
        if "=" in v:
            zid, num = v.split("=", 1)
            out[zid.strip()] = float(num)
        else:
            out.update({zid: float(v) for zid in zone_ids})
    return out


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", help="video file or camera index; omit for the synthetic scene")
    ap.add_argument("--zones", help="venue.json or a zones list (default: p4/venue.json for synthetic, "
                                    "whole frame for video)")
    ap.add_argument("--density", action="append", help="fixed density, '5.2' or 'zone_id=5.2' (repeatable)")
    ap.add_argument("--every", type=int, default=1, help="video only: analyse every Nth frame, for speed")
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--show", action="store_true", help="also show a live window")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # "/m²" in alerts

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    synthetic = args.video is None
    if synthetic:
        zones = load_zones(args.zones or VENUE, SYNTHETIC_SIZE)
        sim = SyntheticCrowd(zones, SYNTHETIC_SIZE)
        size, fps = sim.size, FPS
        max_s = args.max_seconds or SYNTHETIC_SECONDS
        cap = None
    else:
        cap = cv2.VideoCapture(int(args.video) if args.video.isdigit() else args.video)
        if not cap.isOpened():
            raise SystemExit(f"can't open video: {args.video}")
        size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        fps = (cap.get(cv2.CAP_PROP_FPS) or 25.0) / max(args.every, 1)
        zones = load_zones(args.zones, size) if args.zones else [whole_frame(*size)]
        max_s = args.max_seconds
    fixed_density = parse_density(args.density, [z.id for z in zones])

    zones_file = args.zones or (VENUE if synthetic else None)
    data = json.loads(Path(zones_file).read_text(encoding="utf-8")) if zones_file else {}
    analyzer = FlowAnalyzer(zones, size)
    engine = RiskEngine(zones, RiskConfig.from_venue(data) if isinstance(data, dict) else None)
    writer = cv2.VideoWriter(str(out_dir / "demo.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    alerts_log = (out_dir / "alerts.jsonl").open("w", encoding="utf-8")
    banner = None

    i = 0
    try:
        while True:
            t = i / fps
            if max_s is not None and t > max_s:
                break
            if synthetic:
                scripted_motion(sim, t)
                frame = sim.step()
                density, eta, incidents = scripted_inputs(t)
            else:
                for _ in range(max(args.every, 1) - 1):
                    cap.grab()
                ok, frame = cap.read()
                if not ok:
                    break
                density, eta, incidents = fixed_density, {}, []

            flows = analyzer.update(frame, t)
            upd = engine.update(t, density=density, minutes_to_critical=eta, flow=flows, incidents=incidents)
            for a in upd.alerts:
                line = f"[{t:5.1f}s] {a.level.name} {a.zone_name}: {a.message} -> {', '.join(a.responders)}"
                if a.redirect_to:
                    line += f"; route to {a.redirect_to['zone_name']}"
                print(line)
                alerts_log.write(json.dumps(a.to_dict(), ensure_ascii=False) + "\n")
                banner = (line, LEVEL_COLORS[a.level])
            write_json_atomic(out_dir / "state.json", upd.to_dict())

            shown = draw_overlay(frame, zones, upd.zones, analyzer)
            cv2.putText(shown, f"P3 optical flow + risk engine   t={t:4.1f}s", (12, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 1, cv2.LINE_AA)
            if banner:
                draw_banner(shown, *banner)
            writer.write(shown)
            if args.show:
                try:
                    cv2.imshow("p3", shown)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break
                except cv2.error:
                    args.show = False  # headless OpenCV build: keep writing the file
            i += 1
    finally:
        writer.release()
        alerts_log.close()
        if cap is not None:
            cap.release()

    print(f"wrote {out_dir / 'demo.mp4'} ({i} frames), alerts.jsonl, state.json")


if __name__ == "__main__":
    main()
