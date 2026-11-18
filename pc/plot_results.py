#!/usr/bin/env python3
"""Plot the CNN/SNN results for the report.

    python pc/plot_results.py                          # pc/results/comparison.csv
    python pc/plot_results.py path/to/comparison.csv

Saves PNGs next to the CSV. Each plot is skipped (with a message) if its input is missing.

  From comparison.csv (results.py):
    confusion_matrices.png   normalised confusion matrix per model
    accuracy_per_class.png   accuracy per class, CNN and SNN side by side
    outcomes.png             both correct / only CNN / only SNN / both wrong
    timing.png               inference, input preparation and DFX switch (log scale)
    examples.png             test images the models disagree on or both get wrong
  From the board log (newest *.jsonl next to the CSV, or --log):
    time_breakdown.png       where the time of one image goes (UART, PS, PL, DFX, rest)
  From the training output:
    training_curves.png      accuracy and loss per epoch, CNN and SNN
    rounding_effect.png      CNN accuracy with float, truncated and rounded Q8.8 weights
  From the HLS synthesis reports:
    resources.png            BRAM, DSP, FF, LUT per IP as % of the chip
  From the test images:
    snn_pipeline.png         image -> Sobel -> ON/OFF spikes -> 12x16 features
"""

import argparse
import csv
import json
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use("Agg")  # write files only, no window
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CSV = Path(__file__).resolve().parent / "results" / "comparison.csv"
DEFAULT_TEST_DIR = REPO / "data" / "shapes_dataset" / "test"
CNN_HISTORY = REPO / "cnn" / "trained_model" / "reports" / "history.csv"
SNN_HISTORY = REPO / "snn" / "snn_mlp_model" / "reports" / "history.csv"
CNN_WEIGHTS_H5 = REPO / "cnn" / "trained_model" / "models" / "dwpw_cnn.weights.h5"
SYNTH_REPORTS = {"cnn": REPO / "cnn" / "build" / "hls" / "syn" / "report" / "ds_cnn_csynth.rpt",
                 "snn": REPO / "snn" / "build" / "hls" / "syn" / "report" / "snn_mlp_csynth.rpt"}
REQUEST_BYTES = 2 + 8 + 1 + 4800           # A6 6A + header + mask + pixels (send_images.py)
REPLY_BYTES = {"result": 37, "dfx": 15}    # framed reply sizes on the UART
SOBEL_RANK = 4319                          # 90th percentile of 4800 pixels (snn_mlp.py)
MODELS = ("cnn", "snn")
COLORS = {"cnn": "#2a6fb0", "snn": "#e07b39"}
OUTCOMES = {"both_correct": "Both correct", "cnn_only_correct": "Only CNN correct",
            "snn_only_correct": "Only SNN correct", "both_wrong": "Both wrong"}


