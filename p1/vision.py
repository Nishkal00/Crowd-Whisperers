"""P1: people detection, pose, face blur and person-down detection with YOLOv8.

Two models per frame: the YOLOv8 person detector counts people (it finds two to four
times as many in a crowd as the pose model does), and YOLOv8-pose gives body keypoints
for spotting someone lying down and for finding heads to blur.

    vision = Vision()
    result = vision.process(frame, ts)   # ts in seconds
    result.people      # every person: box, confidence, keypoints, lying or not
    result.frame       # the frame with faces blurred, safe to show or send
    result.down        # people lying still for 5 s or more
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

MODELS = Path(__file__).parent / "models"

# COCO keypoint indices
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHOULDER, R_SHOULDER, L_HIP, R_HIP = 5, 6, 11, 12
HEAD = (NOSE, L_EYE, R_EYE, L_EAR, R_EAR)


@dataclass
class Person:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels
    conf: float
    keypoints: np.ndarray | None = None     # (17, 3): x, y, confidence
    lying: bool = False

    @property
    def foot(self) -> tuple[float, float]:
        """Where the person stands: bottom centre of the box."""
        x1, _, x2, y2 = self.box
        return (x1 + x2) / 2, y2

    @property
    def centre(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return (x1 + x2) / 2, (y1 + y2) / 2


@dataclass
class Down:
    """Someone who has been lying still for at least `down_after_s`."""
    id: int
    box: tuple[float, float, float, float]
    since: float     # when they were first seen lying still
    conf: float

    def bbox_fraction(self, width: int, height: int) -> list[float]:
        """[x, y, w, h] as 0-1 fractions of the frame, the format the control room expects."""
        x1, y1, x2, y2 = self.box
        return [round(x1 / width, 4), round(y1 / height, 4),
                round((x2 - x1) / width, 4), round((y2 - y1) / height, 4)]


@dataclass
class VisionResult:
    people: list[Person]
    frame: np.ndarray                 # faces blurred
    down: list[Down] = field(default_factory=list)


def is_lying(box, keypoints, min_kp_conf: float = 0.4) -> bool:
    """Torso closer to horizontal than vertical.

    Needs a clearly seen torso. A wide, flat box alone isn't enough: in a crowd, people
    hidden below the shoulders get boxes like that while standing still.
    """
    if keypoints is None:
        return False
    k = keypoints
    if not all(k[i, 2] >= min_kp_conf for i in (L_SHOULDER, R_SHOULDER, L_HIP, R_HIP)):
        return False
    sx, sy = (k[L_SHOULDER, :2] + k[R_SHOULDER, :2]) / 2
    hx, hy = (k[L_HIP, :2] + k[R_HIP, :2]) / 2
    angle = math.degrees(math.atan2(abs(sx - hx), abs(sy - hy) + 1e-6))  # 0 = upright
    return angle > 55


class _Track:
    def __init__(self, tid: int, person: Person, ts: float):
        self.id, self.box, self.conf = tid, person.box, person.conf
        self.anchor = person.centre   # where they were when they stopped moving
        self.since = self.last_seen = ts


class DownDetector:
    """Follows people who are lying down and reports those who stay still long enough."""

    def __init__(self, down_after_s: float = 5.0, forget_after_s: float = 1.5, still_frac: float = 0.35):
        self.down_after_s = down_after_s
        self.forget_after_s = forget_after_s  # tolerate a few frames where the detector misses them
        self.still_frac = still_frac          # moving more than this share of their size restarts the clock
        self._tracks: list[_Track] = []
        self._next_id = 1

    def update(self, people: list[Person], ts: float) -> list[Down]:
        self._tracks = [t for t in self._tracks if ts - t.last_seen <= self.forget_after_s]
        free = list(self._tracks)
        for p in (p for p in people if p.lying):
            t = max(free, key=lambda t: _iou(t.box, p.box), default=None)
            if t is None or _iou(t.box, p.box) < 0.2:
                self._tracks.append(_Track(self._next_id, p, ts))
                self._next_id += 1
                continue
            free.remove(t)
            size = max(p.box[2] - p.box[0], p.box[3] - p.box[1])
            if math.dist(p.centre, t.anchor) > self.still_frac * size:
                t.anchor, t.since = p.centre, ts
            t.box, t.conf, t.last_seen = p.box, p.conf, ts
        return [Down(t.id, t.box, t.since, t.conf) for t in self._tracks
                if ts - t.since >= self.down_after_s]


class Vision:
    def __init__(self, person_model: str | Path = MODELS / "yolov8n.pt",
                 pose_model: str | Path = MODELS / "yolov8n-pose.pt",
                 conf: float = 0.15, imgsz: int = 1280, pose_conf: float = 0.3, pose_imgsz: int = 640,
                 device: str | None = None, down_after_s: float = 5.0, haar_faces: bool = True,
                 haar_every: int = 3):
        from ultralytics import YOLO  # imported here so the rest of the package works without it

        # small, far-away people in a crowd need the larger input size and a low threshold
        self.person_model, self.pose_model = YOLO(str(person_model)), YOLO(str(pose_model))
        self.conf, self.imgsz, self.pose_conf, self.pose_imgsz = conf, imgsz, pose_conf, pose_imgsz
        self.device = device
        self.downs = DownDetector(down_after_s)
        # the pose keypoints find most heads; the face detector catches people YOLO missed
        self.face_detector = (cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
                              if haar_faces else None)
        self.haar_every = haar_every  # it's slow; faces barely move in a few frames, so reuse its boxes
        self._faces: list = []
        self._frames = 0

    def warmup(self, frame: np.ndarray) -> None:
        """The first call loads the models onto the GPU and takes several seconds."""
        self.detect(frame)

    def detect(self, frame: np.ndarray) -> list[Person]:
        r = self.person_model(frame, conf=self.conf, imgsz=self.imgsz, classes=[0],
                              device=self.device, verbose=False)[0]
        people = [Person(tuple(float(v) for v in b), float(c))
                  for b, c in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy())]

        pr = self.pose_model(frame, conf=self.pose_conf, imgsz=self.pose_imgsz, device=self.device, verbose=False)[0]
        if pr.keypoints is not None and len(pr.boxes):
            for b, c, k in zip(pr.boxes.xyxy.cpu().numpy(), pr.boxes.conf.cpu().numpy(), pr.keypoints.data.cpu().numpy()):
                box = tuple(float(v) for v in b)
                match = max(people, key=lambda p: _iou(p.box, box), default=None)
                if match is not None and _iou(match.box, box) >= 0.5 and match.keypoints is None:
                    match.keypoints = k
                else:  # someone only the pose model saw (e.g. lying down)
                    people.append(Person(box, float(c), k))
        for p in people:
            p.lying = is_lying(p.box, p.keypoints)
        return people

    def process(self, frame: np.ndarray, ts: float) -> VisionResult:
        people = self.detect(frame)
        down = self.downs.update(people, ts)
        if self.face_detector is not None and self._frames % self.haar_every == 0:
            self._faces = find_faces(frame, self.face_detector)
        self._frames += 1
        return VisionResult(people, blur_faces(frame, people, extra=self._faces), down)


def find_faces(frame: np.ndarray, face_detector, scale: float = 0.4) -> list[tuple]:
    """Frontal faces from OpenCV's Haar detector, as padded (x1, y1, x2, y2) boxes."""
    small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), None, fx=scale, fy=scale)
    out = []
    for x, y, fw, fh in face_detector.detectMultiScale(small, scaleFactor=1.2, minNeighbors=5, minSize=(12, 12)):
        pad = 0.2 * fw  # room for the face to move until the next look
        out.append(((x - pad) / scale, (y - pad) / scale, (x + fw + pad) / scale, (y + fh + pad) / scale))
    return out


