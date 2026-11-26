#!/usr/bin/env python3
"""PC side of the IMU application (ps/ps_imu_main.c).

    py imu_tool.py --port COM8 info
    py imu_tool.py --port COM8 record --label circle --takes 20
    py imu_tool.py --port COM8 stream --seconds 30 --out idle.csv
    py imu_tool.py check ../data/imu/circle               # hardware filter vs filter_model.py
    py imu_tool.py --port COM8 window --out window.csv   # newest filtered window from the board
    py imu_tool.py --port COM8 infer --input window.csv --models both --classes 5

record: clears the capture (filter state starts at zero), streams every sample
(timestamp, raw and filtered a_x a_y a_z g_x g_y g_z), discards a warm-up and
then records takes of one shape. Before a take: Enter starts it (--auto: a
3-2-1 countdown instead). After a take: Enter saves it, Space records it
again, q stops the run (the saved takes stay). Saved takes go to
<out>/<label>/<label>_NNN.csv. A new take gets the lowest free number, so
the numbers of deleted takes are used again. Each run also writes
<out>/<label>/runs/<date>_<time>/ with the whole stream (stream.csv) and
run.json (sample rate, filter, IMU scales, takes). The whole stream is kept
so that `check` can compare the filter bit for bit, and so that the takes can
be filtered again offline (raw/FIR/IIR training sets from one recording).

All durations (--seconds, --warmup) are real time, measured with the PL
timestamps. The sensor's own clock sets the sample rate (97.6 Hz on the test
board instead of the nominal 104 Hz), so the number of samples in a take
follows from the measured rate.

infer: debug inference with SW1 low. The window goes to the accelerator
exactly as sent: int16[n_samples][6], oldest sample first.
"""

import argparse
import csv
import datetime as dt
import json
import os
import queue
import re
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import serial

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "filters"))
import filter_model  # noqa: E402

DEFAULT_PORT = "COM3"
DEFAULT_BAUD = 4_000_000
DEFAULT_OUT = REPO / "data" / "imu"

REQUEST_START = b"\xa6\x6a"
REPLY_START, REPLY_END = 0xA5, 0x5A
VERSION = 1
HEADER = struct.Struct("<HBBI")
DIAGNOSTIC, DFX_TIMING = 3, 4
IMU_COMMAND, IMU_INFER, IMU_INFO, IMU_SAMPLES, IMU_WINDOW, IMU_RESULT = 16, 17, 18, 19, 20, 21
CMD_INFO, CMD_STREAM_START, CMD_STREAM_STOP, CMD_CLEAR, CMD_WINDOW = 0, 1, 2, 3, 4
INFO = struct.Struct("<BBHHHIIIIBBBB")
SAMPLES_HEAD = struct.Struct("<IIB")
RECORD = struct.Struct("<I6h6h")
RESULT_HEAD = struct.Struct("<BBBBIII")
MODELS = {1: "cnn", 2: "snn"}
MODEL_MASKS = {"cnn": 1, "snn": 2, "both": 3}
FILTERS = {0: "none", 1: "fir", 2: "iir"}
T_WRAP = 1 << 32            # t_us from the PL is a 32-bit microsecond counter (wraps after 71.6 min)
CHANNELS = ["a_x", "a_y", "a_z", "g_x", "g_y", "g_z"]
COLUMNS = ["index", "t_us"] + [f"raw_{c}" for c in CHANNELS] + [f"filt_{c}" for c in CHANNELS]
STATUS_NAMES = {
    0: "ok", 1: "invalid length", 2: "invalid model mask", 3: "model unavailable",
    4: "inference failed or timed out", 5: "unsupported version", 6: "unsupported message type",
    7: "truncated request", 9: "SW1 is high (live mode): PC inference refused",
    10: "IMU capture not ready", 11: "unknown command",
}


class BoardError(Exception):
    pass


