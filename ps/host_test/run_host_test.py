#!/usr/bin/env python3
"""Build the PS IMU application on the PC with stub drivers and drive it with
pc/imu_tool.py, without a board (Linux or WSL, gcc, Python with numpy, scipy
and pyserial).

    python ps/host_test/run_host_test.py [fir|iir]

The real ps_imu_main.c, ps_imu.c, ps_accel.c and ps_uart.c run against
mock.c (UART on a pseudo-terminal, fake accelerator, DFX that always works)
and pl_sim.py (the capture BRAM). Checked: info, streaming with the filter
check, labelled recording, window snapshot, debug inference with both models
(class and scores from the fake accelerator), SW1 refusing PC inference,
malformed requests.
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import serial

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PS = REPO / "ps"
BUILD = HERE / "build"
TOOL = [sys.executable, str(REPO / "pc" / "imu_tool.py")]
N = json.loads((REPO / "filters" / "filters.json").read_text())["n_samples"]   # window length
RATE = 97.64    # real sample rate of the PL stand-in (the board's sensor); the header says 104 Hz
CFLAGS = ["-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror", "-DSDT", "-no-pie", "-fno-pie",
          f"-I{HERE / 'stubs'}", f"-I{PS}"]

failures = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def tool(port, *args, ok=True, keys=None):
    """Run imu_tool.py. keys: the key presses for record, one per line (empty line = Enter)."""
    r = subprocess.run(TOOL + ["--port", port, *args], capture_output=True, text=True, timeout=60,
                       input=keys if keys is not None else "")
    out = r.stdout + r.stderr
    if ok and r.returncode != 0:
        print(out)
    return r.returncode, out


def build():
    BUILD.mkdir(exist_ok=True)
    common = [str(PS / "ps_uart.c"), str(PS / "ps_accel.c"), str(HERE / "mock.c")]
    subprocess.run(["gcc", *CFLAGS, "-o", str(BUILD / "ps_imu_host"), str(PS / "ps_imu_main.c"),
                    str(PS / "ps_imu.c"), *common], check=True)
    print("built ps_imu_host with -Wall -Wextra -Werror")


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "fir"
    build()
    shm = BUILD / "bram.bin"
    shm.write_bytes(bytes(16384 + 64))
    pty_file = BUILD / "pty.txt"
    pty_file.unlink(missing_ok=True)
    stream_npz = BUILD / "pl_stream.npz"
    stream_npz.unlink(missing_ok=True)          # never compare with the previous run's data
    procs = []
    try:
        procs.append(subprocess.Popen([sys.executable, str(HERE / "pl_sim.py"), str(shm), kind, str(stream_npz),
                                       str(RATE)]))
        # start the PS only after pl_sim.py has written the reference and the BRAM header
        for _ in range(600):
            if stream_npz.exists() and np.fromfile(shm, dtype=np.uint32, count=1)[0] == 0x494D5531:
                break
            time.sleep(0.1)
        time.sleep(0.5)
        env = dict(os.environ, MOCK_SHM=str(shm), MOCK_PTY_FILE=str(pty_file), MOCK_SAMPLES=str(N))
        procs.append(subprocess.Popen([str(BUILD / "ps_imu_host")], env=env))
        for _ in range(50):
            if pty_file.exists() and pty_file.read_text().strip():
                break
            time.sleep(0.1)
        port = pty_file.read_text().strip()
        ref = np.load(stream_npz)
        ctrl = np.memmap(shm, dtype=np.uint32, mode="r+", shape=(16384 // 4 + 16,))[16384 // 4:]

        rc, out = tool(port, "info")
        check(rc == 0 and f"filter         {kind}" in out and f"n_samples      {N}" in out
              and "imu_ok         True" in out, "info: filter, window length, sensor ok")

        csv = BUILD / "stream.csv"
        rc, out = tool(port, "stream", "--seconds", "3", "--out", str(csv))
        check(rc == 0 and "no samples lost" in out, "stream 3 s without loss")
        rc, out = tool(port, "check", str(csv))
        check(rc == 0 and "PASS" in out, "check: streamed filtered values = filter_model.py on the raw values")
        d = np.loadtxt(csv, delimiter=",", skiprows=1, dtype=np.int64)
        check(np.array_equal(d[:, 2:8], ref["raw"][: len(d)]) and np.array_equal(d[:, 8:14], ref["filt"][: len(d)]),
              "stream: raw and filtered match the PL stand-in sample for sample")
        dts = np.diff(d[:, 1])
        check(abs(np.median(dts) - 1e6 / RATE) < 500, f"stream: timestamps step {np.median(dts):.0f} us ({RATE} Hz)")
        check(abs(len(d) - 3 * RATE) <= 2 and d[-1, 1] - d[0, 1] < 3e6,
              f"stream --seconds 3: 3 s of real time ({len(d)} samples)")

        rec = BUILD / "rec"
        shutil.rmtree(rec, ignore_errors=True)
        folder = rec / "circle"
        # auto: take 1 saved (Enter), take 2 recorded again (Space), then q stops the run
        rc, out = tool(port, "record", "--label", "circle", "--takes", "3", "--seconds", "1", "--warmup", "0.3",
                       "--auto", "--out", str(rec), keys="\n \nq\n")
        runs = sorted(folder.glob("runs/*/run.json"))
        meta = json.loads(runs[0].read_text()) if runs else {}
        takes = meta.get("takes", [])
        check(rc == 0 and len(runs) == 1 and [t["file"] for t in takes] == ["circle_001.csv"]
              and all(t["complete"] for t in takes) and len(meta.get("discarded", [])) == 2
              and (folder / "circle_001.csv").exists(),
              "record --auto: Enter saves circle_001.csv, Space redoes, q stops; run.json lists 1 saved, 2 discarded")
        check(all(abs(t["samples"] - RATE) <= 2 for t in takes) and abs(meta.get("fs_measured_hz", 0) - RATE) < 1,
              f"record --seconds 1: takes of 1 s real time ({[t['samples'] for t in takes]} samples)")
        # manual: Enter starts, Enter saves; the numbering continues
        rc, out = tool(port, "record", "--label", "circle", "--takes", "1", "--seconds", "1", "--warmup", "0.3",
                       "--out", str(rec), keys="\n\n")
        check(rc == 0 and (folder / "circle_002.csv").exists() and len(list(folder.glob("runs/*/run.json"))) == 2,
              "record: a second run saves circle_002.csv and its own run log")
        rc, out = tool(port, "check", str(folder))
        check(rc == 0 and out.count("PASS") == 2, "check: both runs of the shape folder")
        # a deleted take frees its number: the next run fills the gap first
        (folder / "circle_001.csv").unlink()
        rc, out = tool(port, "record", "--label", "circle", "--takes", "2", "--seconds", "1", "--warmup", "0.3",
                       "--out", str(rec), keys="\n\n\n\n")
        names = sorted(p.name for p in folder.glob("circle_*.csv"))
        check(rc == 0 and names == ["circle_001.csv", "circle_002.csv", "circle_003.csv"],
              f"record: the free number 001 is used again, then 003 ({names})")
        r = subprocess.run([sys.executable, str(REPO / "pc" / "imu_path.py"), str(folder)],
                           capture_output=True, text=True, timeout=60)
        check(r.returncode == 0 and "3 takes from 3 run(s), 1 deleted or replaced takes left out" in r.stdout
              and (folder / "path.html").exists(),
              "imu_path: 3 takes, the replaced circle_001 of the first run left out")

        time.sleep(N / RATE + 0.5)              # one full window since the last clear (record)
        win = BUILD / "window.csv"
        rc, out = tool(port, "window", "--out", str(win))
        w = np.loadtxt(win, delimiter=",", skiprows=1, dtype=np.int64)
        end = int(w[-1, 0]) + 1
        check(rc == 0 and len(w) == N and np.array_equal(w[:, 8:14], ref["filt"][end - N:end]),
              f"window: the newest {N} filtered samples, oldest first")

        res = BUILD / "infer.json"
        rc, out = tool(port, "infer", "--input", str(win), "--models", "both", "--classes", "5", "--json", str(res))
        r = json.loads(res.read_text()) if res.exists() else {"results": {}, "dfx_us": {}}
        s = int(w[:, 8:14].sum())
        exp_cls = s % 5
        cnn, snn = r["results"].get("cnn", {}), r["results"].get("snn", {})
        exp_cnn = [int(np.int16(np.int64(s + 1000 * i) & 0xFFFF)) for i in range(5)]
        exp_snn = [int(np.int32(np.int64(s + 1000 * i) & 0xFFFFFFFF)) for i in range(5)]
        check(rc == 0 and cnn.get("class_id") == exp_cls and cnn.get("scores") == exp_cnn,
              "infer: CNN class and 16-bit scores from the window the PC sent")
        check(snn.get("class_id") == exp_cls and snn.get("scores") == exp_snn, "infer: SNN class and 32-bit scores")
        check(r["dfx_us"] == {"cnn": 1234, "snn": 1234}, "infer: one DFX load per model, timing reported")

        # "both" ran the SNN last, so it is still loaded: no switch for it
        rc, out = tool(port, "infer", "--input", str(win), "--models", "snn", ok=False)
        check(rc == 0 and "loaded by DFX" not in out, "infer again with the loaded model: no switch")
        rc, out = tool(port, "infer", "--input", str(win), "--models", "cnn", ok=False)
        check(rc == 0 and "CNN loaded by DFX" in out, "infer with the other model: one switch")

        ctrl[2] = 1
        time.sleep(0.05)
        rc, out = tool(port, "infer", "--input", str(win), ok=False)
        check(rc != 0 and "SW1 is high" in out, "SW1 high: PC inference refused")
        rc, out = tool(port, "info")
        check("sw1            1" in out, "info: SW1 reported")
        ctrl[2] = 0

        # malformed requests: wrong length, wrong type, unknown command
        with serial.Serial(port, timeout=1) as ser:
            hdr = struct.Struct("<HBBI")

            def ask(kind_, body, ident):
                ser.write(b"\xa6\x6a" + hdr.pack(len(body), 1, kind_, ident) + body)
                data = ser.read(64)
                i = data.find(bytes([0xA5]) + hdr.pack(1, 1, 3, ident))
                return data[i + 9] if i >= 0 else None

            check(ask(17, struct.pack("<BBH", 1, 5, N) + bytes(100), 901) == 1, "infer with a short body: status 1")
            check(ask(1, bytes(10), 902) == 6, "request of an unknown type: status 6")
            check(ask(16, bytes([9]), 903) == 11, "unknown IMU command: status 11")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=5)

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
