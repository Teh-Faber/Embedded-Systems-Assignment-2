#!/usr/bin/env python3
"""3D path of the recorded takes of one shape, for inspection.

    py imu_path.py ../data/imu/square            # writes ../data/imu/square/path.html
    py imu_path.py ../data/imu/square --open     # and opens it in the browser

For each saved take of each run in the shape folder (imu_tool.py record):
the gyro gives the orientation, the accelerometer minus gravity gives the
acceleration in a fixed frame with z up, and two integrations give the
position. The integration covers the motion of the take: from the rest just
before the motion starts to the rest just after it stops. Two drift
corrections, shown as two views on the page:

  open    velocity zero at both ends. This removes a constant acceleration
          error.
  closed  also the same position at both ends (the shape ends where it
          starts). This removes an acceleration error that changes linearly
          in time, for example from an accelerometer offset while the board
          turns. Use it for closed shapes (square, circle, triangle, ...).

The page starts with the open view. --view closed starts with the closed
view. The checkbox "closed shape" on the page switches between them.

Use the paths to inspect the takes, not as exact measurements. The rotation
of a path about the vertical axis is arbitrary.

A take is shown only while its CSV file is in the shape folder: delete the
file of a bad take and run the script again. A file that imu_tool.py has
since reused for a newer take belongs to the newer run.

Only numpy is needed. The HTML page has no external files.
"""

import argparse
import json
import webbrowser
from pathlib import Path

import numpy as np

G = 9.80665                 # m/s^2 per g
BEFORE_S, AFTER_S = 1.0, 1.5  # how far the motion of a take may reach before and after it
MOVE, REST = 15.0, 7.5        # motion score above MOVE: moving, below REST: at rest


def load_run(run_dir):
    """stream.csv as an int64 array and run.json as a dict."""
    d = np.loadtxt(run_dir / "stream.csv", delimiter=",", skiprows=1, dtype=np.int64, ndmin=2)
    meta = json.loads((run_dir / "run.json").read_text())
    return d, meta


def smooth(x, n):
    """Moving average over n samples, same length."""
    n = max(1, int(n))
    return np.convolve(x, np.ones(n) / n, mode="same")


def expm_so3(phi):
    """Rotation matrix of the rotation vector phi (Rodrigues)."""
    th = np.linalg.norm(phi)
    if th < 1e-12:
        return np.eye(3)
    k = phi / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * (K @ K)


def level_frame(f):
    """Body-to-world rotation with world z along the measured specific force
    at rest (that is, up). World x is the body x axis made horizontal."""
    z = f / np.linalg.norm(f)
    ref = np.array([1.0, 0, 0]) if abs(z[0]) < 0.9 else np.array([0, 1.0, 0])
    x = ref - z * (z @ ref)
    x /= np.linalg.norm(x)
    return np.vstack([x, np.cross(z, x), z])


def world_acceleration(t, f_g, w_rad, a, b):
    """Acceleration (m/s^2) in the world frame from sample a to sample b, and
    the time from a (s). Sample a is at rest: it gives the tilt and gravity."""
    f0 = f_g[max(0, a - 10):a + 1].mean(axis=0)
    C = level_frame(f0)
    g0 = np.linalg.norm(f0)
    n = b - a + 1
    acc = np.zeros((n, 3))
    for k in range(n):
        i = a + k
        acc[k] = (C @ f_g[i] - [0.0, 0.0, g0]) * G
        if k < n - 1:
            C = C @ expm_so3(w_rad[i] * (t[i + 1] - t[i]))
    return acc, t[a:b + 1] - t[a]


def cumtrapz(y, tt):
    """Running integral from 0, trapezoidal rule."""
    return np.vstack([np.zeros(y.shape[1]), np.cumsum((y[1:] + y[:-1]) / 2 * np.diff(tt)[:, None], axis=0)])


def position(acc, tt, closed):
    """Position (m) with velocity zero at both ends. closed: also position
    zero at the end. The removed velocity drift is c1*t (open) or
    c1*t + c2*t^2 (closed)."""
    v = cumtrapz(acc, tt)
    T = tt[-1]
    if closed:
        p_end = cumtrapz(v, tt)[-1]
        c = np.linalg.solve([[T, T ** 2], [T ** 2 / 2, T ** 3 / 3]], np.vstack([v[-1], p_end]))
        v = v - np.outer(tt, c[0]) - np.outer(tt ** 2, c[1])
    else:
        v = v - np.outer(tt / T, v[-1])
    return cumtrapz(v, tt)


