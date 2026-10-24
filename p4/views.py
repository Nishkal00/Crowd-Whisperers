"""Extra camera angles cut from another camera's picture, and where the zones fall in them.

A camera in venue.json with "from": "cam1" and a "quad" (four corners in cam1's picture, as
0-1 fractions: top-left, top-right, bottom-right, bottom-left) is a perspective close-up of
that part of cam1. The simulator uses this to draw the view; the server uses it to place
the zone outlines on it, so both always agree.
"""
import numpy as np

CORNERS = [[0, 0], [1, 0], [1, 1], [0, 1]]


def homography(src, dst):
    """3x3 matrix taking the four points `src` onto the four points `dst`."""
    a, b = [], []
    for (x, y), (u, v) in zip(src, dst):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b += [u, v]
    h = np.linalg.solve(np.array(a, float), np.array(b, float))
    return np.append(h, 1.0).reshape(3, 3)


def project(h, points):
    p = np.c_[np.asarray(points, float), np.ones(len(points))] @ h.T
    return p[:, :2] / p[:, 2:3]


def zone_views(venue):
    """{zone_id: {camera: polygon}} for every derived camera that can see the zone."""
    out = {}
    for cam, c in venue.get("cameras", {}).items():
        if not c.get("quad"):
            continue
        h = homography(c["quad"], CORNERS)
        for zid, z in venue["zones"].items():
            if z.get("polygon") and z.get("camera") == c.get("from"):
                poly = project(h, z["polygon"])
                if not ((poly >= 0) & (poly <= 1)).all(axis=1).any():
                    continue  # the zone is entirely outside this view
                out.setdefault(zid, {})[cam] = [[round(float(x), 4), round(float(y), 4)] for x, y in poly]
    return out


def pil_coeffs(quad, src_size, out_size):
    """Coefficients for PIL's Image.transform(size, Image.PERSPECTIVE, ...): output pixel -> source pixel."""
    sw, sh = src_size
    ow, oh = out_size
    h = homography([[0, 0], [ow, 0], [ow, oh], [0, oh]], [[x * sw, y * sh] for x, y in quad])
    return tuple(h.flatten()[:8])
