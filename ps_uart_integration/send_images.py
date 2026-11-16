#!/usr/bin/env python3
"""Send test images, print progress and save one JSONL record per request."""

import argparse
import json
import math
from pathlib import Path
import struct
import sys
import time

from PIL import Image
import serial


IMAGE_SIZE = (80, 60)
PIXEL_COUNT = 4800
CLASS_NAMES = ("Circle", "Figure Eight", "Square", "Triangle")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}
MODELS = {1: "cnn", 2: "snn"}
MODEL_MASKS = {"cnn": 1, "snn": 2, "both": 3}

# Requests follow the lab's A6 6A marker and length field. Replies use A5 and
# 5A. The shared header is length, version, message type and image ID.
REQUEST_START = b"\xa6\x6a"
REPLY_START, REPLY_END = b"\xa5", b"\x5a"
VERSION = 1
IMAGE, RESULT, DIAGNOSTIC, DFX_TIMING = 1, 2, 3, 4
HEADER = struct.Struct("<HBBI")
RESULT_BODY = struct.Struct("<BBBII4i")
DFX_BODY = struct.Struct("<BI")
REPLY_LENGTHS = {RESULT: {RESULT_BODY.size}, DIAGNOSTIC: {1, 1 + PIXEL_COUNT},
                 DFX_TIMING: {DFX_BODY.size}}
TEST_RESULT = 128
STATUS_NAMES = {
    1: "invalid request length",
    2: "invalid model mask",
    3: "requested model is unavailable",
    4: "inference failed or timed out",
    5: "unsupported request version",
    6: "unsupported request type",
    7: "request was truncated",
    8: "image echo is unavailable; load the UART test application",
}


def test_groups(dataset, class_names, limit):
    """Use held-out folders and assign the same image IDs in both model passes."""
    root = dataset / "test" if (dataset / "test").is_dir() else dataset
    next_id = 1
    groups = []
    for class_id, name in enumerate(class_names):
        folder = root / name
        if not folder.is_dir():
            raise ValueError(f"Missing test class folder: {folder}")
        paths = sorted(
            (path for path in folder.iterdir()
             if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES),
            key=lambda path: (path.name.casefold(), path.name),
        )
        if not paths:
            raise ValueError(f"No test images in {folder}.")
        groups.append((class_id, list(enumerate(paths[:limit], start=next_id))))
        next_id += len(paths)
    return root, groups


def read_pixels(path):
    """Return the 4,800 raw grayscale bytes; model preparation stays on the PS."""
    with Image.open(path) as image:
        if image.size != IMAGE_SIZE:
            raise ValueError(f"{path}: expected 80 x 60 pixels, got {image.size}.")
        return image.convert("L").tobytes()


def image_frame(image_id, mask, pixels):
    body = bytes([mask]) + pixels
    return REQUEST_START + HEADER.pack(len(body), VERSION, IMAGE, image_id) + body


def send_frame(port, image_id, mask, pixels):
    frame = image_frame(image_id, mask, pixels)
    if port.write(frame) != len(frame):
        raise OSError("The serial port did not accept the entire request.")


def read_exact(port, size, deadline):
    """Serial reads can split any field, including the header."""
    data = bytearray()
    while len(data) < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for a complete reply frame.")
        data.extend(port.read(size - len(data)))
    return bytes(data)


def read_reply(port, image_id, deadline):
    """Read a lab-framed reply and validate its shape and image association."""
    while read_exact(port, 1, deadline) != REPLY_START:
        pass
    size, version, message_type, returned_id = HEADER.unpack(read_exact(port, HEADER.size, deadline))
    if version != VERSION:
        raise ValueError(f"Unsupported reply version {version}.")
    if size not in REPLY_LENGTHS.get(message_type, ()):
        raise ValueError(f"Unexpected reply type {message_type} or body length {size}.")
    body = read_exact(port, size, deadline)
    if read_exact(port, 1, deadline) != REPLY_END:
        raise ValueError("Invalid reply end marker; expected 5A.")
    if returned_id != image_id:
        raise ValueError(f"Received image ID {returned_id}; expected {image_id}.")
    return message_type, body


def status_text(status):
    return f"{STATUS_NAMES.get(status, 'unknown status')} (status={status})"