def load_rows(path):
    """Rows where both models returned a result (same filter as results.py)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["comparison"] in OUTCOMES]
    if not rows:
        raise SystemExit(f"No complete CNN/SNN pairs in {path}.")
    # Class names in class-ID order, taken from the CSV itself
    names = {int(r["true_id"]): r["true_label"] for r in rows}
    return rows, [names[i] for i in sorted(names)]


def confusion(rows, model, n):
    m = np.zeros((n, n), dtype=int)
    for r in rows:
        m[int(r["true_id"]), int(r[f"{model}_prediction_id"])] += 1
    return m


def accuracy(rows, model):
    return sum(r[f"{model}_correct"] == "True" for r in rows) / len(rows)


def plot_confusion(rows, classes, out):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))
    for ax, model in zip(axes, MODELS):
        counts = confusion(rows, model, len(classes))
        norm = counts / counts.sum(axis=1, keepdims=True)
        ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
        for i in range(len(classes)):
            for j in range(len(classes)):
                ax.text(j, i, f"{norm[i, j]:.2f}\n({counts[i, j]})", ha="center", va="center",
                        fontsize=9, color="white" if norm[i, j] > 0.5 else "black")
        ax.set_xticks(range(len(classes)), classes, rotation=30, ha="right")
        ax.set_yticks(range(len(classes)), classes)
        ax.set_xlabel("Predicted")
        ax.set_ylabel("True")
        ax.set_title(f"{model.upper()}  (accuracy {accuracy(rows, model):.2%})")
    fig.suptitle(f"Confusion matrices on the board, {len(rows)} test images")
    fig.tight_layout()
    fig.savefig(out / "confusion_matrices.png", dpi=200)
    plt.close(fig)


def plot_accuracy_per_class(rows, classes, out):
    x = np.arange(len(classes))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for k, model in enumerate(MODELS):
        m = confusion(rows, model, len(classes))
        acc = m.diagonal() / m.sum(axis=1)
        bars = ax.bar(x + (k - 0.5) * 0.38, acc * 100, 0.38, label=model.upper(), color=COLORS[model])
        ax.bar_label(bars, fmt="%.1f", fontsize=8, padding=2)
    ax.set_xticks(x, classes)
    ax.set_ylabel("Accuracy (%)")
    ax.set_ylim(0, 105)
    ax.set_title("Accuracy per class on the board")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "accuracy_per_class.png", dpi=200)
    plt.close(fig)


def plot_outcomes(rows, out):
    counts = [sum(r["comparison"] == key for r in rows) for key in OUTCOMES]
    colors = ["#4a9b5e", COLORS["cnn"], COLORS["snn"], "#b0443c"]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    bars = ax.bar(list(OUTCOMES.values()), counts, color=colors)
    ax.bar_label(bars, labels=[f"{c} ({c / len(rows):.1%})" for c in counts], padding=2)
    ax.set_ylabel("Images")
    ax.set_ylim(0, max(counts) * 1.15)
    ax.set_title("Where the models agree and disagree")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "outcomes.png", dpi=200)
    plt.close(fig)


def plot_timing(rows, out):
    # Median per model; DFX only exists on requests that switched modules
    stages = ("Inference", "Input preparation (PS)", "DFX switch")
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = np.arange(len(stages))
    for k, model in enumerate(MODELS):
        dfx = [float(r[f"{model}_dfx_us"]) for r in rows if r[f"{model}_dfx_us"]]
        values = [median(float(r[f"{model}_infer_us"]) for r in rows),
                  median(float(r[f"{model}_prep_us"]) for r in rows),
                  median(dfx) if dfx else 0]
        bars = ax.bar(x + (k - 0.5) * 0.38, values, 0.38, label=model.upper(), color=COLORS[model])
        ax.bar_label(bars, labels=[f"{v / 1000:.2f} ms" if v >= 1000 else f"{v:.0f} µs" for v in values],
                     fontsize=8, padding=2)
    ax.set_yscale("log")
    ax.set_xticks(x, stages)
    ax.set_ylabel("Time (µs, log scale, median)")
    ax.set_title("Timing on the board")
    ax.legend()
    ax.grid(axis="y", alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out / "timing.png", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 1. Where the time of one image goes
# ---------------------------------------------------------------------------
def plot_time_breakdown(log_path, baud, out):
    """Median per image for 'both' requests: UART (computed from bytes and baud),
    PS preparation, PL inference, DFX switch (measured on the board), rest = PC/USB."""
    records = [json.loads(line) for line in log_path.open(encoding="utf-8") if line.strip()]
    records = [r for r in records if set(r["results"]) == {"cnn", "snn"}
               and all(v["status"] == 0 for v in r["results"].values()) and not r["transport_error"]]
    if not records:
        print(f"time_breakdown.png skipped: no complete 'both' requests in {log_path}")
        return

    def med(f):
        return median(f(r) for r in records) / 1000.0  # us -> ms

    n_dfx = median(len(r["dfx_us"]) for r in records)
    uart_bytes = REQUEST_BYTES + 2 * REPLY_BYTES["result"] + n_dfx * REPLY_BYTES["dfx"]
    parts = {
        "UART transfer": uart_bytes * 10 / baud * 1000.0,  # 8N1 = 10 bits per byte
        "PS input preparation": med(lambda r: r["results"]["cnn"]["prep_us"] + r["results"]["snn"]["prep_us"]),
        "CNN inference (PL)": med(lambda r: r["results"]["cnn"]["infer_us"]),
        "SNN inference (PL)": med(lambda r: r["results"]["snn"]["infer_us"]),
        "DFX switch (PCAP)": med(lambda r: sum(r["dfx_us"].values())),
    }
    total = median(r["round_trip_ms"] for r in records)
    parts["PC, USB and rest"] = max(total - sum(parts.values()), 0.0)

    colors = ["#8c96a6", "#6b8e5a", COLORS["cnn"], COLORS["snn"], "#b0443c", "#c9ced6"]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    left = 0.0
    for (name, ms), color in zip(parts.items(), colors):
        ax.barh(0, ms, left=left, color=color, label=f"{name}: {ms:.2f} ms")
        left += ms
    ax.set_yticks([])
    ax.set_xlabel("Time per image (ms, median)")
    ax.set_title(f"One image through both models: {total:.1f} ms round trip "
                 f"({len(records)} images, {baud / 1e6:g} Mbaud)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=3, frameon=False, fontsize=9)
    ax.set_xlim(0, total * 1.02)
    fig.tight_layout()
    fig.savefig(out / "time_breakdown.png", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 2. Training curves
# ---------------------------------------------------------------------------
def read_history(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def plot_training_curves(out):
    if not CNN_HISTORY.is_file() or not SNN_HISTORY.is_file():
        print(f"training_curves.png skipped: need {CNN_HISTORY} and {SNN_HISTORY}")
        return
    cnn, snn = read_history(CNN_HISTORY), read_history(SNN_HISTORY)
    col = lambda rows, key: [float(r[key]) for r in rows]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.5))
    # Keras history has no epoch column: row i is epoch i+1
    e_cnn = range(1, len(cnn) + 1)
    e_snn = [int(r["epoch"]) for r in snn]
    a1.plot(e_cnn, [v * 100 for v in col(cnn, "accuracy")], color=COLORS["cnn"], ls="--", label="CNN train")
    a1.plot(e_cnn, [v * 100 for v in col(cnn, "val_accuracy")], color=COLORS["cnn"], label="CNN validation")
    a1.plot(e_snn, [v * 100 for v in col(snn, "train_acc")], color=COLORS["snn"], ls="--", label="SNN train")
    a1.plot(e_snn, [v * 100 for v in col(snn, "val_acc")], color=COLORS["snn"], label="SNN validation")
    best = int(np.argmax(col(snn, "val_acc")))
    a1.scatter([e_snn[best]], [col(snn, "val_acc")[best] * 100], color=COLORS["snn"], zorder=3)
    a1.annotate(f"SNN best epoch {e_snn[best]}", (e_snn[best], col(snn, "val_acc")[best] * 100),
                textcoords="offset points", xytext=(-10, -18), ha="right", fontsize=8)
    a1.set_xlabel("Epoch"); a1.set_ylabel("Accuracy (%)"); a1.set_title("Accuracy")
    a1.grid(alpha=0.3); a1.legend(fontsize=8)
    a2.plot(e_cnn, col(cnn, "loss"), color=COLORS["cnn"], ls="--", label="CNN train")
    a2.plot(e_cnn, col(cnn, "val_loss"), color=COLORS["cnn"], label="CNN validation")
    a2.plot(e_snn, col(snn, "train_ce_loss"), color=COLORS["snn"], ls="--", label="SNN train")
    a2.set_xlabel("Epoch"); a2.set_ylabel("Cross-entropy loss"); a2.set_title("Loss (SNN logs train loss only)")
    a2.grid(alpha=0.3); a2.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "training_curves.png", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 3. SNN feature pipeline (same integer steps as snn_mlp.py and the HLS IP;
#    tools/check_snn_features.py verifies those two against each other)
# ---------------------------------------------------------------------------
def q88(img_u8):
    """Q8.8 values as prepare_input() in ps/ps_main.c writes them."""
    return (1024 * img_u8.astype(np.int32) + 127) // 255 - 512


def sobel_xy(q):
    """3x3 Sobel x + y with reflect-101 borders (what cv2.Sobel does)."""
    p = np.pad(q, 1, mode="reflect")
    h, w = q.shape
    win = lambda dy, dx: p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
    gx = (win(-1, 1) - win(-1, -1)) + 2 * (win(0, 1) - win(0, -1)) + (win(1, 1) - win(1, -1))
    gy = (win(1, -1) + 2 * win(1, 0) + win(1, 1)) - (win(-1, -1) + 2 * win(-1, 0) + win(-1, 1))
    return gx + gy


def snn_spikes(img_u8):
    s = sobel_xy(q88(img_u8))
    pos, neg = np.maximum(s, 0), np.maximum(-s, 0)
    on = pos > np.sort(pos, axis=None)[SOBEL_RANK]
    off = neg > np.sort(neg, axis=None)[SOBEL_RANK]
    return s, on, off


def plot_snn_pipeline(test_dir, classes, out):
    firsts = [sorted((test_dir / c).glob("*.png")) for c in classes]
    if not all(firsts):
        print(f"snn_pipeline.png skipped: no test images in {test_dir}")
        return
    titles = ["Input (80x60)", "Sobel x + y", "ON spikes", "OFF spikes", "Features: ON count per 5x5 cell"]
    fig, axes = plt.subplots(len(classes), 5, figsize=(14, 2.6 * len(classes)))
    for row, (name, files) in enumerate(zip(classes, firsts)):
        img = np.array(Image.open(files[0]).convert("L"))
        s, on, off = snn_spikes(img)
        cells = on.reshape(12, 5, 16, 5).sum(axis=(1, 3))
        lim = np.abs(s).max() or 1
        panels = [(img, "gray", 0, 255), (s, "RdBu_r", -lim, lim), (on, "gray_r", 0, 1),
                  (off, "gray_r", 0, 1), (cells, "Blues", 0, 25)]
        for col, (data, cmap, lo, hi) in enumerate(panels):
            ax = axes[row, col]
            ax.imshow(data, cmap=cmap, vmin=lo, vmax=hi, interpolation="nearest")
            ax.set_xticks([]); ax.set_yticks([])
            if row == 0:
                ax.set_title(titles[col], fontsize=9)
        axes[row, 0].set_ylabel(name, fontsize=10)
    fig.suptitle("What the SNN IP computes before the network (one test image per class)")
    fig.tight_layout()
    fig.savefig(out / "snn_pipeline.png", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 4. Effect of truncating vs rounding the CNN weights to Q8.8
# ---------------------------------------------------------------------------
def cnn_folded_weights(h5_path):
    """BN-folded float weights from the Keras weights file (same folding as train_cnn.py)."""
    import h5py  # only needed for this plot
    eps = 1e-3   # BN_EPS in train_cnn.py build_model()
    with h5py.File(h5_path, "r") as f:
        v = lambda name, i: np.array(f[f"layers/{name}/vars/{i}"], dtype=np.float64)
        def bn(name):
            gamma, beta, mean, var = (v(name, i) for i in range(4))
            scale = gamma / np.sqrt(var + eps)
            return scale, beta - mean * scale
        s1, b1 = bn("batch_normalization")
        sd, bd = bn("batch_normalization_1")
        sp, bp = bn("batch_normalization_2")
        return {
            "c1w": (v("conv2d", 0) * s1).reshape(9, 16), "c1b": b1,
            "dww": (v("depthwise_conv2d", 0)[:, :, :, 0] * sd).reshape(9, 16).T, "dwb": bd,
            "pww": (v("conv2d_1", 0) * sp).reshape(16, 8), "pwb": bp,
            "clw": v("conv2d_2", 0).reshape(8, 4), "clb": v("conv2d_2", 1),
        }


def cnn_forward(x, w):
    """Float forward pass of the folded network; x is (60, 80) normalised input."""
    def conv3(a, taps):  # a (H,W,C), taps (C,9), 'same' zero padding like Keras
        h, wd, _ = a.shape
        p = np.pad(a, ((1, 1), (1, 1), (0, 0)))
        out = np.zeros_like(a)
        for k in range(9):
            out += p[k // 3:k // 3 + h, k % 3:k % 3 + wd, :] * taps[:, k]
        return out
    a = np.maximum(conv3(np.repeat(x[..., None], 16, axis=2), w["c1w"].T) + w["c1b"], 0)
    a = a.reshape(30, 2, 40, 2, 16).max(axis=(1, 3))
    a = np.maximum(conv3(a, w["dww"]) + w["dwb"], 0)
    a = np.maximum(a @ w["pww"] + w["pwb"], 0)
    a = a.reshape(5, 6, 5, 8, 8).mean(axis=(1, 3))
    return a.mean(axis=(0, 1)) @ w["clw"] + w["clb"]


def plot_rounding_effect(test_dir, classes, out):
    if not CNN_WEIGHTS_H5.is_file():
        print(f"rounding_effect.png skipped: {CNN_WEIGHTS_H5} not found (train the CNN first)")
        return
    try:
        w = cnn_folded_weights(CNN_WEIGHTS_H5)
    except ImportError:
        print("rounding_effect.png skipped: pip install h5py")
        return
    variants = {"Float (Python)": w,
                "Q8.8 truncated (old export)": {k: np.floor(a * 256) / 256 for k, a in w.items()},
                "Q8.8 rounded (new export)": {k: np.round(a * 256) / 256 for k, a in w.items()}}
    images, labels = [], []
    for cid, name in enumerate(classes):
        for p in sorted((test_dir / name).glob("*.png")):
            images.append((np.array(Image.open(p).convert("L")) / 255.0 - 0.5) / 0.25)
            labels.append(cid)
    labels = np.array(labels)
    accs = {}
    for name, wv in variants.items():
        pred = np.array([cnn_forward(x, wv).argmax() for x in images])
        accs[name] = [np.mean(pred[labels == c] == c) * 100 for c in range(len(classes))] + \
                     [np.mean(pred == labels) * 100]
    groups = classes + ["All"]
    x = np.arange(len(groups))
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for k, (name, vals) in enumerate(accs.items()):
        bars = ax.bar(x + (k - 1) * 0.27, vals, 0.27, label=name,
                      color=["#8c96a6", "#b0443c", "#4a9b5e"][k])
        ax.bar_label(bars, fmt="%.0f", fontsize=7, padding=2)
    ax.set_xticks(x, groups)
    ax.set_ylabel("Accuracy (%)"); ax.set_ylim(0, 108)
    ax.set_title(f"CNN with the same trained weights, {len(images)} test images\n"
                 "(numpy model; only the weights are quantised)", fontsize=10)
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3, frameon=False)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "rounding_effect.png", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 5. Resources from the HLS synthesis reports
# ---------------------------------------------------------------------------
def synth_utilisation(path):
    """Total and Available rows of the top-level 'Utilization Estimates' table."""
    lines = path.read_text(errors="replace").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("== Utilization Estimates"))
    header = next(l for l in lines[start:] if "BRAM_18K" in l)
    names = [c.strip() for c in header.strip("|").split("|")][1:]
    def row(label):
        line = next(l for l in lines[start:] if l.startswith(f"|{label}"))
        return dict(zip(names, (float(c) for c in line.strip("|").split("|")[1:])))
    return row("Total"), row("Available")


def plot_resources(out):
    missing = [str(p) for p in SYNTH_REPORTS.values() if not p.is_file()]
    if missing:
        print(f"resources.png skipped: run C Synthesis first ({', '.join(missing)} missing)")
        return
    data = {m: synth_utilisation(p) for m, p in SYNTH_REPORTS.items()}
    kinds = ["BRAM_18K", "DSP", "FF", "LUT"]
    x = np.arange(len(kinds))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for k, model in enumerate(MODELS):
        total, avail = data[model]
        pct = [total[t] / avail[t] * 100 for t in kinds]
        bars = ax.bar(x + (k - 0.5) * 0.38, pct, 0.38, label=model.upper(), color=COLORS[model])
        ax.bar_label(bars, labels=[f"{int(total[t])}" for t in kinds], fontsize=8, padding=2)
    ax.set_xticks(x, kinds)
    ax.set_ylabel("% of the xc7z020")
    ax.set_title("HLS resource estimate per IP (labels: absolute count)")
    ax.legend(); ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "resources.png", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 6. Example images
# ---------------------------------------------------------------------------
def plot_examples(rows, classes, test_dir, out, per_group=6):
    groups = [("both_wrong", "Both wrong"), ("cnn_only_correct", "Only CNN correct"),
              ("snn_only_correct", "Only SNN correct")]
    def spread(key):
        """Up to per_group rows of this outcome, taking the classes in turn."""
        by_class = [[r for r in rows if r["comparison"] == key and r["true_label"] == c] for c in classes]
        out_rows = []
        for i in range(per_group):
            for lst in by_class:
                if i < len(lst) and len(out_rows) < per_group:
                    out_rows.append(lst[i])
        return out_rows
    picked = [spread(key) for key, _ in groups]
    if not (test_dir / rows[0]["image_path"]).is_file():
        print(f"examples.png skipped: test images not found under {test_dir}")
        return
    fig, axes = plt.subplots(len(groups), per_group, figsize=(2.1 * per_group, 2.3 * len(groups)))
    for g, ((_, title), items) in enumerate(zip(groups, picked)):
        for k in range(per_group):
            ax = axes[g, k]
            ax.set_xticks([]); ax.set_yticks([])
            if k == 0:
                ax.set_ylabel(title, fontsize=10)
            if k >= len(items):
                ax.axis("off")
                continue
            r = items[k]
            ax.imshow(Image.open(test_dir / r["image_path"]).convert("L"), cmap="gray", vmin=0, vmax=255)
            ax.set_title(f"true {r['true_label']}\nCNN {r['cnn_prediction']} | SNN {r['snn_prediction']}",
                         fontsize=7)
    fig.suptitle("Test images where the models disagree or both fail")
    fig.tight_layout()
    fig.savefig(out / "examples.png", dpi=200)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="?", type=Path, default=DEFAULT_CSV, help="Per-image CSV from results.py")
    parser.add_argument("--log", type=Path, help="Board log (.jsonl); default: newest *.jsonl next to the CSV")
    parser.add_argument("--test-dir", type=Path, default=DEFAULT_TEST_DIR, help="Test images (one folder per class)")
    parser.add_argument("--baud", type=int, default=4_000_000, help="UART baud rate used for the run")
    args = parser.parse_args()

    rows, classes = load_rows(args.csv)
    out = args.csv.resolve().parent
    plot_confusion(rows, classes, out)
    plot_accuracy_per_class(rows, classes, out)
    plot_outcomes(rows, out)
    plot_timing(rows, out)
    plot_examples(rows, classes, args.test_dir, out)

    log = args.log or max(out.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, default=None)
    if log is None:
        print("time_breakdown.png skipped: no *.jsonl next to the CSV (use --log)")
    else:
        print(f"Board log for time_breakdown.png: {log}")
        plot_time_breakdown(log, args.baud, out)

    plot_training_curves(out)
    plot_snn_pipeline(args.test_dir, classes, out)
    plot_rounding_effect(args.test_dir, classes, out)
    plot_resources(out)
    print(f"Plots saved to {out}")


if __name__ == "__main__":
    main()