class Board:
    """Serial link with a reader thread. Sample frames go to self.samples,
    every other reply to self.replies."""

    def __init__(self, port, baud=DEFAULT_BAUD):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.ser.reset_input_buffer()
        self.samples = queue.Queue()
        self.replies = queue.Queue()
        self.next_id = 1
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
        self.ser.close()

    def _read(self, n):
        data = bytearray()
        while len(data) < n and not self.stop.is_set():
            data.extend(self.ser.read(n - len(data)))
        return bytes(data)

    def _reader(self):
        while not self.stop.is_set():
            b = self.ser.read(1)
            if not b or b[0] != REPLY_START:
                continue
            head = self._read(HEADER.size)
            if len(head) < HEADER.size:
                continue
            length, version, kind, ident = HEADER.unpack(head)
            body = self._read(length)
            end = self._read(1)
            if version != VERSION or len(body) != length or end != bytes([REPLY_END]):
                continue    # garbage that looked like a start byte
            if kind == IMU_SAMPLES:
                self.samples.put(body)
            else:
                self.replies.put((kind, ident, body))

    def send(self, kind, body):
        ident = self.next_id
        self.next_id += 1
        self.ser.write(REQUEST_START + HEADER.pack(len(body), VERSION, kind, ident) + body)
        return ident

    def reply(self, ident, timeout=3.0):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BoardError("no reply from the board (right application? right port?)")
            try:
                kind, rid, body = self.replies.get(timeout=left)
            except queue.Empty:
                continue
            if rid == ident:
                return kind, body

    def command(self, cmd, arg=None, timeout=3.0):
        body = bytes([cmd]) if arg is None else bytes([cmd, arg])
        kind, body = self.reply(self.send(IMU_COMMAND, body), timeout)
        if kind == DIAGNOSTIC and body[0] != 0:
            raise BoardError(STATUS_NAMES.get(body[0], f"status {body[0]}"))
        return kind, body

    def info(self):
        kind, body = self.command(CMD_INFO)
        if kind != IMU_INFO:
            raise BoardError(f"unexpected reply type {kind}")
        f = INFO.unpack(body)
        keys = ["app_version", "filter_id", "n_samples", "slots", "odr_hz", "count", "status",
                "sensor", "last_ts_us", "sw1", "loaded_model", "imu_ready", "streaming"]
        d = dict(zip(keys, f))
        st = d["status"]
        d["filter"] = FILTERS.get(d["filter_id"], "?")
        d["imu_ok"] = bool(st & 1)
        d["decoupled"] = bool(st & 4)
        d["imu_retries"] = (st >> 8) & 0xFF
        d["filter_errors"] = (st >> 16) & 0xFF
        d["overruns"] = (st >> 24) & 0xFF
        d["who_am_i"] = f"0x{d['sensor'] & 0xFF:02X}"
        return d

    def window(self):
        kind, body = self.command(CMD_WINDOW)
        if kind != IMU_WINDOW:
            raise BoardError(f"unexpected reply type {kind}")
        end, n = struct.unpack_from("<IH", body)
        data = np.frombuffer(body[6:], dtype="<i2").reshape(n, 6).astype(np.int16)
        return end, data

    def infer(self, window, mask, n_classes, timeout=10.0):
        window = np.asarray(window, dtype="<i2")
        body = struct.pack("<BBH", mask, n_classes, window.shape[0]) + window.tobytes()
        ident = self.send(IMU_INFER, body)
        pending = {m for m in MODELS if mask & m}
        results, dfx = {}, {}
        while pending:
            kind, body = self.reply(ident, timeout)
            if kind == DIAGNOSTIC:
                raise BoardError(STATUS_NAMES.get(body[0], f"status {body[0]}"))
            if kind == DFX_TIMING:
                model, us = struct.unpack("<BI", body)
                dfx[MODELS[model]] = us
                continue
            if kind != IMU_RESULT:
                raise BoardError(f"unexpected reply type {kind}")
            model, status, cls, n, infer_us, prep_us, win = RESULT_HEAD.unpack_from(body)
            scores = list(struct.unpack_from(f"<{n}i", body, RESULT_HEAD.size))
            results[MODELS[model]] = {
                "status": status, "status_text": STATUS_NAMES.get(status, str(status)),
                "class_id": cls if status == 0 else None, "infer_us": infer_us,
                "prep_us": prep_us, "scores": scores if status == 0 else None,
            }
            pending.discard(model)
        return results, dfx