def motion_bounds(score, i0, i1, fs):
    """Rest points around the motion of the take i0..i1: back from its first
    moving sample and forward from its last one, until the score is low."""
    lo = max(0, i0 - int(BEFORE_S * fs))
    hi = min(len(score) - 1, i1 + int(AFTER_S * fs))
    moving = np.flatnonzero(score[i0:i1 + 1] > MOVE)
    if len(moving) == 0:                              # no clear motion: quietest moments
        return lo + int(np.argmin(score[lo:i0 + 1])), i1 + int(np.argmin(score[i1:hi + 1]))
    a, b = i0 + moving[0], i0 + moving[-1]
    while a > lo and score[a] > REST:
        a -= 1
    while b < hi and score[b] > REST:
        b += 1
    return a, b


def plane_basis(p):
    """right, up, normal of the best-fit plane. For a drawing in a horizontal
    plane, up is world y and the normal points up. Otherwise up is world z."""
    _, s, vt = np.linalg.svd(p - p.mean(axis=0), full_matrices=False)
    n = vt[2] if vt[2][2] >= 0 else -vt[2]
    ref = np.array([0, 1.0, 0]) if abs(n[2]) > 0.7 else np.array([0, 0, 1.0])
    up = ref - n * (n @ ref)
    up /= np.linalg.norm(up)
    return np.cross(up, n), up, n, float(s[2] / s[0]) if s[0] > 0 else 0.0


def take_file_matches(path, take):
    """True when the take file exists and is the take of this run: its first
    sample has the run's index and PL timestamp (file numbers are reused)."""
    if not path.exists():
        return False
    with open(path) as f:
        f.readline()
        first = f.readline().split(",")
    return len(first) > 1 and int(first[0]) >= take["first_index"] and int(first[1]) == take["t_start_us"]


def takes_of_run(run_dir, filtered):
    """The kept takes of one run, and the number of takes left out."""
    d, meta = load_run(run_dir)
    shape_folder = run_dir.parent.parent
    imu = meta["imu"]
    cols = slice(8, 14) if filtered else slice(2, 8)
    x = d[:, cols].astype(float)
    f_g = x[:, 0:3] * imu["accel_mg_per_lsb"] * 1e-3
    w_dps = x[:, 3:6] * imu["gyro_mdps_per_lsb"] * 1e-3
    t = d[:, 1] * 1e-6
    fs = meta.get("fs_measured_hz") or meta["odr_nominal_hz"]

    # gyro bias: median over the still parts of the run
    still = smooth(np.linalg.norm(w_dps - np.median(w_dps, axis=0), axis=1) < 4.0, 0.25 * fs) > 0.999
    bias = np.median(w_dps[still], axis=0) if still.sum() > 0.5 * fs else w_dps[:int(0.5 * fs)].mean(axis=0)
    w_rad = np.deg2rad(w_dps - bias)

    # motion score: angular rate (dps) plus deviation of |f| from 1 g (0.01 g = 1)
    g_rest = np.median(np.linalg.norm(f_g[still], axis=1)) if still.any() else 1.0
    score = smooth(np.linalg.norm(w_dps - bias, axis=1)
                   + 100 * np.abs(np.linalg.norm(f_g, axis=1) - g_rest), 0.15 * fs)

    out, left_out = [], 0
    for tk in meta["takes"]:
        if not take_file_matches(shape_folder / tk["file"], tk):
            left_out += 1                             # deleted, or the number was reused
            continue
        i0 = tk["first_index"]
        i1 = i0 + tk["samples"] - 1
        a, b = motion_bounds(score, i0, i1, fs)
        acc, tt = world_acceleration(t, f_g, w_rad, a, b)
        # shown range: the take and the motion. Outside a..b the hand is at rest.
        s0, s1 = min(a, i0), max(b, i1)
        k0, k1 = i0 - s0, i1 - s0
        views, gap = {}, 0.0
        for mode in ("open", "closed"):
            p = position(acc, tt, mode == "closed")
            if mode == "open":
                gap = float(np.linalg.norm(p[-1] - p[0])) * 100
            p = np.vstack([np.repeat(p[:1], a - s0, axis=0), p, np.repeat(p[-1:], s1 - b, axis=0)])
            right, up, normal, flat = plane_basis(p[k0:k1 + 1])
            q = (p - p[k0:k1 + 1].mean(axis=0)) * 100   # cm, around the take's centre
            plane = q[k0:k1 + 1] @ np.vstack([right, up]).T
            views[mode] = {
                "pts": [round(float(v), 2) for v in q.ravel()],
                "basis": [[round(float(v), 4) for v in vec] for vec in (right, up, normal)],
                "size_cm": [round(float(v), 1) for v in np.ptp(plane, axis=0)],
                "flatness": round(flat, 3),
            }
        out.append({
            "name": tk["file"], "run": run_dir.name, "samples": tk["samples"],
            "seconds": round(float(t[i1] - t[i0]), 2),
            "motion_s": round(float(t[b] - t[a]), 2), "gap_cm": round(gap, 1),
            "k0": int(k0), "k1": int(k1), "views": views,
            "max_rate_dps": round(float(np.max(np.linalg.norm(w_dps[i0:i1 + 1] - bias, axis=1))), 1),
            "bias_dps": [round(float(v), 3) for v in bias],
        })
    return out, left_out


