#!/usr/bin/env python3
"""Plot the CNN/SNN comparison from the per-image CSV written by results.py.

    python pc/plot_results.py                          # pc/results/comparison.csv
    python pc/plot_results.py path/to/comparison.csv

Saves four PNGs next to the CSV:
    confusion_matrices.png   normalised confusion matrix per model
    accuracy_per_class.png   accuracy per class, CNN and SNN side by side
    outcomes.png             both correct / only CNN / only SNN / both wrong
    timing.png               inference, input preparation and DFX switch (log scale)
"""

import argparse
import csv
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use("Agg")  # write files only, no window
import matplotlib.pyplot as plt
import numpy as np

DEFAULT_CSV = Path(__file__).resolve().parent / "results" / "comparison.csv"
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="?", type=Path, default=DEFAULT_CSV, help="Per-image CSV from results.py")
    args = parser.parse_args()

    rows, classes = load_rows(args.csv)
    out = args.csv.resolve().parent
    plot_confusion(rows, classes, out)
    plot_accuracy_per_class(rows, classes, out)
    plot_outcomes(rows, out)
    plot_timing(rows, out)
    print(f"Saved 4 plots for {len(rows)} images to {out}")


if __name__ == "__main__":
    main()