class Stream:
    """Collects streamed samples; checks that indices are consecutive."""

    def __init__(self, board):
        self.board = board
        self.rows = []          # index, t_us, raw[6], filt[6]
        self.lost = 0
        self.gaps = []
        self.t_wrap = 0         # added to t_us after the PL counter wrapped
        self.t_prev = None

    def pump(self, timeout=0.05):
        try:
            body = self.board.samples.get(timeout=timeout)
        except queue.Empty:
            return
        while True:
            first, lost, n = SAMPLES_HEAD.unpack_from(body)
            if lost:
                self.lost += lost
                self.gaps.append((first - lost, lost))
            expected = self.rows[-1][0] + 1 if self.rows else first
            if self.rows and first != expected and not lost:
                self.gaps.append((expected, first - expected))
            for i in range(n):
                rec = RECORD.unpack_from(body, SAMPLES_HEAD.size + RECORD.size * i)
                if self.t_prev is not None and rec[0] < self.t_prev:
                    self.t_wrap += T_WRAP
                self.t_prev = rec[0]
                self.rows.append((first + i, rec[0] + self.t_wrap) + rec[1:])
            try:
                body = self.board.samples.get_nowait()
            except queue.Empty:
                return

    def last_index(self):
        return self.rows[-1][0] if self.rows else -1

    def wait_until(self, index, timeout):
        """Wait for the sample with this index."""
        deadline = time.monotonic() + timeout
        while self.last_index() < index:
            if time.monotonic() > deadline:
                raise BoardError("samples stopped arriving (sensor unplugged? see `info`)")
            self.pump()

    def wait_time(self, t_us, timeout):
        """Wait for a sample with a PL timestamp of t_us or later."""
        deadline = time.monotonic() + timeout
        while not self.rows or self.rows[-1][1] < t_us:
            if time.monotonic() > deadline:
                raise BoardError("samples stopped arriving (sensor unplugged? see `info`)")
            self.pump()

    def time_of(self, index):
        """PL timestamp of the sample with this index (or the next one received)."""
        t = None
        for r in reversed(self.rows):
            if r[0] < index:
                break
            t = r[1]
        if t is None:
            raise BoardError(f"sample {index} was not received")
        return t


def rate_hz(rows):
    """Sample rate measured with the PL timestamps."""
    if len(rows) < 2 or rows[-1][1] == rows[0][1]:
        return 0.0
    return (rows[-1][0] - rows[0][0]) * 1e6 / (rows[-1][1] - rows[0][1])


def save_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)


def load_csv(path):
    data = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.int64, ndmin=2)
    return data


def run_meta(board_info, args, extra):
    design = filter_model.load()
    return {
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "label": getattr(args, "label", None),
        "board": board_info,
        "odr_nominal_hz": board_info["odr_hz"],
        "window_samples": board_info["n_samples"],
        "filter": board_info["filter"],
        "filter_design": {k: design[k] for k in ("fir", "iir")},
        "imu": design["imu"],
        "units": {"raw_a": f"{design['imu']['accel_mg_per_lsb']} mg/LSB",
                  "raw_g": f"{design['imu']['gyro_mdps_per_lsb']} mdps/LSB",
                  "t_us": "microseconds since the capture was cleared (PL clock, real time)"},
        "columns": COLUMNS,
        **extra,
    }


def cmd_info(args):
    b = Board(args.port, args.baud)
    try:
        info = b.info()
    finally:
        b.close()
    for k, v in info.items():
        print(f"{k:14s} {v}")


