"""
Synthetic hand-drawn shape dataset generator (60x80 grayscale).

Shapes are drawn as a single pen stroke with hand-drawn imperfections
(wobble, crooked/curved sides, overshoot tails or holes at the closing point),
plus augmentation (rotation, line thickness, size) and optional IMU-style
flaws (drift, axis stretch/skew, jitter, uneven speed).

Output layout (one folder per class, same as the course datasets):
    OUT_ROOT/train/<class>/*.png
    OUT_ROOT/val/<class>/*.png
    OUT_ROOT/test/<class>/*.png

No pixel-identical image appears twice anywhere in the dataset.
"""

import hashlib
from pathlib import Path

import cv2
import numpy as np

# ===================== EDIT THESE =====================
OUT_ROOT = Path(__file__).resolve().parents[1] / "data" / "shapes_dataset"  # <repo>/data

# Remove "Triangle" for the 3-class version.
CLASSES = ["Circle", "Figure Eight", "Square", "Triangle"]

# Images per class in each split.
N_PER_CLASS = {"train": 2000, "val": 500, "test": 500}

IMG_H, IMG_W = 60, 80          # same resolution as the CNN/SNN labs
BACKGROUND = 255               # white background
FOREGROUND = 0                 # black lines
SEED = 42                      # fixed seed -> reproducible dataset

# --- Augmentation ---
ROT_RANGE_DEG = (-180, 180)    # rotation
THICK_RANGE_PX = (1.0, 3.0)    # line thickness per image (constant within a stroke)
MAX_THICK_FRACTION = 14        # line at most (shape height / 14) px, so small shapes don't fill in
GAP_PROB = 0.45                # share of drawings with a hole where the stroke should close
GAP_RANGE = (0.03, 0.08)       # hole size, as a fraction of the outline length
HEIGHT_RANGE_PX = (18, 46)     # shape size (larger side, in px of the 60 px height)
MIN_TRI_COMPACTNESS = 0.40     # slivers allowed; redraw if thinner than this
                               # (4*pi*area/perimeter^2: equilateral = 0.60, a line = 0)

# --- IMU-style flaws ---
IMU_FLAW_PERCENT = 5          # 0 = none (off), 50 = moderate, 100 = strong
IMU_RANDOMIZE = True           # True: each image gets a random level between 0 and IMU_FLAW_PERCENT
                               # False: every image gets exactly IMU_FLAW_PERCENT
IMU_SAMPLES = 150              # samples per drawing (~1.5 s at 100 Hz)
# ======================================================

SS = 4                         # internal supersampling factor for smooth lines


# ---------- helpers ----------
def periodic_smooth(pts, width):
    """Round corners: moving average along a closed path (wraps around)."""
    if width < 2:
        return pts
    k = np.ones(width) / width
    pad = np.vstack([pts[-width:], pts, pts[:width]])
    sm = np.stack([np.convolve(pad[:, i], k, mode="same") for i in range(2)], 1)
    return sm[width:-width]


def polygon_outline(rng, verts, n=600, bow=0.0):
    """Closed path through vertices, sampled by arc length; sides bowed in/out."""
    v = np.vstack([verts, verts[:1]])
    seg = np.linalg.norm(np.diff(v, axis=0), axis=1)
    cum = np.concatenate([[0], np.cumsum(seg)]) / seg.sum()
    u = np.linspace(0, 1, n, endpoint=False)
    k = np.searchsorted(cum, u, side="right") - 1
    f = (u - cum[k]) / (cum[k + 1] - cum[k])
    p = v[k] + (v[k + 1] - v[k]) * f[:, None]
    d = v[k + 1] - v[k]
    nrm = np.stack([d[:, 1], -d[:, 0]], 1) / (np.linalg.norm(d, axis=1, keepdims=True) + 1e-9)
    side_bow = rng.normal(0, bow, len(verts))[k]
    return p + nrm * (side_bow * np.sin(np.pi * f))[:, None]


def resample_stroke(path, u_start, length):
    """Walk along a closed path from u_start for 'length' laps (>1 = overshoot)."""
    n = len(path)
    idx = (u_start + np.linspace(0, length, int(n * length))) * n
    i0 = np.floor(idx).astype(int) % n
    i1 = (i0 + 1) % n
    f = (idx - np.floor(idx))[:, None]
    return path[i0] * (1 - f) + path[i1] * f


def compactness(p):
    """4*pi*area/perimeter^2 of a closed path: 0.60 equilateral triangle, -> 0 for a sliver."""
    area = 0.5 * abs(np.dot(p[:, 0], np.roll(p[:, 1], 1)) - np.dot(p[:, 1], np.roll(p[:, 0], 1)))
    per = np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum()
    return 4 * np.pi * area / per ** 2


