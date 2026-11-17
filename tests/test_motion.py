"""Edge video tahlili (oddiy RTSP kameralar uchun) — sun'iy kadrlar bilan."""
import numpy as np

from edge.motion import MotionConfig, MotionDetector, polygon_mask

H, W = 90, 160
DOOR = {"region_id": 1, "kind": "door", "polygon": [[0.1, 0.2], [0.3, 0.2], [0.3, 0.9], [0.1, 0.9]]}
DOOR2 = {"region_id": 2, "kind": "door", "polygon": [[0.6, 0.2], [0.8, 0.2], [0.8, 0.9], [0.6, 0.9]]}


def scene():
    rng = np.random.default_rng(1)
    return (rng.integers(90, 140, size=(H, W))).astype(np.uint8)


def with_person(base, x0, w=14):
    f = base.copy()
    f[25:80, x0:x0 + w] = 20  # qorong'i siluet
    return f


def run(det, frames, fps=5.0, t0=0.0):
    out = []
    for i, f in enumerate(frames):
        out += det.process(f, t0 + i / fps)
    return out


def test_polygon_mask():
    m = polygon_mask(DOOR["polygon"], H, W)
    assert m[50, 30] and not m[50, 100]
    assert abs(m.mean() - 0.2 * 0.7) < 0.02


def test_passerby_gives_only_enter():
    base = scene()
    det = MotionDetector([DOOR, DOOR2], H, W)
    frames = [base] * 10
    # odam chapdan o'ngga o'tib ketadi (~0.8 s ichida 1-eshik zonasidan o'tadi)
    frames += [with_person(base, x) for x in range(0, 150, 8)]
    frames += [base] * 20
    ev = run(det, frames)
    types = [(e["type"], e.get("region_id")) for e in ev]
    assert ("enter", 1) in types and ("dwell", 1) not in types


def test_person_standing_at_door_gives_dwell():
    base = scene()
    det = MotionDetector([DOOR, DOOR2], H, W)
    frames = [base] * 10 + [with_person(base, 20)] * 20 + [base] * 10  # 4 soniya eshik oldida
    ev = run(det, frames)
    assert [e["type"] for e in ev if e.get("region_id") == 1] == ["enter", "dwell"]
    assert not [e for e in ev if e.get("region_id") == 2]


def test_covered_camera_tamper_and_restore():
    base = scene()
    det = MotionDetector([DOOR], H, W, MotionConfig(covered_seconds=3))
    black = np.full((H, W), 5, np.uint8)
    ev = run(det, [base] * 10 + [black] * 25 + [base] * 5)
    tam = [e for e in ev if e["type"] == "tamper"]
    assert [e["state"] for e in tam] == ["active", "inactive"] and tam[0]["reason"] == "covered"
    assert not [e for e in ev if e["type"] == "dwell"]  # yopilgan paytda soxta dwell yo'q


def test_moved_camera_tamper():
    base = scene()
    det = MotionDetector([DOOR], H, W, MotionConfig(moved_seconds=5))
    other = np.random.default_rng(7).integers(0, 256, size=(H, W)).astype(np.uint8)  # butunlay boshqa sahna
    ev = run(det, [base] * 10 + [other] * 40)
    assert any(e["type"] == "tamper" and e["reason"] == "moved" for e in ev)