def blur_faces(frame: np.ndarray, people: list[Person], face_detector=None, extra=()) -> np.ndarray:
    """Copy of `frame` with every head we can find blurred."""
    out = frame.copy()
    h, w = frame.shape[:2]
    regions = [r for r in (_head_box(p) for p in people) if r] + list(extra)
    if face_detector is not None:
        regions += find_faces(frame, face_detector)
    for x1, y1, x2, y2 in regions:
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(w, int(x2)), min(h, int(y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        k = max(15, (max(x2 - x1, y2 - y1) // 2) | 1)
        out[y1:y2, x1:x2] = cv2.GaussianBlur(out[y1:y2, x1:x2], (k, k), 0)
    return out


def _head_box(p: Person, min_kp_conf: float = 0.3):
    x1, y1, x2, y2 = p.box
    k = p.keypoints
    if k is not None:
        pts = k[list(HEAD)]
        pts = pts[pts[:, 2] >= min_kp_conf, :2]
        if len(pts):
            cx, cy = pts.mean(axis=0)
            spread = float(np.ptp(pts, axis=0).max()) if len(pts) > 1 else 0.0
            if k[L_SHOULDER, 2] >= min_kp_conf and k[R_SHOULDER, 2] >= min_kp_conf:
                spread = max(spread, 0.6 * float(np.linalg.norm(k[L_SHOULDER, :2] - k[R_SHOULDER, :2])))
            s = max(spread, 0.12 * max(x2 - x1, y2 - y1), 6.0)
            return cx - s, cy - s, cx + s, cy + s
    if p.lying:
        return None  # no idea which end the head is
    return x1, y1, x2, y1 + 0.22 * (y2 - y1)  # top of an upright box


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0
