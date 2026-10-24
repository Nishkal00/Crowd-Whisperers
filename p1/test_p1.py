import numpy as np

from p1.vision import DownDetector, Person, blur_faces, is_lying


def kps(shoulder_y, hip_y, shoulder_x=(40, 60), hip_x=(42, 58)):
    k = np.zeros((17, 3), np.float32)
    k[0] = (50, shoulder_y - 15, 0.9)                 # nose
    k[5], k[6] = (shoulder_x[0], shoulder_y, 0.9), (shoulder_x[1], shoulder_y, 0.9)
    k[11], k[12] = (hip_x[0], hip_y, 0.9), (hip_x[1], hip_y, 0.9)
    return k


def test_upright_torso_is_not_lying():
    assert not is_lying((30, 20, 70, 160), kps(40, 90))


def test_horizontal_torso_is_lying():
    k = np.zeros((17, 3), np.float32)
    k[5], k[6] = (100, 50, 0.9), (100, 70, 0.9)       # shoulders stacked vertically...
    k[11], k[12] = (160, 52, 0.9), (160, 68, 0.9)     # ...hips off to the side
    assert is_lying((90, 40, 200, 80), k)


def test_no_torso_no_lying():
    # a wide box alone is what someone hidden below the shoulders in a crowd looks like
    assert not is_lying((0, 0, 150, 60), None)
    k = kps(40, 90)
    k[11, 2] = k[12, 2] = 0.1  # hips not seen
    assert not is_lying((0, 0, 150, 60), k)


def lying(x=100.0):
    return Person((x, 50, x + 120, 90), 0.8, None, lying=True)


def test_down_only_after_lying_still_for_5_s():
    det = DownDetector(down_after_s=5)
    for i in range(10):  # seen every half second, wobbling a little
        assert det.update([lying(100 + i % 3)], i * 0.5) == []
    (d,) = det.update([lying(101)], 5.0)
    assert d.since == 0.0
    assert d.bbox_fraction(1000, 500) == [0.101, 0.1, 0.12, 0.08]


def test_moving_restarts_the_clock_and_upright_people_are_ignored():
    det = DownDetector(down_after_s=5)
    for i in range(15):                                # every 0.5 s; rolls 50 px at 2.5 s
        x = 100 if i < 5 else 150
        assert det.update([lying(x)], i * 0.5) == []   # never 5 s still in one place
    assert len(det.update([lying(150)], 7.5)) == 1     # 5 s since the move
    standing = Person((0, 0, 40, 120), 0.9, None, lying=False)
    assert det.update([standing], 7.0) == []


def test_short_detection_gaps_are_tolerated():
    det = DownDetector(down_after_s=5, forget_after_s=1.5)
    det.update([lying()], 0.0)
    det.update([], 1.0)                                # detector missed them for a frame
    assert len(det.update([lying()], 5.0)) == 0        # gap > forget_after_s since 0 s: new track
    det = DownDetector(down_after_s=5, forget_after_s=1.5)
    for t in (0.0, 1.2, 2.4, 3.6, 4.8):
        det.update([lying()], t)
    assert len(det.update([lying()], 5.5)) == 1


def test_blur_changes_only_the_head():
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (200, 200, 3), dtype=np.uint8)
    p = Person((80, 40, 120, 190), 0.9, kps(70, 120), lying=False)
    out = blur_faces(frame, [p])
    assert not np.array_equal(out[50:60, 45:55], frame[50:60, 45:55])   # around the nose
    assert np.array_equal(out[150:200], frame[150:200])                 # legs untouched