def cmd_stream(args):
    b = Board(args.port, args.baud)
    try:
        info = b.info()
        s = Stream(b)
        b.command(CMD_STREAM_START, 1)
        s.wait_until(0, 5)
        # args.seconds of real time from the first sample, by the PL timestamps
        t_end = s.rows[0][1] + round(args.seconds * 1e6)
        s.wait_time(t_end, args.seconds + 5)
        b.command(CMD_STREAM_STOP)
        rows = [r for r in s.rows if r[1] < t_end]
    finally:
        b.close()
    out = Path(args.out)
    save_csv(out, rows)
    fs = rate_hz(rows)
    meta = run_meta(info, args, {"seconds": args.seconds, "fs_measured_hz": round(fs, 3),
                                     "samples": len(rows), "lost": s.lost, "gaps": s.gaps})
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{len(rows)} samples in {args.seconds:g} s ({fs:.2f} Hz measured, filter {info['filter']}) -> {out}")
    report_loss(s)


def report_loss(s):
    if s.lost or s.gaps:
        print(f"WARNING: {s.lost} samples lost, gaps at {s.gaps}. The PC did not keep up "
              "(serial buffer) or the stream was interrupted.")
    else:
        print("no samples lost")


class Keys:
    """Single key presses without Enter: the Windows console (msvcrt) or a
    POSIX terminal (cbreak mode). When stdin is not a terminal (tests, pipes),
    each input line is one key: an empty line is Enter, otherwise the line's
    first character."""

    ENTER, SPACE = "\r", " "

    def __enter__(self):
        self.tty = sys.stdin.isatty()
        self.old = None
        self.lines = queue.Queue()
        if not self.tty:
            threading.Thread(target=self._read_lines, daemon=True).start()
        elif os.name != "nt":
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def _read_lines(self):
        for line in sys.stdin:
            line = line.rstrip("\r\n")
            self.lines.put(line[0] if line else self.ENTER)

    def get(self):
        """The next key, or None. Enter is ENTER, Ctrl+C is q."""
        if not self.tty:
            try:
                return self.lines.get_nowait()
            except queue.Empty:
                return None
        if os.name == "nt":
            import msvcrt
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):        # arrow and function keys send two codes
                msvcrt.getwch()
                return None
        else:
            import select
            if not select.select([sys.stdin], [], [], 0)[0]:
                return None
            ch = os.read(self.fd, 1).decode(errors="ignore")
        if ch == "\n":
            return self.ENTER
        if ch == "\x03":
            return "q"
        return ch.lower()

    def flush(self):
        """Forget the keys pressed during a take (terminal only)."""
        if self.tty:
            while self.get() is not None:
                pass


def wait_key(stream, keys, allowed):
    """Keep reading samples until one of the allowed keys is pressed."""
    while True:
        ch = keys.get()
        if ch in allowed:
            return ch
        stream.pump(0.02)


def countdown(stream):
    print("  3", end="", flush=True)
    for c in (2, 1, "GO"):
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            stream.pump(0.02)
        print(f" {c}", end="", flush=True)
    print()


def free_take_number(folder, label):
    """The lowest number n >= 1 without a <label>_NNN.csv file in the folder."""
    pattern = re.compile(re.escape(label) + r"_(\d+)\.csv")
    used = {int(m.group(1)) for p in folder.glob(f"{label}_*.csv") if (m := pattern.fullmatch(p.name))}
    n = 1
    while n in used:
        n += 1
    return n


