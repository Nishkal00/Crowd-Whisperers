"""P1 on its own: webcam or video with people, poses and blurred faces.

    python -m p1.p1_video              # webcam
    python -m p1.p1_video clip.mp4     # a video file

For the whole system (counts, risk levels, dashboard) use run.py at the repo root.
"""
import sys

import cv2

from p1.vision import Vision


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else 0
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print("Cannot open the camera or video. Check camera permissions or the file path.")
        raise SystemExit(1)

    vision = Vision()
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    print("P1 running: person detection, pose estimation and face blur. Press q to quit.")
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        result = vision.process(frame, i / fps)
        out = result.frame
        for p in result.people:
            x1, y1, x2, y2 = map(int, p.box)
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255) if p.lying else (0, 200, 0), 2)
        for d in result.down:
            x1, y1, x2, y2 = map(int, d.box)
            cv2.putText(out, "PERSON DOWN", (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.putText(out, f"{len(result.people)} people", (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        try:
            cv2.imshow("P1 - Crowd Whisperers", out)
        except cv2.error:
            print("This OpenCV build can't open windows (opencv-python-headless). Use run.py for the dashboard.")
            break
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
        i += 1
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