def check_rejections(port, timeout):
    """The test app must reject one short request and one invalid mask."""
    requests = ((1, 3, b""), (2, 4, bytes(PIXEL_COUNT)))
    for expected_status, mask, pixels in requests:
        send_frame(port, 0, mask, pixels)
        message = read_reply(port, 0, time.monotonic() + timeout)
        if message != (DIAGNOSTIC, bytes([expected_status])):
            raise ValueError(f"Expected a diagnostic for {status_text(expected_status)}.")
        print(f"PROTOCOL PASS: {status_text(expected_status)} was rejected.")


def check_echo(port, image_id, pixels, timeout):
    """Compare every pixel in the PS receive buffer with the original image."""
    start = time.monotonic()
    send_frame(port, image_id, 0, pixels)
    message_type, body = read_reply(port, image_id, start + timeout)
    if message_type != DIAGNOSTIC:
        raise ValueError(f"Expected an echo reply, received type {message_type}.")
    if body[0] != 0:
        raise ValueError(f"Echo rejected: {status_text(body[0])}.")
    if len(body) != 1 + PIXEL_COUNT:
        raise ValueError(f"Echo has {len(body) - 1} pixels; expected {PIXEL_COUNT}.")
    if body[1:] != pixels:
        index = next(i for i, (sent, echoed) in enumerate(zip(pixels, body[1:])) if sent != echoed)
        raise ValueError(f"Echo differs at pixel {index}: sent {pixels[index]}, received {body[index + 1]}.")
    return round((time.monotonic() - start) * 1000, 3)