def cmd_record(args):
    folder = Path(args.out) / args.label
    run_dir = folder / "runs" / dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    b = Board(args.port, args.baud)
    saved, discarded = [], []
    s = None
    try:
        info = b.info()
        if not info["imu_ok"]:
            raise BoardError(f"sensor not ok (WHO_AM_I {info['who_am_i']}, retries {info['imu_retries']})")
        s = Stream(b)
        b.command(CMD_STREAM_START, 1)          # clear: filter state starts at zero
        s.wait_until(0, 5)
        print(f"filter {info['filter']}; warming up {args.warmup} s ...")
        s.wait_time(s.rows[0][1] + round(args.warmup * 1e6), args.warmup + 5)
        print(f"sample rate {rate_hz(s.rows):.2f} Hz (PL timestamps; nominal {info['odr_hz']} Hz)")
        span = round(args.seconds * 1e6)        # length of a take in PL microseconds
        folder.mkdir(parents=True, exist_ok=True)
        with Keys() as keys:
            while len(saved) < args.takes:
                name = f"{args.label}_{free_take_number(folder, args.label):03d}"
                head = f"take {len(saved) + 1}/{args.takes} ({name})"
                if args.auto:
                    print(head)
                    countdown(s)
                else:
                    keys.flush()
                    print(f"{head}: Enter = start, q = stop")
                    if wait_key(s, keys, {Keys.ENTER, "q"}) == "q":
                        break
                    print("  GO")
                # the take: args.seconds of real time from its first sample
                start = s.last_index() + 1
                s.wait_until(start, 5)
                t_start = s.time_of(start)
                s.wait_time(t_start + span, args.seconds + 5)
                sel = [r for r in s.rows if r[0] >= start and r[1] < t_start + span]
                # complete: no sample of the take was lost on the way to the PC
                complete = sel[0][0] == start and sel[-1][0] - sel[0][0] + 1 == len(sel)
                take = {"first_index": start, "samples": len(sel), "t_start_us": t_start, "complete": complete}
                warn = "" if complete else " WARNING: samples lost, redo this take."
                keys.flush()
                print(f"  {len(sel)} samples.{warn} Enter = save, Space = redo, q = stop")
                key = wait_key(s, keys, {Keys.ENTER, Keys.SPACE, "q"})
                if key == Keys.ENTER:
                    take["file"] = f"{name}.csv"
                    save_csv(folder / take["file"], sel)
                    saved.append(take)
                    print(f"  saved {take['file']}")
                else:
                    discarded.append(take)
                    print("  discarded")
                    if key == "q":
                        break
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            b.command(CMD_STREAM_STOP)
        except Exception:
            pass
        b.close()

    if not saved:
        print("no takes saved")
        return
    rows = s.rows
    save_csv(run_dir / "stream.csv", rows)
    meta = run_meta(info, args, {"warmup_s": args.warmup, "take_seconds": args.seconds,
                                     "fs_measured_hz": round(rate_hz(rows), 3),
                                     "takes": saved, "discarded": discarded,
                                     "stream_samples": len(rows), "lost": s.lost, "gaps": s.gaps})
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{len(saved)} takes of '{args.label}' saved in {folder} (run log: {run_dir})")
    report_loss(s)


def check_stream(stream, meta_path, kind=None):
    """Compare the filtered columns of one stream with filter_model.py."""
    meta = json.loads(meta_path.read_text())
    kind = kind or meta["filter"]
    d = load_csv(stream)
    idx, raw, filt = d[:, 0], d[:, 2:8], d[:, 8:14]
    if idx[0] != 0 or not np.array_equal(idx, np.arange(len(idx))):
        print(f"FAIL {stream}: the stream must start at index 0 after a clear and have no gaps")
        return False
    design = filter_model.load()
    expect = filter_model.apply(kind, raw, design)
    bad = np.argwhere(expect != filt)
    print(f"{stream}: {len(idx)} samples, filter {kind}: {len(bad)} of {filt.size} values differ from filter_model.py")
    if len(bad):
        i, c = bad[0]
        print(f"FAIL: first difference at sample {i}, {CHANNELS[c]}: board {filt[i, c]}, model {expect[i, c]}")
        return False
    print("PASS: the hardware filter is bit-exact with the fixed-point model")
    return True


def cmd_check(args):
    """Recompute the filter from the raw columns and compare with the board.
    The path is a shape folder (all its runs), a run folder or a stream CSV."""
    path = Path(args.path)
    if path.is_dir():
        streams = sorted(path.glob("runs/*/stream.csv")) or [path / "stream.csv"]
        # run.json; session.json in recordings made before 26 Nov 2026
        items = [(p, p.parent / "run.json" if (p.parent / "run.json").exists() else p.parent / "session.json")
                 for p in streams]
    else:
        items = [(path, path.with_suffix(".json"))]
    results = [check_stream(p, m, args.filter) for p, m in items]
    if not all(results):
        sys.exit(f"{results.count(False)} of {len(results)} streams FAILED")


