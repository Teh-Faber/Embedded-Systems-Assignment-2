"""
Export the shapes test images as text for the two HLS testbenches.

Reads : <repo>/data/shapes_dataset/test/<class>/*.png
Writes: <repo>/data/shapes_test_txt/<class>/<name>.txt

Each .txt is one image: 60 lines of 80 grayscale values (0..255).
Both cnn/ds_cnn_tb.cpp and snn/snn_mlp_tb.cpp read this format,
so both testbenches see exactly the same images.
"""

from pathlib import Path

import cv2

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "data" / "shapes_dataset" / "test"
DST = REPO_ROOT / "data" / "shapes_test_txt"

IMG_H, IMG_W = 60, 80
MAX_PER_CLASS = 50          # images per class (0 = all)


def main():
    if not SRC.is_dir():
        raise SystemExit(f"Missing {SRC}. Run tools/generate_shapes.py first.")

    for class_dir in sorted(p for p in SRC.iterdir() if p.is_dir()):
        files = sorted(class_dir.glob("*.png"))
        if MAX_PER_CLASS:
            files = files[:MAX_PER_CLASS]

        out_dir = DST / class_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            if img is None or img.shape != (IMG_H, IMG_W):
                raise SystemExit(f"Unexpected image (need {IMG_W}x{IMG_H} grayscale): {f}")
            rows = (" ".join(str(int(v)) for v in row) for row in img)
            (out_dir / f"{f.stem}.txt").write_text("\n".join(rows) + "\n")

        print(f"{class_dir.name}: {len(files)} images -> {out_dir}")


if __name__ == "__main__":
    main()
