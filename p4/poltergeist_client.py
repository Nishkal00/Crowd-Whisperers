"""Tiny helper for P1-P3: send frames, zone numbers and events to the P4 server.

    from poltergeist_client import Poltergeist
    pg = Poltergeist("http://localhost:8000")            # camera="cam1" by default

    pg.frame(frame)                                      # annotated OpenCV frame (BGR numpy) or JPEG bytes
    pg.zones({"platform_1": {"count": 312}})             # count OR density; optional: surge, level, minutes_to_critical
    pg.event("person_down", zone="platform_1", bbox=[x, y, w, h])   # bbox as 0-1 fractions of the frame

Everything is sent from background threads, so your vision loop never waits on the network.
Frames and zone numbers are "latest wins": if sending falls behind, only the newest is sent,
so the control room never shows old numbers. Events are all sent, in order.
If the server is down, data is dropped and a warning is printed once every 10 s.
Only uses the standard library (plus OpenCV if you pass numpy frames).
"""
import json
import queue
import threading
import time
import urllib.parse
import urllib.request


class Poltergeist:
    def __init__(self, url="http://localhost:8000", camera="cam1", max_fps=6, jpeg_quality=70, max_width=1280):
        # On Windows "localhost" tries IPv6 first and only falls back to IPv4 (what the server
        # listens on) after about 2 s, so every request took 2 s and the page fell behind.
        self.url = local_ipv4(url.rstrip("/"))
        self.camera = camera
        self.min_gap = 1.0 / max_fps
        self.jpeg_quality = jpeg_quality
        self.max_width = max_width
        self._latest_frame = None
        self._frame_lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._latest_zones = {}
        self._zones_lock = threading.Lock()
        self._zones_ready = threading.Event()
        self._jobs = queue.Queue(maxsize=200)
        self._last_warn = 0
        threading.Thread(target=self._frame_worker, daemon=True).start()
        threading.Thread(target=self._zones_worker, daemon=True).start()
        threading.Thread(target=self._json_worker, daemon=True).start()

    # ---------- public ----------
    def frame(self, frame):
        """Queue the newest frame. Older unsent frames are dropped."""
        with self._frame_lock:
            self._latest_frame = frame
        self._frame_ready.set()

    def zones(self, zones):
        """Queue the newest zone numbers, merged per zone into any not sent yet."""
        with self._zones_lock:
            for zid, m in zones.items():
                self._latest_zones[zid] = {**self._latest_zones.get(zid, {}), **m}
        self._zones_ready.set()

    def event(self, type, zone, bbox=None, confidence=None, note=None):
        self._put("/api/event", {"type": type, "zone": zone, "bbox": bbox, "confidence": confidence, "note": note})

    # ---------- internals ----------
    def _put(self, path, payload):
        try:
            self._jobs.put_nowait((path, payload))
        except queue.Full:
            pass

    def _warn(self, err):
        if time.time() - self._last_warn > 10:
            self._last_warn = time.time()
            print(f"[poltergeist] can't reach {self.url}: {err}")

    def _post(self, path, body, content_type):
        req = urllib.request.Request(self.url + path, data=body, headers={"Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.read()

    def _encode(self, frame):
        if isinstance(frame, (bytes, bytearray)):
            return bytes(frame)
        import cv2  # only needed for numpy frames
        h, w = frame.shape[:2]
        if w > self.max_width:
            frame = cv2.resize(frame, (self.max_width, int(h * self.max_width / w)))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
        return buf.tobytes() if ok else None

    def _frame_worker(self):
        last = 0
        while True:
            self._frame_ready.wait()
            wait = self.min_gap - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            with self._frame_lock:
                frame, self._latest_frame = self._latest_frame, None
                self._frame_ready.clear()
            if frame is None:
                continue
            last = time.time()
            try:
                data = self._encode(frame)
                if data:
                    self._post(f"/api/frame?camera={self.camera}", data, "image/jpeg")
            except Exception as e:
                self._warn(e)

    def _zones_worker(self):
        while True:
            self._zones_ready.wait()
            with self._zones_lock:
                zones, self._latest_zones = self._latest_zones, {}
                self._zones_ready.clear()
            if not zones:
                continue
            try:
                body = json.dumps({"camera": self.camera, "zones": zones}).encode("utf-8")
                self._post("/api/zones", body, "application/json")
            except Exception as e:
                self._warn(e)

    def _json_worker(self):
        while True:
            path, payload = self._jobs.get()
            try:
                self._post(path, json.dumps(payload).encode("utf-8"), "application/json")
            except Exception as e:
                self._warn(e)


def local_ipv4(url):
    """http://localhost:8000 -> http://127.0.0.1:8000; other addresses unchanged."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != "localhost":
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc="127.0.0.1" + (f":{parts.port}" if parts.port else "")))
