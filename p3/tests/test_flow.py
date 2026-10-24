import cv2
import numpy as np

from p3.flow import FlowAnalyzer, FlowConfig
from p3.zones import Zone, whole_frame

W, H, FPS = 320, 240, 10.0


def texture(seed: int = 0) -> np.ndarray:
    """Blurred noise twice the frame size, so it can be shifted without running out."""
    rng = np.random.default_rng(seed)
    noise = cv2.GaussianBlur(rng.uniform(0, 255, (2 * H, 2 * W)).astype(np.float32), (0, 0), 2)
    return cv2.normalize(noise, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def shifted(tex: np.ndarray, dx: float) -> np.ndarray:
    m = np.float32([[1, 0, dx - W / 2], [0, 1, -H / 2]])
    return cv2.warpAffine(tex, m, (W, H), flags=cv2.INTER_LINEAR)


def zoomed(tex: np.ndarray, scale: float) -> np.ndarray:
    crop = tex[H // 2:H // 2 + H, W // 2:W // 2 + W]
    m = cv2.getRotationMatrix2D((W / 2, H / 2), 0, scale)
    return cv2.warpAffine(crop, m, (W, H), flags=cv2.INTER_LINEAR)


def run(frames, zones=None, cfg=None):
    analyzer = FlowAnalyzer(zones or [whole_frame(W, H)], (W, H), cfg or FlowConfig(warmup_s=1.0))
    return [analyzer.update(f, i / FPS) for i, f in enumerate(frames)]


def test_surge_detected_with_direction():
    tex = texture()
    # 2 s of slow drift, then the whole view moves right at 3 px/frame
    xs = np.cumsum([0.15] * 20 + [3.0] * 20)
    res = run([shifted(tex, x) for x in xs])
    last = res[-1]["frame"]
    assert last.surge and not last.scatter
    assert last.coherence > 0.8
    assert last.direction_deg < 15 or last.direction_deg > 345  # pointing right
    assert not any(r["frame"].surge for r in res[1:20])


def test_scatter_detected_when_motion_spreads_outward():
    tex = texture(1)
    scales = [1.0] * 20 + [1.04 ** k for k in range(1, 16)]
    res = run([zoomed(tex, s) for s in scales])
    last = res[-1]["frame"]
    assert last.scatter and not last.surge
    assert last.radial > 0.5
    assert last.coherence < 0.4


def test_calm_scene_raises_nothing():
    tex = texture(2)
    rng = np.random.default_rng(0)
    xs = np.cumsum(rng.uniform(-0.2, 0.2, 40))
    res = run([shifted(tex, x) for x in xs])
    assert not any(r["frame"].surge or r["frame"].scatter for r in res[1:])


def test_only_the_moving_zone_surges():
    tex = texture(3)
    left = Zone("left", "Left", [(0, 0), (W // 2, 0), (W // 2, H), (0, H)])
    right = Zone("right", "Right", [(W // 2, 0), (W, 0), (W, H), (W // 2, H)])
    still = shifted(tex, 0)
    frames = []
    for x in np.cumsum([0.1] * 20 + [3.0] * 20):
        f = still.copy()
        f[:, W // 2:] = shifted(tex, x)[:, W // 2:]
        frames.append(f)
    last = run(frames, zones=[left, right])[-1]
    assert last["right"].surge
    assert not last["left"].surge and not last["left"].scatter


def test_slow_first_frame_still_gets_a_warm_up():
    # e.g. YOLO loading onto the GPU: the second frame arrives 6 s after the first
    tex = texture(4)
    analyzer = FlowAnalyzer([whole_frame(W, H)], (W, H), FlowConfig(warmup_s=1.0))
    analyzer.update(shifted(tex, 0), 0.0)
    first = analyzer.update(shifted(tex, 3.0), 6.0)["frame"]
    assert not first.surge and first.baseline > 0
    for i in range(1, 25):
        res = analyzer.update(shifted(tex, 3.0 + 0.15 * i), 6.0 + i / FPS)["frame"]
    assert not res.surge and not res.scatter


def test_first_frame_returns_nothing():
    analyzer = FlowAnalyzer([whole_frame(W, H)], (W, H))
    assert analyzer.update(shifted(texture(), 0), 0.0) == {}
    assert analyzer.arrows() == []
