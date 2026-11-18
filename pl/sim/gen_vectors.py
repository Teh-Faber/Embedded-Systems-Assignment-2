"""
Test vectors for the dfx_static RTL testbench (pl/sim/tb_dfx_static.sv).

Writes into pl/sim/vectors/:
  images.mem    one 16-bit hex word per line, 4800 words (60x80, Q8.8) per image
  expected.mem  one line per image: <gt> <pred> <logit0> <logit1> <logit2> <logit3>
                (hex, logits are the raw ap_fixed<16,8> bits)
  images.txt    which image each vector is, for reading the simulation log

Pixels are converted exactly like ps/ps_main.c:  q = (1024*p + 127)/255 - 512.
pred and logits come from the HLS C source (cnn/ds_cnn.cpp) compiled as a
golden model, so they are bit-exact with the RTL.

  python pl/sim/gen_vectors.py --per-class 2 --golden <path to golden binary>

Build the golden binary once (needs g++ and the ap_fixed headers, e.g. from
<Vitis>/include or github.com/Xilinx/HLS_arbitrary_Precision_Types):

  g++ -O2 -std=c++14 -I <ap_fixed include dir> -I cnn \
      cnn/ds_cnn.cpp pl/sim/golden_main.cpp -o golden

Images come from data/shapes_dataset/test/<class>/*.png if that folder exists,
otherwise they are drawn with tools/generate_shapes.py.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
CLASSES = ["Circle", "Figure Eight", "Square", "Triangle"]
H, W = 60, 80


def to_q88(img_u8):
    p = img_u8.astype(np.int64).ravel()
    return ((1024 * p + 127) // 255 - 512).astype(np.int16)


def load_images(per_class, seed):
    test_dir = REPO / "data" / "shapes_dataset" / "test"
    out = []
    if test_dir.is_dir():
        import cv2
        for gt, name in enumerate(CLASSES):
            files = sorted((test_dir / name).glob("*.png"))[:per_class]
            for f in files:
                img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
                if img is None or img.shape != (H, W):
                    sys.exit(f"{f}: not a {W}x{H} grayscale image")
                out.append((gt, str(f.relative_to(REPO)), img))
        return out

    sys.path.insert(0, str(REPO / "tools"))
    import generate_shapes as gs
    rng = np.random.default_rng(seed)
    for gt, name in enumerate(CLASSES):
        for i in range(per_class):
            out.append((gt, f"generated {name} #{i} (seed {seed})", gs.draw_sample(name, rng)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-class", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--golden", required=True, help="golden model binary (see above)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "vectors")
    args = ap.parse_args()

    images = load_images(args.per_class, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)

    with open(args.out / "images.mem", "w", newline="\n") as f:
        for _, _, img in images:
            for v in to_q88(img):
                f.write(f"{int(v) & 0xFFFF:04x}\n")

    res = subprocess.run([args.golden, str(args.out / "images.mem"), str(len(images))],
                         check=True, capture_output=True, text=True).stdout.split("\n")

    with open(args.out / "expected.mem", "w", newline="\n") as f, \
         open(args.out / "images.txt", "w", newline="\n") as g:
        for k, (gt, src, _) in enumerate(images):
            pred, *logits = res[k].split()
            f.write(f"{gt:02x} {pred} {' '.join(logits)}\n")
            lf = [int(x, 16) - (1 << 16) * (int(x, 16) >> 15) for x in logits]
            g.write(f"{k}: gt={gt} ({CLASSES[gt]}) golden_pred={int(pred, 16)} "
                    f"logits={[v / 256 for v in lf]}  {src}\n")

    print(f"{len(images)} vectors written to {args.out}")
    print((args.out / "images.txt").read_text())


if __name__ == "__main__":
    main()
