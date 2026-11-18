#!/usr/bin/env python3
"""Send one drawing to the board and show what the CNN and SNN predict.

    python pc/classify_drawing.py my_shape.png            # once
    python pc/classify_drawing.py my_shape.png --watch    # again every time the file is saved
    python pc/classify_drawing.py drawings/ --watch       # every new or re-saved PNG in the folder
    python pc/classify_drawing.py drawings/               # once: the newest PNG in the folder

Draw black lines on a white background (Paint's default), canvas 80 wide x 60 high,
brush 1-3 px, save as PNG. Other sizes are scaled to 80x60 (a warning is printed).

--watch checks modification times a few times per second (no screen capture).
With a file, every save of that file is sent; it may not exist yet when you start.
With a folder, every PNG that appears or changes after the start is sent, so
"hand(1).png", "hand(2).png", ... all work. PNGs already there at the start are skipped.
Stop with Ctrl+C.

The UART frames, replies and DFX timing come from send_images.py (same folder),
so the protocol is the same as for the test runs.
"""

import argparse
import os
import time
from pathlib import Path

from PIL import Image
import serial

import send_images as si  # same folder: frame format, reply parsing, defaults

POLL_S = 0.25        # how often --watch checks the file
SETTLE_S = 0.3       # the file must stay unchanged this long before it is read (Paint may write in steps)


def load_drawing(path):
    """Return the 4,800 raw grayscale bytes, scaled to 80x60 if needed."""
    with Image.open(path) as image:
        gray = image.convert("L")
        if gray.size != si.IMAGE_SIZE:
            print(f"Note: {gray.size[0]}x{gray.size[1]} scaled to 80x60 (width x height).")
            gray = gray.resize(si.IMAGE_SIZE, Image.Resampling.BOX)
        pixels = gray.tobytes()
    # The training images are white with dark lines; warn if the drawing looks inverted
    if sum(pixels) / len(pixels) < 128:
        print("Warning: the image is mostly dark. The models expect dark lines on a white background.")
    return pixels


def classify(port, pixels, image_id, models, timeout):
    """Send one request and print both results (uses send_images.collect_results)."""
    record = {"image_id": image_id, "results": {}, "dfx_us": {}, "transport_error": None}
    mask = si.MODEL_MASKS[models]
    si.collect_results(port, record, pixels, mask, timeout, protocol_test=False)
    if record["transport_error"]:
        print(f"Error: {record['transport_error']}")
        return
    for model in ("cnn", "snn"):
        result = record["results"].get(model)
        if result is None:
            continue
        if result["status"] != 0:
            print(f"  {model.upper()}: {si.status_text(result['status'])}")
            continue
        dfx = record["dfx_us"].get(model)
        dfx_text = f", DFX switch {dfx / 1000:.2f} ms" if dfx is not None else ""
        print(f"  {model.upper()}: {si.CLASS_NAMES[result['class_id']]:<13} "
              f"scores {tuple(result['scores'])}  {result['infer_us']} us{dfx_text}")
    print(f"  round trip {record['round_trip_ms']:.1f} ms")


def file_stamp(path):
    """(modification time, size), or None while the file does not exist."""
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


def watched_files(target):
    """The file itself, or every PNG in the folder."""
    if target.is_dir():
        return [p for p in target.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
    return [target]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path, help="PNG to classify, or a folder of PNGs")
    parser.add_argument("--watch", action="store_true", help="Send again every time the file is saved")
    parser.add_argument("--port", default=si.DEFAULT_PORT, help="For example COM3 or /dev/ttyUSB1")
    parser.add_argument("--models", choices=si.MODEL_MASKS, default="both")
    parser.add_argument("--baud", type=int, default=4_000_000)
    parser.add_argument("--timeout", type=float, default=5.0, help="Seconds per request")
    args = parser.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.05, write_timeout=args.timeout) as port:
        port.reset_input_buffer()
        image_id = 1_000_000  # well away from the test-set IDs

        if not args.watch:
            image = args.image
            if image.is_dir():
                pngs = watched_files(image)
                if not pngs:
                    print(f"No PNG files in {image}.")
                    return 1
                image = max(pngs, key=lambda p: p.stat().st_mtime)  # newest PNG
            print(f"{image}:")
            classify(port, load_drawing(image), image_id, args.models, args.timeout)
            return 0

        folder_mode = args.image.is_dir()
        print(f"Watching {args.image.resolve()} (save in Paint to classify, Ctrl+C to stop)")
        # Folder: skip the PNGs that are already there. File: send it once at the start if it exists.
        last_sent = {p: file_stamp(p) for p in watched_files(args.image)} if folder_mode else {}
        try:
            while True:
                for path in sorted(watched_files(args.image), key=lambda p: file_stamp(p) or (0, 0)):
                    stamp = file_stamp(path)
                    if stamp is None or stamp == last_sent.get(path):
                        continue
                    time.sleep(SETTLE_S)
                    if file_stamp(path) != stamp:
                        continue  # still being written; check again on the next round
                    try:
                        pixels = load_drawing(path)
                    except OSError as error:
                        print(f"Could not read {path.name} yet ({error}); retrying.")
                        continue
                    last_sent[path] = stamp
                    image_id += 1
                    print(f"\n[{time.strftime('%H:%M:%S')}] {path.name}:")
                    classify(port, pixels, image_id, args.models, args.timeout)
                time.sleep(POLL_S)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
