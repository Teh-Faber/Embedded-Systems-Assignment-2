#!/usr/bin/env python3
"""Exercise the lab-framed UART protocol with ps_uart_test.c on the board."""

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
import math
from pathlib import Path
import struct
import sys
import time

from PIL import Image
import serial


IMAGE_SIZE = (80, 60)
PIXEL_COUNT = 4800
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}
HEADER = struct.Struct("<HBBI")
MODEL_BODY = struct.Struct("<BBBII4i")
IMAGE_START, REPLY_START, REPLY_END = b"\xa6\x6a", b"\xa5", b"\x5a"
VERSION = 1
IMAGE, RESULT, DIAGNOSTIC = 1, 2, 3
TEST_RESULT = 128
MODELS = {1: "cnn", 2: "snn"}
REPLY_SIZES = {RESULT: {MODEL_BODY.size}, DIAGNOSTIC: {1, 1 + PIXEL_COUNT}}
DIAGNOSTIC_STATUS = {
    1: "invalid request length", 2: "invalid model mask", 5: "unsupported version",
    6: "unsupported request type", 7: "truncated request", 8: "echo unavailable",
}
CSV_FIELDS = (
    "round", "image", "image_id", "test", "model", "status", "round_trip_ms",
    "mismatch_bytes", "first_mismatch_index", "detail", "request_note",
)


class FrameError(ValueError):
    """Keep a model ID when a malformed reply can still be attributed."""

    def __init__(self, message, image_id=None, model_id=None):
        super().__init__(message)
        self.image_id = image_id
        self.model_id = model_id


@dataclass
class Observation:
    status: str
    round_trip_ms: float
    detail: str = ""
    mismatch_bytes: int | None = None
    first_mismatch_index: int | None = None