def write_html(path, label, takes, filtered, view):
    data = json.dumps({"label": label, "filtered": filtered, "view": view, "takes": takes}, separators=(",", ":"))
    path.write_text(HTML.replace("/*DATA*/null", data), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", help="shape folder from imu_tool.py record, e.g. ../data/imu/square")
    p.add_argument("--filtered", action="store_true", help="use the filtered columns instead of the raw ones")
    p.add_argument("--out", help="HTML file (default: <folder>/path.html)")
    p.add_argument("--view", choices=["open", "closed"], default="open", help="start view (default: open)")
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    args = p.parse_args()

    folder = Path(args.folder)
    runs = sorted(r.parent for r in folder.glob("runs/*/run.json"))
    if not runs:
        raise SystemExit(f"no runs in {folder} (expected runs/<date>_<time>/run.json)")
    takes, left_out = [], 0
    for run in runs:
        kept, n = takes_of_run(run, args.filtered)
        takes += kept
        left_out += n
    takes.sort(key=lambda tk: tk["name"])
    out = Path(args.out) if args.out else folder / "path.html"
    write_html(out, folder.name, takes, args.filtered, args.view)
    print(f"{len(takes)} takes from {len(runs)} run(s), {left_out} deleted or replaced takes left out, "
          f"start view {args.view} -> {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>IMU path</title>
<style>
  body { margin: 0; font: 14px/1.4 system-ui, sans-serif; color: #222; background: #fafafa; }
  header { padding: 10px 16px; border-bottom: 1px solid #ddd; background: #fff; }
  header h1 { font-size: 18px; margin: 0 0 4px; }
  header p { margin: 0; color: #555; font-size: 13px; }
  main { display: flex; gap: 12px; padding: 12px; }
  #list { width: 300px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; align-content: start;
          max-height: calc(100vh - 110px); overflow-y: auto; }
  .thumb { background: #fff; border: 1px solid #ddd; border-radius: 4px; cursor: pointer; padding: 2px; }
  .thumb.sel { border-color: #1f6feb; box-shadow: 0 0 0 2px #1f6feb33; }
  .thumb canvas { display: block; width: 100%; }
  .thumb div { font-size: 11px; text-align: center; color: #444; }
  #view { flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  #bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  button { font: inherit; padding: 3px 10px; border: 1px solid #bbb; border-radius: 4px; background: #fff; cursor: pointer; }
  button:hover { background: #f0f0f0; }
  #main { width: 100%; height: calc(100vh - 200px); min-height: 360px; background: #fff; border: 1px solid #ddd;
          border-radius: 4px; cursor: grab; }
  #info { font-size: 13px; color: #333; }
  .legend span { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin: 0 3px 0 10px; vertical-align: middle; }
</style>
</head>
<body>
<header>
  <h1 id="title">IMU path</h1>
  <p>Position from gyro + accelerometer, integrated over the motion of each take (velocity zero at both ends).
     "Closed shape" also makes the path end where it starts; this removes more drift, but use it only for closed shapes.
     Drag to rotate, wheel to zoom, arrow keys for the previous/next take. Colour runs from the start (blue) to the end (orange)
     of the take; grey is the part before and after it. The rotation about the vertical axis is arbitrary.</p>
</header>
<main>
  <div id="list"></div>
  <div id="view">
    <div id="bar">
      <button id="bPlane">Plane view</button>
      <button id="bTop">Top (z up)</button>
      <button id="bFront">Front</button>
      <button id="bSide">Side</button>
      <label><input type="checkbox" id="cClosed"> closed shape</label>
      <label><input type="checkbox" id="cExt" checked> before/after</label>
      <label><input type="checkbox" id="cAll"> all takes (plane view)</label>
      <span class="legend"><span style="background:#2ca02c"></span>start<span style="background:#d62728"></span>end</span>
    </div>
    <canvas id="main"></canvas>
    <div id="info"></div>
  </div>
</main>
<script>
const DATA = /*DATA*/null;
const takes = DATA.takes;
let sel = 0, zoom = 1;
let M = [[1,0,0],[0,1,0],[0,0,1]];           // view rotation: rows are screen right, screen up, towards the viewer

const $ = id => document.getElementById(id);
$("title").textContent = `IMU path: ${DATA.label} (${takes.length} takes, ${DATA.filtered ? "filtered" : "raw"} samples)`;

function pts(flat) { const a = []; for (let i = 0; i < flat.length; i += 3) a.push([flat[i], flat[i+1], flat[i+2]]); return a; }
takes.forEach(tk => { for (const v of Object.values(tk.views)) v.P = pts(v.pts); });
$("cClosed").checked = DATA.view === "closed";
const V = tk => tk.views[$("cClosed").checked ? "closed" : "open"];   // the drift correction shown

function mul(A, B) { return A.map(r => [0,1,2].map(j => r[0]*B[0][j] + r[1]*B[1][j] + r[2]*B[2][j])); }
function rot(axis, a) {
  const c = Math.cos(a), s = Math.sin(a);
  return axis === 0 ? [[1,0,0],[0,c,-s],[0,s,c]] : [[c,0,s],[0,1,0],[-s,0,c]];
}
function color(u) {                         // blue -> orange along the take
  const a = [31,119,180], b = [255,127,14];
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * u)).join(",")})`;
}

function fitCanvas(cv) {
  const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * d); cv.height = Math.round(r.height * d);
  const g = cv.getContext("2d"); g.setTransform(d, 0, 0, d, 0, 0);
  return [g, r.width, r.height];
}

// draw a path given 2D screen coordinates (x right, y up) in cm
function drawPath(g, Q, tk, sx, sy, s, ext, lw) {
  const X = q => sx + q[0] * s, Y = q => sy - q[1] * s;
  if (ext) {
    g.strokeStyle = "#bbb"; g.lineWidth = lw; g.setLineDash([4, 3]);
    for (const [i0, i1] of [[0, tk.k0], [tk.k1, Q.length - 1]]) {
      if (i1 <= i0) continue;
      g.beginPath(); g.moveTo(X(Q[i0]), Y(Q[i0]));
      for (let i = i0 + 1; i <= i1; i++) g.lineTo(X(Q[i]), Y(Q[i]));
      g.stroke();
    }
    g.setLineDash([]);
  }
  g.lineWidth = lw * 1.5;
  for (let i = tk.k0; i < tk.k1; i++) {
    g.strokeStyle = color((i - tk.k0) / (tk.k1 - tk.k0));
    g.beginPath(); g.moveTo(X(Q[i]), Y(Q[i])); g.lineTo(X(Q[i+1]), Y(Q[i+1])); g.stroke();
  }
  for (const [i, c] of [[tk.k0, "#2ca02c"], [tk.k1, "#d62728"]]) {
    g.fillStyle = c; g.beginPath(); g.arc(X(Q[i]), Y(Q[i]), lw * 2.5, 0, 7); g.fill();
  }
}

function planeCoords(tk) { const [r, u] = V(tk).basis; return V(tk).P.map(p => [p[0]*r[0]+p[1]*r[1]+p[2]*r[2], p[0]*u[0]+p[1]*u[1]+p[2]*u[2]]); }
function extent(Q, i0, i1) {
  let m = 1;
  for (let i = i0; i <= i1; i++) m = Math.max(m, Math.abs(Q[i][0]), Math.abs(Q[i][1]));
  return m;
}

function drawThumbs() {
  const list = $("list"); list.innerHTML = "";
  takes.forEach((tk, j) => {
    const d = document.createElement("div"); d.className = "thumb" + (j === sel ? " sel" : "");
    const cv = document.createElement("canvas"); cv.style.height = "110px";
    const lab = document.createElement("div"); lab.textContent = tk.name.replace(".csv", "");
    d.append(cv, lab); list.append(d);
    d.onclick = () => { sel = j; planeView(); drawThumbs(); };
    requestAnimationFrame(() => {
      const [g, w, h] = fitCanvas(cv), Q = planeCoords(tk);
      const s = 0.42 * Math.min(w, h) / extent(Q, tk.k0, tk.k1);
      drawPath(g, Q, tk, w / 2, h / 2, s, false, 1);
    });
  });
}

function drawAxes(g, w, h) {
  const o = [42, h - 42], L = 28, names = ["x", "y", "z"], cols = ["#d62728", "#2ca02c", "#1f77b4"];
  for (let a = 0; a < 3; a++) {
    const e = [0, 0, 0]; e[a] = 1;
    const x = M[0][0]*e[0] + M[0][1]*e[1] + M[0][2]*e[2], y = M[1][0]*e[0] + M[1][1]*e[1] + M[1][2]*e[2];
    g.strokeStyle = cols[a]; g.lineWidth = 2; g.beginPath(); g.moveTo(o[0], o[1]); g.lineTo(o[0] + x*L, o[1] - y*L); g.stroke();
    g.fillStyle = cols[a]; g.fillText(names[a], o[0] + x*(L+8) - 3, o[1] - y*(L+8) + 4);
  }
}

function drawScale(g, w, h, s) {
  let cm = 1; for (const c of [1, 2, 5, 10, 20, 50]) if (c * s < w * 0.25) cm = c;
  g.strokeStyle = "#444"; g.lineWidth = 2; g.beginPath(); g.moveTo(w - 20 - cm*s, h - 24); g.lineTo(w - 20, h - 24); g.stroke();
  g.fillStyle = "#444"; g.fillText(`${cm} cm`, w - 20 - cm*s, h - 30);
}

function draw() {
  const [g, w, h] = fitCanvas($("main")), tk = takes[sel];
  g.font = "12px system-ui, sans-serif";
  if ($("cAll").checked) {
    let m = 1; const all = takes.map(t => { const Q = planeCoords(t); m = Math.max(m, extent(Q, t.k0, t.k1)); return Q; });
    const s = 0.42 * Math.min(w, h) / m * zoom;
    g.globalAlpha = 0.25; takes.forEach((t, j) => { if (j !== sel) drawPath(g, all[j], t, w/2, h/2, s, false, 1); });
    g.globalAlpha = 1; drawPath(g, all[sel], tk, w/2, h/2, s, false, 2);
    drawScale(g, w, h, s);
  } else {
    const ext = $("cExt").checked;
    const P = V(tk).P;
    const Q = P.map(p => [M[0][0]*p[0]+M[0][1]*p[1]+M[0][2]*p[2], M[1][0]*p[0]+M[1][1]*p[1]+M[1][2]*p[2], M[2][0]*p[0]+M[2][1]*p[1]+M[2][2]*p[2]]);
    const R = Math.max(1, ...P.slice(tk.k0, tk.k1 + 1).map(p => Math.hypot(p[0], p[1], p[2])));
    const s = 0.45 * Math.min(w, h) / R * zoom;
    drawPath(g, Q, tk, w/2, h/2, s, ext, 1.5);
    drawAxes(g, w, h); drawScale(g, w, h, s);
  }
  const v = V(tk);
  $("info").textContent = `${tk.name} (run ${tk.run}): ${tk.samples} samples, ${tk.seconds} s, motion ${tk.motion_s} s, ` +
    `size in its plane ${v.size_cm[0]} x ${v.size_cm[1]} cm, flatness ${v.flatness} (0 = flat), ` +
    `max rate ${tk.max_rate_dps} dps, end-to-start gap without "closed shape" ${tk.gap_cm} cm`;
}

function setView(rows) { M = rows; zoom = 1; draw(); }
function planeView() { const [r, u, n] = V(takes[sel]).basis; setView([r, u, n]); }
$("bPlane").onclick = planeView;
$("bTop").onclick = () => setView([[1,0,0],[0,1,0],[0,0,1]]);
$("bFront").onclick = () => setView([[1,0,0],[0,0,1],[0,-1,0]]);
$("bSide").onclick = () => setView([[0,1,0],[0,0,1],[1,0,0]]);
$("cExt").onchange = draw; $("cAll").onchange = draw;
$("cClosed").onchange = () => { drawThumbs(); planeView(); };

const cv = $("main"); let drag = null;
cv.onmousedown = e => { drag = [e.clientX, e.clientY]; cv.style.cursor = "grabbing"; };
window.onmouseup = () => { drag = null; cv.style.cursor = "grab"; };
window.onmousemove = e => {
  if (!drag || $("cAll").checked) return;
  const dx = e.clientX - drag[0], dy = e.clientY - drag[1]; drag = [e.clientX, e.clientY];
  M = mul(rot(0, -dy * 0.01), mul(rot(1, dx * 0.01), M)); draw();
};
cv.onwheel = e => { e.preventDefault(); zoom *= Math.exp(-e.deltaY * 0.001); draw(); };
window.onkeydown = e => {
  if (e.key === "ArrowRight" || e.key === "ArrowDown") sel = Math.min(takes.length - 1, sel + 1);
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp") sel = Math.max(0, sel - 1);
  else return;
  e.preventDefault(); planeView(); drawThumbs();
};
window.onresize = draw;
drawThumbs(); planeView();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
