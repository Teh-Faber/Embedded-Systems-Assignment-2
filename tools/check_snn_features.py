#!/usr/bin/env python3
"""Check that the SNN features from HLS and Python are bit-exact, on real test images.

Python side: the feature functions imported from snn/snn_mlp.py (the training code).
HLS side:    extract_features() from snn/snn_mlp.cpp, compiled with g++ and the
             ap_int headers from the Vitis install, fed with the same Q8.8 buffer
             the PS writes (prepare_input() in ps/ps_main.c).

Run with the SNN venv (it needs the imports of snn_mlp.py):

    snn/.venv/bin/python tools/check_snn_features.py
    snn/.venv/bin/python tools/check_snn_features.py --limit-per-class 50
    snn/.venv/bin/python tools/check_snn_features.py --hls-include /tools/2026.1/Vitis/include

Needs g++ (Ubuntu: sudo apt install build-essential).
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[1]
SNN_DIR = REPO / "snn"
DEFAULT_TEST_DIR = REPO / "data" / "shapes_dataset" / "test"

# Small C++ driver: includes the real HLS source and calls extract_features() per image
DRIVER = r"""
#include <cstdio>
#include <cstdint>
#include "snn_mlp.cpp"
int main(int argc, char **argv) {
    FILE *in = fopen(argv[1], "rb"), *out = fopen(argv[2], "wb");
    if (!in || !out) return 1;
    int16_t raw[IMG_H * IMG_W];
    static pix_t img[IMG_H * IMG_W];
    static data_t feats[T_STEPS][N_FEATURES];
    while (fread(raw, sizeof(int16_t), IMG_H * IMG_W, in) == IMG_H * IMG_W) {
        for (int i = 0; i < IMG_H * IMG_W; i++) img[i] = raw[i];
        extract_features(img, feats);
        for (int i = 0; i < N_FEATURES; i++) {
            int16_t v = (int16_t)feats[0][i];
            fwrite(&v, sizeof(int16_t), 1, out);
        }
    }
    fclose(in); fclose(out);
    return 0;
}
"""


def find_hls_include(user_path):
    """Folder that contains ap_int.h: --hls-include, $XILINX_HLS, $XILINX_VITIS or a /tools search."""
    candidates = [user_path] if user_path else []
    for var in ("XILINX_HLS", "XILINX_VITIS"):
        if os.environ.get(var):
            candidates.append(os.path.join(os.environ[var], "include"))
    candidates += sorted(glob.glob("/tools/*/Vitis*/include")) + sorted(glob.glob("/opt/*/*/Vitis*/include"))
    for c in candidates:
        if c and Path(c, "ap_int.h").is_file():
            return Path(c)
    sys.exit("ap_int.h not found. Source the Vitis settings64.sh or pass --hls-include <Vitis>/include.")


def feat_lut_from_header():
    """feat_lut[] from snn/fpga_weights.h: feature value for each spike count in one cell."""
    text = (SNN_DIR / "fpga_weights.h").read_text()
    m = re.search(r"feat_lut\[\d+\]\s*=\s*\{([^}]*)\}", text)
    if not m:
        sys.exit("feat_lut not found in snn/fpga_weights.h (re-export the weights from snn_mlp.py).")
    return np.array([int(v) for v in m.group(1).split(",")], dtype=np.int16)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test-dir", type=Path, default=DEFAULT_TEST_DIR, help="Folder with one subfolder per class")
    parser.add_argument("--limit-per-class", type=int, help="Only the first N images per class (default: all)")
    parser.add_argument("--hls-include", help="Folder with ap_int.h (default: searched)")
    args = parser.parse_args()

    # The real training code, not a copy
    sys.path.insert(0, str(SNN_DIR))
    import snn_mlp

    lut = feat_lut_from_header()
    cells_px = (snn_mlp.IMG_H // snn_mlp.GRID_H) * (snn_mlp.IMG_W // snn_mlp.GRID_W)

    paths = []
    for name in snn_mlp.CLASS_FOLDERS:
        files = sorted((args.test_dir / name).glob("*.png"))
        if not files:
            sys.exit(f"No images in {args.test_dir / name}.")
        paths += files[:args.limit_per_class]

    q_inputs, py_feats = [], []
    for p in paths:
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        # Q8.8 buffer exactly as prepare_input() in ps/ps_main.c builds it
        q_inputs.append((1024 * img.astype(np.int32) + 127) // 255 - 512)
        # Python features as used for training: spike density per cell (0..1)
        dens = snn_mlp.spikes_to_snn_mlp_features(snn_mlp.image_to_spikes(img))[0]
        counts = np.rint(dens * cells_px).astype(int)   # density -> spike count (exact)
        py_feats.append(lut[counts])                     # count -> value, as the HLS LUT does

    hls_inc = find_hls_include(args.hls_include)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        np.array(q_inputs, dtype="<i2").tofile(tmp / "inputs.bin")
        (tmp / "driver.cpp").write_text(DRIVER)
        build = ["g++", "-O2", "-std=c++17", "-w", f"-I{hls_inc}", f"-I{SNN_DIR}",
                 str(tmp / "driver.cpp"), "-o", str(tmp / "driver")]
        subprocess.run(build, check=True)
        subprocess.run([str(tmp / "driver"), str(tmp / "inputs.bin"), str(tmp / "hls.bin")], check=True)
        hls = np.fromfile(tmp / "hls.bin", dtype="<i2").reshape(len(paths), -1)

    py = np.array(py_feats, dtype=np.int16)
    diff = py != hls
    bad = np.flatnonzero(diff.any(axis=1))

    print(f"HLS source:  {SNN_DIR / 'snn_mlp.cpp'}")
    print(f"ap headers:  {hls_inc}")
    print(f"Images:      {len(paths)}  ({args.test_dir})")
    print(f"Features:    {py.size}  ({py.shape[1]} per image)")
    print(f"Mismatching images:   {len(bad)}")
    print(f"Mismatching features: {int(diff.sum())}")
    for i in bad[:10]:
        cols = np.flatnonzero(diff[i])
        print(f"  {paths[i].relative_to(args.test_dir)}: {len(cols)} features differ, "
              f"first at index {cols[0]} (python {py[i, cols[0]]}, hls {hls[i, cols[0]]})")
    print("RESULT: BIT-EXACT" if len(bad) == 0 else "RESULT: NOT BIT-EXACT")
    return 0 if len(bad) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