def decode_result(body, pending, pixels, protocol_test):
    """Check result IDs and keep test data separate from real classifications."""
    model_id, status, class_id, infer_us, prep_us, *scores = RESULT_BODY.unpack(body)
    if model_id not in pending:
        raise ValueError(f"Duplicate or unrequested result from model ID {model_id}.")
    if protocol_test:
        expected = (
            TEST_RESULT, (pixels[0] + model_id) % len(CLASS_NAMES),
            0x01020300 + model_id, 0x04050600 + model_id,
            -100000 - model_id, 200000 + model_id, pixels[PIXEL_COUNT // 2], pixels[-1],
        )
        if (status, class_id, infer_us, prep_us, *scores) != expected:
            raise ValueError(f"{MODELS[model_id].upper()} reply does not match its protocol-test data.")
    elif status == TEST_RESULT:
        raise ValueError("The board is running the UART test app; use --protocol-test.")
    elif status == 0 and class_id >= len(CLASS_NAMES):
        raise ValueError(f"Out-of-range class ID {class_id} for {MODELS[model_id].upper()}.")

    has_result = status in (0, TEST_RESULT)
    return model_id, {
        "status": status,
        "class_id": class_id if has_result else None,
        "infer_us": infer_us if has_result else None,
        "prep_us": prep_us if has_result else None,
        "scores": scores if has_result else None,
    }


def collect_results(port, record, pixels, mask, timeout, protocol_test):
    """Collect the requested model IDs in any order, with no terminal I/O."""
    image_id = record["image_id"]
    pending = {model_id for model_id in MODELS if mask & model_id}
    start = time.monotonic()
    try:
        send_frame(port, image_id, mask, pixels)
        while pending:
            kind, body = read_reply(port, image_id, start + timeout)
            if kind == DIAGNOSTIC:
                reason = status_text(body[0]) if body[0] else "unexpected echo"
                raise ValueError(f"Board diagnostic: {reason}.")
            if kind == DFX_TIMING:
                model_id, duration = DFX_BODY.unpack(body)
                if model_id not in pending or MODELS[model_id] in record["dfx_us"]:
                    raise ValueError(f"Unexpected DFX timing for model ID {model_id}.")
                record["dfx_us"][MODELS[model_id]] = duration
                continue
            model_id, result = decode_result(body, pending, pixels, protocol_test)
            record["results"][MODELS[model_id]] = result
            pending.remove(model_id)
    except (OSError, ValueError, KeyboardInterrupt) as error:
        models = ", ".join(MODELS[model].upper() for model in sorted(pending))
        record["transport_error"] = f"{error or 'Interrupted'} Pending models: {models}."
    finally:
        # Stop at the last validated result, before printing or writing the log.
        record["round_trip_ms"] = round((time.monotonic() - start) * 1000, 3)


def print_results(record, counts):
    """Show the live class, prediction, scores and latency as in the lab client."""
    image_name = record["image_path"]
    true_id = record["true_class_id"]
    names = record["class_names"]
    if record["echo_ms"] is not None:
        print(f"{image_name}: ECHO PASS ({PIXEL_COUNT} bytes, {record['echo_ms']:.3f} ms)")
    for model, result in record["results"].items():
        prefix = f"{image_name} [{model.upper()}] id={record['image_id']}"
        status = result["status"]
        if status == TEST_RESULT:
            print(f"{prefix}: PROTOCOL PASS")
        elif status != 0:
            print(f"{prefix}: ERROR {status_text(status)}")
        else:
            pred = result["class_id"]
            correct = pred == true_id
            counts[model][0] += 1
            counts[model][1] += int(correct)
            switch_time = record["dfx_us"].get(model)
            dfx = f" dfx={switch_time} us" if switch_time is not None else ""
            print(f"{prefix}: true={names[true_id]} pred={names[pred]} "
                  f"scores={tuple(result['scores'])} time={result['infer_us']} us "
                  f"prep={result['prep_us']} us{dfx} {'OK' if correct else 'WRONG'}")


def run(args):
    root, groups = test_groups(args.dataset, args.classes, args.limit_per_class)
    mask = MODEL_MASKS[args.models]
    selected = [name for model_id, name in MODELS.items() if mask & model_id]
    counts = {model: [0, 0] for model in selected}
    total = 0
    test_replies = 0
    print(f"Dataset: {root}\nPort: {args.port} at {args.baud} baud, 8N1\n"
          f"Models: {args.models}\nClass IDs: {dict(enumerate(args.classes))}")
    with args.output.open("x", encoding="utf-8") as log, serial.Serial(
        args.port, args.baud, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=min(0.05, args.timeout),
        write_timeout=args.timeout, xonxoff=False, rtscts=False, dsrdtr=False
    ) as port:
        port.reset_input_buffer()
        if args.protocol_test:
            check_rejections(port, args.timeout)
        for class_id, images in groups:
            print(f"\nProcessing class: {args.classes[class_id]}\nImages selected: {len(images)}")
            for image_id, path in images:
                pixels = read_pixels(path)
                record = {
                    "image_id": image_id,
                    "image_path": path.relative_to(root).as_posix(),
                    "class_names": args.classes,
                    "true_class_id": class_id,
                    "requested_models": selected,
                    "results": {},
                    "dfx_us": {},
                    "echo_ms": None,
                    "transport_error": None,
                }
                if args.protocol_test:
                    record["echo_ms"] = check_echo(port, image_id, pixels, args.timeout)
                collect_results(port, record, pixels, mask, args.timeout, args.protocol_test)
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
                log.flush()
                print_results(record, counts)
                if record["transport_error"]:
                    raise ValueError(f"{record['image_path']}: {record['transport_error']} "
                                     f"Partial results saved to {args.output}.")
                total += 1
                if args.protocol_test:
                    test_replies += len(record["results"])

    print(f"\nImage requests: {total}\nSaved: {args.output}")
    if args.protocol_test:
        print(f"Protocol test passed: 2 rejected requests, {total} byte-for-byte echoes, "
              f"{test_replies} model-format replies.")
        return 0
    for model, (valid, correct) in counts.items():
        accuracy = f"{correct / valid:.2%}" if valid else "n/a"
        print(f"{model.upper()}: {valid}/{total} valid, {correct} correct, accuracy {accuracy}.")
    return int(any(valid != total for valid, _ in counts.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="For example COM3 or /dev/ttyUSB1")
    parser.add_argument("--dataset", required=True, type=Path, help="Dataset root or test folder")
    parser.add_argument("--output", required=True, type=Path, help="New JSONL file")
    parser.add_argument("--models", choices=MODEL_MASKS, default="both")
    parser.add_argument("--classes", nargs=4, default=CLASS_NAMES, metavar="CLASS",
                        help="Four class folders in ascending model class ID order")
    parser.add_argument("--limit-per-class", type=int)
    parser.add_argument("--baud", type=int, default=4_000_000)
    parser.add_argument("--timeout", type=float, default=5.0, help="Seconds per request")
    parser.add_argument("--protocol-test", action="store_true", help="Use with ps_uart_test.c")
    args = parser.parse_args()
    if len(set(args.classes)) != 4:
        parser.error("--classes requires four distinct names.")
    if args.limit_per_class is not None and args.limit_per_class < 1:
        parser.error("--limit-per-class must be positive.")
    if args.baud < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("Use a positive baud rate and a finite positive timeout.")
    try:
        return run(args)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