# ---------- shape outlines (unit-ish size, centred at 0, y down) ----------
# Each returns (closed path, start position as a fraction of the outline).
def square(rng):
    a = rng.uniform(0.85, 1.15)                          # slightly rectangular
    v = np.array([[-a, -1], [a, -1], [a, 1], [-a, 1]], float)
    v += rng.normal(0, 0.08, v.shape)                    # crooked corners
    path = polygon_outline(rng, v, bow=0.06)
    path = periodic_smooth(path, int(rng.choice([1, 8, 20, 35])))  # sharp .. rounded corners
    return path, rng.uniform(-0.03, 0.03)               # start near a corner


def circle(rng):
    t = np.linspace(0, 2 * np.pi, 600, endpoint=False)
    asp = rng.uniform(1.1, 1.6)                          # hand-drawn circles are rarely round
    rad = 1 + sum(rng.normal(0, 0.07 / k) * np.cos(k * t + rng.uniform(0, 2 * np.pi))
                  for k in (2, 3, 4))                    # egg-shaped / lumpy
    path = np.stack([asp * rad * np.cos(t), rad * np.sin(t)], 1)
    path[:, 1] *= 1 / asp ** 0.3
    return path, rng.uniform(0.70, 0.95)


def eight(rng):
    t = np.linspace(0, 2 * np.pi, 600, endpoint=False) - np.pi / 2   # start at the top
    w = rng.uniform(0.30, 0.55)                          # narrow
    top, bot = rng.uniform(0.75, 1.25, 2)                # unequal loops
    y = np.sin(t)
    x = w * np.sin(2 * t) * np.where(y < 0, top, bot)
    path = np.stack([x, y], 1)
    path[:, 0] += rng.uniform(-0.35, 0.35) * path[:, 1]  # slant
    return path, 0.0


def triangle(rng):
    while True:
        base = rng.uniform(1.4, 1.8)
        h = rng.uniform(0.7, 1.4)
        apex_x = rng.uniform(-0.7, 0.7) * base / 2       # lopsided apex
        v = np.array([[apex_x, -h / 2], [base / 2, h / 2], [-base / 2, h / 2]])
        if rng.random() < 0.3:                           # sometimes point-down
            v[:, 1] *= -1
        v += rng.normal(0, 0.08, v.shape)
        path = polygon_outline(rng, v, bow=0.08)         # curved sides
        path = periodic_smooth(path, int(rng.choice([1, 6, 15])))
        if compactness(path) >= MIN_TRI_COMPACTNESS:     # sliver limit
            return path, rng.uniform(-0.02, 0.02)


SHAPES = {"Square": square, "Circle": circle, "Figure Eight": eight, "Triangle": triangle}


# ---------- IMU-style flaws ----------
def imu_flaws(rng, pts, k):
    """Simulate artefacts of a path reconstructed from IMU samples (k scales all effects)."""
    # Uneven drawing speed: resample with a time warp (fast parts -> fewer samples).
    n = len(pts)
    u = np.linspace(0, 1, IMU_SAMPLES)
    warp = u + 0.08 * k * np.sin(2 * np.pi * u + rng.uniform(0, 2 * np.pi)) / (2 * np.pi)
    idx = np.clip(warp, 0, 1) * (n - 1)
    pts = np.stack([np.interp(idx, np.arange(n), pts[:, i]) for i in range(2)], 1)
    s = u[:, None]
    # Axis stretch + skew (sensor scale / misalignment errors).
    sx, sy = rng.uniform(1 - 0.15 * k, 1 + 0.15 * k, 2)
    skew = rng.uniform(-0.15, 0.15) * k
    pts = pts @ np.array([[sx, skew], [0, sy]]).T
    # Drift: bias integrated twice (~ time^2) plus random-walk drift.
    bias = rng.normal(0, 0.15 * k, 2)
    walk = np.cumsum(rng.normal(0, 0.012 * k, (IMU_SAMPLES, 2)), axis=0)
    pts = pts + bias * s ** 2 + walk
    # Sensor jitter on every sample.
    return pts + rng.normal(0, 0.012 * k, pts.shape)