def cmd_window(args):
    b = Board(args.port, args.baud)
    try:
        end, data = b.window()
    finally:
        b.close()
    rows = [(end - len(data) + i, 0, *([0] * 6), *data[i]) for i in range(len(data))]
    save_csv(Path(args.out), rows)
    print(f"window of {len(data)} samples ending before index {end} -> {args.out}")


def cmd_infer(args):
    if args.input:
        d = load_csv(args.input)
        window = d[:, 8:14] if args.column == "filt" else d[:, 2:8]
        if args.n_samples and len(window) > args.n_samples:
            window = window[-args.n_samples:]
    else:
        window = None
    b = Board(args.port, args.baud)
    try:
        if window is None:
            _, window = b.window()
            print(f"using the board's current window ({len(window)} samples)")
        t0 = time.monotonic()
        results, dfx = b.infer(window, MODEL_MASKS[args.models], args.classes)
        rtt = (time.monotonic() - t0) * 1000
    finally:
        b.close()
    for model, us in dfx.items():
        print(f"{model.upper()} loaded by DFX in {us} us")
    for model, r in results.items():
        if r["status"] == 0:
            print(f"{model.upper()}: class {r['class_id']}  scores {r['scores']}  "
                  f"inference {r['infer_us']} us  copy {r['prep_us']} us")
        else:
            print(f"{model.upper()}: {r['status_text']}")
    print(f"round trip {rtt:.1f} ms")
    if args.json:
        Path(args.json).write_text(json.dumps({"results": results, "dfx_us": dfx}, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="capture configuration and sensor state")

    r = sub.add_parser("record", help="takes of one shape for training data")
    r.add_argument("--label", required=True, help="shape (class) name, e.g. circle, square, idle")
    r.add_argument("--takes", type=int, default=10, help="number of takes to save")
    r.add_argument("--seconds", type=float, default=2.5, help="length of one take, real time")
    r.add_argument("--warmup", type=float, default=1.0, help="discarded after the clear (filter transient)")
    r.add_argument("--auto", action="store_true", help="3-2-1 countdown instead of Enter before each take")
    r.add_argument("--out", default=str(DEFAULT_OUT), help="the takes go to <out>/<label>/")

    s = sub.add_parser("stream", help="one continuous recording")
    s.add_argument("--seconds", type=float, default=10.0)
    s.add_argument("--out", required=True)

    c = sub.add_parser("check", help="compare the hardware filter with filter_model.py")
    c.add_argument("path", help="a shape folder or run folder from record, or a CSV from stream")
    c.add_argument("--filter", choices=["fir", "iir", "none"], help="override the filter in the metadata")

    w = sub.add_parser("window", help="the newest filtered window from the board")
    w.add_argument("--out", default="window.csv")

    i = sub.add_parser("infer", help="debug inference on a window (SW1 low)")
    i.add_argument("--input", help="CSV (record/stream/window format); default: the board's current window")
    i.add_argument("--column", choices=["filt", "raw"], default="filt")
    i.add_argument("--n-samples", type=int, default=0, help="use the last n rows of the CSV")
    i.add_argument("--models", choices=sorted(MODEL_MASKS), default="cnn")
    i.add_argument("--classes", type=int, default=5, help="number of model outputs to read")
    i.add_argument("--json", help="also save the results here")

    args = p.parse_args()
    try:
        {"info": cmd_info, "record": cmd_record, "stream": cmd_stream, "check": cmd_check,
         "window": cmd_window, "infer": cmd_infer}[args.cmd](args)
    except BoardError as e:
        sys.exit(f"board: {e}")


if __name__ == "__main__":
    main()