def image_paths(root, limit):
    """Walk the class folders in a dataset split in a repeatable order."""
    if not root.is_dir():
        raise ValueError(f"Dataset folder does not exist: {root}")
    folders = sorted(path for path in root.iterdir() if path.is_dir())
    images = []
    for folder in [root, *folders]:
        files = sorted(path for path in folder.iterdir()
                       if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
        images.extend(files[:limit])
    if not images:
        raise ValueError(f"No images found in {root}. Use the train, val, or test folder.")
    return images


def load_pixels(path):
    """Use the same 80 x 60 raw grayscale representation as the main sender."""
    with Image.open(path) as image:
        if image.size != IMAGE_SIZE:
            raise ValueError(f"{path}: expected 80 x 60 pixels, got {image.size}.")
        return image.convert("L").tobytes()


def send_image(port, image_id, mask, pixels):
    # The length counts the model mask and the pixels. The other header fields
    # and the two start-marker bytes are outside that length.
    body = bytes([mask]) + pixels
    frame = IMAGE_START + HEADER.pack(len(body), VERSION, IMAGE, image_id) + body
    if port.write(frame) != len(frame):
        raise TimeoutError("The serial port did not accept the whole request.")


def read_exact(port, size, deadline):
    """Handle short serial reads without resetting the request deadline."""
    data = bytearray()
    while len(data) < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Reply deadline expired.")
        data.extend(port.read(size - len(data)))
    return bytes(data)


def read_reply(port, wanted_id, deadline):
    """Validate header, body length and trailer, skipping old valid replies."""
    while True:
        while read_exact(port, 1, deadline) != REPLY_START:
            pass
        length, version, kind, image_id = HEADER.unpack(read_exact(port, HEADER.size, deadline))
        if version != VERSION or length not in REPLY_SIZES.get(kind, ()):
            raise FrameError(f"Invalid reply header: type={kind}, length={length}, version={version}.",
                             image_id)
        body = read_exact(port, length, deadline)
        if read_exact(port, 1, deadline) != REPLY_END:
            model_id = body[0] if kind == RESULT else None
            raise FrameError("Invalid reply end marker; expected 5A.", image_id, model_id)
        if image_id > wanted_id:
            raise FrameError(f"Received future image ID {image_id}; expected {wanted_id}.", image_id)
        if image_id == wanted_id:
            return kind, body


def echo_observation(body, pixels, elapsed_ms):
    """Compare all pixel bytes and record the first differing position."""
    actual = body[1:]
    if actual == pixels:
        return Observation("ok", elapsed_ms, mismatch_bytes=0)
    first = next(i for i, (sent, echoed) in enumerate(zip(pixels, actual)) if sent != echoed)
    count = sum(sent != echoed for sent, echoed in zip(pixels, actual))
    return Observation("byte_mismatch", elapsed_ms,
                       f"Pixel {first}: sent {pixels[first]}, echoed {actual[first]}.", count, first)


def result_observation(body, pixels, elapsed_ms):
    """Check the deterministic data emitted by ps_uart_test.c for one model."""
    model_id = body[0]
    expected = (
        model_id, TEST_RESULT, (pixels[0] + model_id) % 4,
        0x01020300 + model_id, 0x04050600 + model_id,
        -100000 - model_id, 200000 + model_id, pixels[PIXEL_COUNT // 2], pixels[-1],
    )
    names = ("model_id", "status", "class_id", "infer_us", "prep_us",
             "score_0", "score_1", "score_2", "score_3")
    actual = MODEL_BODY.unpack(body)
    differences = [f"{name}: got {a}, expected {e}"
                   for name, a, e in zip(names, actual, expected) if a != e]
    if differences:
        return Observation("value_mismatch", elapsed_ms, "; ".join(differences))
    return Observation("ok", elapsed_ms)


def run_request(port, image_id, pixels, test, timeout):
    """Account for every expected reply, retaining valid replies after a fault."""
    echo = test == "echo"
    pending = {0} if echo else set(MODELS)
    observations = {}
    note = ""
    resync = False
    write_timeout = False
    start = time.monotonic()

    def elapsed_ms():
        return round((time.monotonic() - start) * 1000, 3)

    try:
        try:
            send_image(port, image_id, 0 if echo else 3, pixels)
        except serial.SerialTimeoutException as error:
            write_timeout = True
            raise TimeoutError("Serial write timed out.") from error
        except TimeoutError:
            write_timeout = True
            raise

        while pending:
            try:
                kind, body = read_reply(port, image_id, start + timeout)
            except FrameError as error:
                if error.image_id is not None and error.image_id < image_id:
                    continue
                resync = True
                if echo:
                    observations[0] = Observation("frame_error", elapsed_ms(), str(error))
                    pending.clear()
                elif error.model_id in pending and error.image_id == image_id:
                    observations[error.model_id] = Observation("frame_error", elapsed_ms(), str(error))
                    pending.remove(error.model_id)
                else:
                    note = note or str(error)
                continue

            if kind == DIAGNOSTIC and body[0] != 0:
                status = body[0]
                message = DIAGNOSTIC_STATUS.get(status, "unknown diagnostic")
                for model_id in pending:
                    observations[model_id] = Observation(
                        "frame_error", elapsed_ms(), f"Board diagnostic {status}: {message}."
                    )
                pending.clear()
            elif echo:
                if kind != DIAGNOSTIC or len(body) != 1 + PIXEL_COUNT:
                    observations[0] = Observation("frame_error", elapsed_ms(), "Expected a full image echo.")
                else:
                    observations[0] = echo_observation(body, pixels, elapsed_ms())
                pending.clear()
            elif kind == RESULT and body[0] in pending:
                model_id = body[0]
                observations[model_id] = result_observation(body, pixels, elapsed_ms())
                pending.remove(model_id)
            else:
                note = note or (f"Duplicate or unexpected model ID {body[0]}."
                                if kind == RESULT else "Unexpected diagnostic reply.")
    except TimeoutError as error:
        resync = True
        for model_id in pending:
            detail = f"{error} Unattributed framing fault: {note}" if note else str(error)
            observations[model_id] = Observation("timeout", elapsed_ms(), detail)
    except OSError as error:
        for model_id in pending:
            observations[model_id] = Observation("serial_error", elapsed_ms(), str(error))
        return observations, note, False, False

    return observations, note, resync, write_timeout


def run(args):
    images = image_paths(args.dataset, args.limit_per_class)
    tests = ("echo", "results") if args.mode == "both" else (args.mode,)
    total_images = len(images) * args.repeat
    if total_images * len(tests) > 0xFFFFFFFF:
        raise ValueError("Too many requests for 32-bit image IDs.")

    counts = {test: Counter() for test in tests}
    complete_pairs = 0
    request_notes = 0
    image_id = 0
    images_done = 0
    print(f"{len(images)} images per round, {args.repeat} round(s), "
          f"{args.port} at {args.baud} baud, 8N1.", flush=True)
    with serial.Serial(args.port, args.baud, timeout=min(0.05, args.timeout),
                       write_timeout=args.timeout, bytesize=serial.EIGHTBITS,
                       parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                       xonxoff=False, rtscts=False, dsrdtr=False) as port, \
            args.output.open("x", encoding="utf-8-sig", newline="") as log:
        writer = csv.DictWriter(log, fieldnames=CSV_FIELDS)
        writer.writeheader()
        port.reset_input_buffer()
        for round_number in range(1, args.repeat + 1):
            for path in images:
                pixels = load_pixels(path)
                relative_path = path.relative_to(args.dataset).as_posix()
                for test in tests:
                    image_id += 1
                    observed, note, resync, write_timeout = run_request(
                        port, image_id, pixels, test, args.timeout
                    )
                    for model_id, reply in observed.items():
                        counts[test][reply.status] += 1
                        writer.writerow({
                            "round": round_number, "image": relative_path, "image_id": image_id,
                            "test": test, "model": MODELS.get(model_id, ""), "status": reply.status,
                            "round_trip_ms": reply.round_trip_ms, "mismatch_bytes": reply.mismatch_bytes,
                            "first_mismatch_index": reply.first_mismatch_index, "detail": reply.detail,
                            "request_note": note,
                        })
                    log.flush()
                    request_notes += bool(note)
                    if test == "results":
                        complete_pairs += not note and all(reply.status == "ok" for reply in observed.values())
                    if any(reply.status == "serial_error" for reply in observed.values()):
                        raise OSError(f"Serial I/O failed on image {image_id}. Partial log saved to {args.output}.")
                    if resync:
                        # Let the PS abandon a partial RX frame before we send again.
                        if write_timeout:
                            port.reset_output_buffer()
                        time.sleep(0.55)
                        port.reset_input_buffer()
                images_done += 1
                if images_done % 100 == 0 or images_done == total_images:
                    print(f"Processed {images_done}/{total_images} images.", flush=True)

    for test, summary in counts.items():
        total = sum(summary.values())
        print(f"{test}: {summary['ok']}/{total} correct replies; "
              f"{', '.join(f'{key}={count}' for key, count in sorted(summary.items()) if key != 'ok') or 'no failures'}.")
    if "results" in counts:
        print(f"Complete valid CNN/SNN pairs: {complete_pairs}/{images_done}.")
    if request_notes:
        print(f"Requests with extra or unattributed framing problems: {request_notes}.")
    print(f"Log saved to {args.output}")
    return int(request_notes > 0 or any(total["ok"] != sum(total.values()) for total in counts.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Serial port, for example COM3")
    parser.add_argument("--dataset", required=True, type=Path, help="Image split directory, for example train")
    parser.add_argument("--mode", choices=("echo", "results", "both"), default="both")
    parser.add_argument("--output", required=True, type=Path, help="New CSV log file")
    parser.add_argument("--limit-per-class", type=int, help="Maximum images per class folder")
    parser.add_argument("--repeat", type=int, default=1, help="Number of passes through the selected images")
    parser.add_argument("--baud", type=int, default=4_000_000)
    parser.add_argument("--timeout", type=float, default=2.0, help="Seconds per request")
    args = parser.parse_args()
    if min(args.repeat, args.baud) < 1 or (args.limit_per_class is not None and args.limit_per_class < 1):
        parser.error("--repeat, --baud, and --limit-per-class must be positive.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive.")
    try:
        return run(args)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"Interrupted. Completed requests remain in {args.output}.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
