
"""P2: Draw a crowd-density heatmap over a video frame."""

import cv2
import numpy as np


ZONE_COLORS = {
    "SAFE": (60, 190, 100),       # Green
    "WARNING": (0, 190, 255),     # Yellow/orange
    "CRITICAL": (50, 50, 240),    # Red
}


def draw_heatmap(frame, zones, results, alpha=0.35):
    """Overlay colored rectangles and labels on a video frame.

    Args:
        frame: OpenCV video frame.
        zones: Dictionary mapping zone names to (x1, y1, x2, y2).
        results: Dictionary mapping zone names to density results.
        alpha: Strength of the color overlay.

    Returns:
        A new frame with the heatmap overlay.
    """
    if frame is None or frame.size == 0:
        raise ValueError("Input frame is empty.")

    overlay = frame.copy()
    output = frame.copy()

    for zone_name, coordinates in zones.items():
        if zone_name not in results:
            continue

        x1, y1, x2, y2 = map(int, coordinates)
        status = results[zone_name]["status"]
        density = results[zone_name]["density"]
        color = ZONE_COLORS.get(status, (150, 150, 150))

        # Color the zone on the overlay.
        cv2.rectangle(
            overlay, (x1, y1), (x2, y2), color, thickness=-1
        )

        # Draw the zone boundary and text on the output.
        cv2.rectangle(
            output, (x1, y1), (x2, y2), color, thickness=2
        )

        label = f"{zone_name}: {density:.1f} people/m2"
        cv2.putText(
            output,
            label,
            (x1 + 5, max(y1 + 22, 22)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
        )

        cv2.putText(
            output,
            status,
            (x1 + 5, max(y1 + 45, 45)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            color,
            2,
        )

    # Blend the colored overlay with the original frame.
    return cv2.addWeighted(
        overlay, alpha, output, 1.0 - alpha, 0
    )


# Colour stops for people_heatmap: (how many bodies overlap, BGR colour, opacity).
# Same green / amber / red as the control room.
HEAT_STOPS = [
    (0.0, (115, 191, 46), 0.0),
    (0.3, (115, 191, 46), 0.0),   # leave the faint edges of each blob clear
    (1.0, (115, 191, 46), 0.22),
    (2.0, (61, 194, 255), 0.35),
    (3.0, (31, 140, 255), 0.45),
    (4.0, (51, 64, 255), 0.55),
]


def people_heatmap(frame, boxes, mask=None, scale=4):
    """Density heatmap from detected people, drawn over a copy of the frame.

    Each person adds a soft blob about one body wide, so the colour shows how tightly
    people are packed: someone on their own stays faint green, a tight cluster turns red.
    Sizing each blob from its own box keeps far-away (smaller) people comparable to near ones.

    Args:
        frame: OpenCV video frame.
        boxes: (x1, y1, x2, y2) per person, in frame pixels.
        mask: optional boolean array the size of the frame; only colour where True.
        scale: the heat is computed at 1/scale resolution, for speed.
    """
    if frame is None or frame.size == 0:
        raise ValueError("Input frame is empty.")

    h, w = frame.shape[:2]
    gh, gw = max(1, h // scale), max(1, w // scale)
    heat = np.zeros((gh, gw), np.float32)

    for x1, y1, x2, y2 in boxes:
        sigma = max(1.0, 0.8 * min(x2 - x1, y2 - y1) / scale)
        cx = (x1 + x2) / 2 / scale
        cy = (y1 + 0.75 * (y2 - y1)) / scale  # lower body: where they take up floor space
        r = int(3 * sigma) + 1
        xa, xb = max(0, int(cx) - r), min(gw, int(cx) + r + 1)
        ya, yb = max(0, int(cy) - r), min(gh, int(cy) + r + 1)
        if xa >= xb or ya >= yb:
            continue
        xs = np.arange(xa, xb, dtype=np.float32) - cx
        ys = np.arange(ya, yb, dtype=np.float32) - cy
        heat[ya:yb, xa:xb] += np.exp(-(ys[:, None] ** 2 + xs[None, :] ** 2) / (2 * sigma * sigma))

    # colour the small grid through a lookup table, then let OpenCV scale it up
    idx = np.clip(heat * (255 / HEAT_STOPS[-1][0]), 0, 255).astype(np.uint8)
    lut = _heat_lut()
    color = cv2.resize(lut[idx, :3].astype(np.uint8), (w, h), interpolation=cv2.INTER_LINEAR)
    alpha = cv2.resize(lut[idx, 3], (w, h), interpolation=cv2.INTER_LINEAR)
    if mask is not None:
        alpha = alpha * mask.astype(np.float32)
    if not alpha.any():
        return frame.copy()
    return cv2.blendLinear(frame, color, 1 - alpha, alpha)


_LUT = None


def _heat_lut():
    """256 rows of (B, G, R, opacity) spanning HEAT_STOPS, built once."""
    global _LUT
    if _LUT is None:
        x = np.linspace(0, HEAT_STOPS[-1][0], 256)
        levels = [s[0] for s in HEAT_STOPS]
        cols = [np.interp(x, levels, [s[1][c] for s in HEAT_STOPS]) for c in range(3)]
        _LUT = np.stack(cols + [np.interp(x, levels, [s[2] for s in HEAT_STOPS])], axis=1).astype(np.float32)
    return _LUT


def create_demo_frame():
    """Create a synthetic frame to test the heatmap without a video."""
    frame = np.zeros((480, 800, 3), dtype=np.uint8)
    frame[:] = (55, 55, 55)

    cv2.putText(
        frame,
        "P2 Crowd Density Heatmap Demo",
        (30, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
    )

    return frame


if __name__ == "__main__":
    frame = create_demo_frame()

    zones = {
        "Gate 1": (40, 80, 260, 400),
        "Gate 2": (290, 80, 510, 400),
        "Gate 3": (540, 80, 760, 400),
    }

    results = {
        "Gate 1": {"density": 3.0, "status": "SAFE"},
        "Gate 2": {"density": 5.0, "status": "WARNING"},
        "Gate 3": {"density": 7.0, "status": "CRITICAL"},
    }

    heatmap = draw_heatmap(frame, zones, results)

    cv2.imshow("P2 Heatmap Demo", heatmap)
    cv2.waitKey(0)
    cv2.destroyAllWindows()