# ---------- one drawing ----------
def draw_sample(name, rng):
    """Draw one randomised hand-drawn shape and return a uint8 HxW image."""
    path, u0 = SHAPES[name](rng)

    # Either a hole at the closing point, or an overshoot tail.
    has_gap = rng.random() < GAP_PROB
    if has_gap:
        length, pre = 1 - rng.uniform(*GAP_RANGE), 0.0
    else:
        length = 1 + (rng.uniform(0.01, 0.06) if name == "Figure Eight" else rng.uniform(0.04, 0.15))
        pre = rng.uniform(0.0, 0.04)                     # start slightly before the corner
    pts = resample_stroke(path, u0 - pre, length + pre)

    # Tails peel away from the outline instead of re-tracing it.
    lap = np.linspace(-pre, length, len(pts))
    tang = np.gradient(pts, axis=0)
    nrm = np.stack([tang[:, 1], -tang[:, 0]], 1) / (np.linalg.norm(tang, axis=1, keepdims=True) + 1e-9)
    lead = np.clip(-lap / max(pre, 1e-6), 0, 1)
    over = np.zeros_like(lap) if has_gap else np.clip((lap - 1) / max(length - 1, 1e-6), 0, 1)
    off = (rng.choice([-1, 1]) * rng.uniform(0.05, 0.15) * lead ** 1.5
           + rng.choice([-1, 1]) * rng.uniform(0.08, 0.25) * over ** 1.5)
    pts = pts + nrm * off[:, None]

    # Gentle low-frequency wobble.
    s = np.linspace(0, 1, len(pts))[:, None]
    for k in (1, 2, 3):
        ph = rng.uniform(0, 2 * np.pi, 2)
        pts = pts + rng.normal(0, 0.02 / k, 2) * np.sin(k * 2 * np.pi * s + ph)

    if IMU_FLAW_PERCENT > 0:
        level = IMU_FLAW_PERCENT / 50.0                  # percent -> strength (100 % = 2 = strong)
        if IMU_RANDOMIZE:
            level = rng.uniform(0.0, level)
        pts = imu_flaws(rng, pts, level)

    # Rotation.
    a = np.deg2rad(rng.uniform(*ROT_RANGE_DEG))
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    pts = pts @ R.T
    pts -= (pts.max(0) + pts.min(0)) / 2

    # Size (larger side, rotation-safe), never touching the border.
    height = rng.uniform(*HEIGHT_RANGE_PX)
    span = pts.max(0) - pts.min(0)
    pts *= height / max(span[1], span[0] * IMG_H / IMG_W)
    span = pts.max(0) - pts.min(0)
    if span[0] > IMG_W - 8 or span[1] > IMG_H - 8:
        pts *= min((IMG_W - 8) / span[0], (IMG_H - 8) / span[1])

    # Position near the centre, clamped inside the frame.
    pts = pts + np.array([IMG_W / 2, IMG_H / 2]) + rng.uniform([-12, -6], [12, 6])
    pts += (np.clip(4 - pts.min(0), 0, None)
            - np.clip(pts.max(0) - (np.array([IMG_W, IMG_H]) - 4), 0, None))

    # Line thickness: constant within the stroke, capped for small shapes.
    thick_px = min(rng.uniform(*THICK_RANGE_PX), height / MAX_THICK_FRACTION)
    t_ss = max(1, int(round(thick_px * SS)))

    # Render at SS x resolution (round joins, sub-pixel), then downsample.
    big = np.full((IMG_H * SS, IMG_W * SS), BACKGROUND, np.uint8)
    pts_fp = np.round(pts * SS * 16).astype(np.int32).reshape(-1, 1, 2)   # shift=4 -> 1/16 px
    cv2.polylines(big, [pts_fp], isClosed=False, color=FOREGROUND,
                  thickness=t_ss, lineType=cv2.LINE_AA, shift=4)
    return cv2.resize(big, (IMG_W, IMG_H), interpolation=cv2.INTER_AREA)


def main():
    rng = np.random.default_rng(SEED)

    # Hashes of every image saved so far, across ALL splits and classes.
    # Any new image whose pixels match one of these is discarded and redrawn,
    # so no pixel-identical image can appear twice in the dataset.
    seen = set()

    for split, n in N_PER_CLASS.items():
        for name in CLASSES:
            out_dir = OUT_ROOT / split / name
            out_dir.mkdir(parents=True, exist_ok=True)

            i = 0
            rejected = 0
            while i < n:
                img = draw_sample(name, rng)

                key = hashlib.sha256(img.tobytes()).digest()
                if key in seen:
                    rejected += 1   # duplicate -> draw a new one instead
                    continue
                seen.add(key)

                cv2.imwrite(str(out_dir / f"{split}_{i:05d}.png"), img)
                i += 1

            print(f"[{split}] {name}: {n} images, {rejected} duplicates redrawn -> {out_dir}")


if __name__ == "__main__":
    main()
