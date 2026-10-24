
import argparse
import cv2
from pathlib import Path
from ultralytics import YOLO

BASE = Path(__file__).resolve().parent
MODEL_PATH = BASE / "models" / "yolov8n-pose.pt"

SKELETON = [
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),
    (5, 11), (6, 12), (11, 12), (11, 13),
    (13, 15), (12, 14), (14, 16), (0, 1),
    (0, 2), (1, 3), (2, 4), (0, 5), (0, 6)
]

parser = argparse.ArgumentParser(
    description="Reusable CCTV person tracking and monitoring"
)
parser.add_argument(
    "--source", default="0",
    help="0 for webcam, video filepath, or authorized RTSP URL"
)
parser.add_argument("--threshold", type=int, default=6)
parser.add_argument("--frames", type=int, default=10)
parser.add_argument(
    "--no-blur", action="store_true",
    help="Disable face blur to improve speed"
)
args = parser.parse_args()

if args.threshold < 1 or args.frames < 1:
    raise SystemExit("threshold and frames must be positive integers")

source = int(args.source) if args.source.isdigit() else args.source

if not MODEL_PATH.exists():
    raise SystemExit(f"Model not found: {MODEL_PATH}")

print("Loading YOLO pose/tracking model...")
model = YOLO(str(MODEL_PATH))

face_model = None
if not args.no_blur:
    face_model = cv2.CascadeClassifier(
        cv2.data.haarcascades
        + "haarcascade_frontalface_default.xml"
    )

    if face_model.empty():
        raise SystemExit("Could not load the face detection model.")

cap = cv2.VideoCapture(source)

if isinstance(source, int):
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

if not cap.isOpened():
    raise SystemExit(f"Cannot open video source: {args.source}")

frame_number = 0
crowd_frames = 0
cached_faces = []

print("CCTV tracking started. Press q to quit.")

try:
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        frame_number += 1
        height, width = frame.shape[:2]

        results = model.track(
            frame,
            persist=True,
            classes=[0],
            conf=0.35,
            imgsz=416,
            tracker="bytetrack.yaml",
            verbose=False
        )

        result = results[0]
        output = frame.copy()

        # Detect faces periodically to reduce processing overhead.
        if face_model is not None and frame_number % 4 == 1:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(
                gray,
                (max(1, width // 2), max(1, height // 2))
            )

            faces = face_model.detectMultiScale(
                small,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(20, 20)
            )

            cached_faces = [
                (x * 2, y * 2, w * 2, h * 2)
                for x, y, w, h in faces
            ]

        # Blur detected face regions.
        if face_model is not None:
            for x, y, w, h in cached_faces:
                x1 = max(0, x - int(w * 0.2))
                y1 = max(0, y - int(h * 0.2))
                x2 = min(width, x + w + int(w * 0.2))
                y2 = min(height, y + h + int(h * 0.2))

                if x2 > x1 and y2 > y1:
                    roi = output[y1:y2, x1:x2]
                    output[y1:y2, x1:x2] = cv2.GaussianBlur(
                        roi, (31, 31), 0
                    )

        people_in_zone = 0

        if result.boxes is not None:
            for i, box in enumerate(result.boxes):
                x1, y1, x2, y2 = map(
                    int, box.xyxy[0].tolist()
                )

                x1 = max(0, min(width - 1, x1))
                x2 = max(0, min(width - 1, x2))
                y1 = max(0, min(height - 1, y1))
                y2 = max(0, min(height - 1, y2))

                if x2 <= x1 or y2 <= y1:
                    continue

                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2

                # Count people whose box centres lie in the central zone.
                if (
                    width * 0.2 <= cx <= width * 0.8
                    and height * 0.2 <= cy <= height * 0.9
                ):
                    people_in_zone += 1

                # Draw the person bounding box and tracking ID.
                cv2.rectangle(
                    output, (x1, y1), (x2, y2),
                    (255, 0, 0), 2
                )

                if box.id is not None:
                    track_id = int(box.id.item())
                    cv2.putText(
                        output,
                        f"ID {track_id}",
                        (x1, max(20, y1 - 7)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, (255, 0, 0), 2
                    )

                # Draw pose keypoints associated with this detection.
                if (
                    result.keypoints is None
                    or i >= len(result.keypoints)
                ):
                    continue

                keypoints = result.keypoints[i]
                points = keypoints.xy[0].cpu().numpy()

                if keypoints.conf is not None:
                    scores = keypoints.conf[0].cpu().numpy()
                else:
                    scores = [1.0] * len(points)

                def valid(j):
                    px, py = points[j]
                    return (
                        scores[j] >= 0.65
                        and x1 <= px <= x2
                        and y1 <= py <= y2
                    )

                # Keep only reliable points inside this person's box.
                valid_points = {}
                for j in range(len(points)):
                    if valid(j):
                        px, py = map(int, points[j])
                        valid_points[j] = (px, py)

                # Connect only keypoints whose endpoints are both valid.
                for a, b in SKELETON:
                    if a in valid_points and b in valid_points:
                        cv2.line(
                            output,
                            valid_points[a],
                            valid_points[b],
                            (0, 255, 0),
                            2
                        )

                # Draw reliable keypoints.
                for px, py in valid_points.values():
                    cv2.circle(
                        output, (px, py), 3, (0, 255, 0), -1
                    )

        # Crowd-threshold warning after consecutive qualifying frames.
        if people_in_zone >= args.threshold:
            crowd_frames += 1
        else:
            crowd_frames = 0

        if crowd_frames >= args.frames:
            cv2.putText(
                output,
                "CROWD THRESHOLD REACHED",
                (15, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (0, 0, 255), 2
            )

        cv2.imshow("P1 - CCTV Tracking", output)

        delay = 1 if isinstance(source, int) else 25
        if cv2.waitKey(delay) & 0xFF == ord("q"):
            break

finally:
    cap.release()
    cv2.destroyAllWindows()
    print("CCTV tracking stopped.")
