#!/usr/bin/env python3
"""Stand-in for the PL capture (imu_capture.vhd) in the PS host test.

Writes the capture BRAM image into a shared file with the same layout and
write order as the RTL: one sample every 1/rate seconds, raw words, filtered
words, timestamp, then COUNT, LAST_TS and STATUS. Handles capture clears
requested through the mock GPIO (mock.c). The filtered values come from
filters/filter_model.py, so this tests the PS and PC plumbing, not the filter
(the GHDL testbench and `imu_tool.py check` on the board do that).

    python pl_sim.py <shm file> <fir|iir|none> <stream.npz> [rate_hz]

rate_hz is the real sample rate (default: the nominal fs). The sensor on the
test board runs at 97.6 Hz while the header says 104 Hz, as here.
"""

import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "filters"))
import filter_model  # noqa: E402

BRAM_BYTES = 16384
CTRL_CLEAR_REQ, CTRL_CLEAR_ACK, CTRL_SW1 = 0, 1, 2
FILTER_ID = {"none": 0, "fir": 1, "iir": 2}


def main():
    shm, kind, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    design = filter_model.load()
    fs, n = design["fs_hz"], design["n_samples"]
    rate = float(sys.argv[4]) if len(sys.argv) > 4 else fs     # real sample rate
    slots = n + 1
    ring = ((12 * slots + 255) // 256) * 256
    filt_off, raw_off = 256, 256 + ring
    ts_off = raw_off + ring

    # 200 s of gesture-like motion, filtered from a zero state like after a clear
    rng = np.random.default_rng(3)
    t = np.arange(int(200 * fs)) / fs
    raw = np.stack([
        6000 * np.sin(2 * np.pi * 1.1 * t), 4000 * np.cos(2 * np.pi * 0.9 * t), 8200 + 0 * t,
        9000 * np.sin(2 * np.pi * 1.7 * t), 3000 * np.sin(2 * np.pi * 0.4 * t), 2000 * np.cos(2 * np.pi * 2.3 * t),
    ], axis=1) + 300 * rng.standard_normal((len(t), 6))
    raw = np.clip(np.round(raw), -32768, 32767).astype(np.int64)
    filt = filter_model.apply(kind, raw, design)
    # write to a temporary file and rename it, so the test never reads a half-written file
    tmp = out.with_name(out.stem + ".tmp.npz")
    np.savez(tmp, raw=raw, filt=filt)
    tmp.replace(out)

    mem = np.memmap(shm, dtype=np.uint32, mode="r+", shape=(BRAM_BYTES // 4 + 16,))
    ctrl = mem[BRAM_BYTES // 4:]

    def w(offset, value):
        mem[offset // 4] = np.uint32(value & 0xFFFFFFFF)

    def words(rec):
        r = [int(v) & 0xFFFF for v in rec]
        return [r[0] | r[1] << 16, r[2] | r[3] << 16, r[4] | r[5] << 16]

    def init():
        w(0x00, 0x494D5531)
        w(0x04, n << 16 | FILTER_ID[kind] << 8 | 1)
        w(0x08, 12 << 24 | 6 << 16 | fs)
        w(0x0C, 0)
        w(0x10, 1)
        w(0x14, 0)
        w(0x18, 0x6C)
        w(0x1C, slots)
        w(0x20, filt_off)
        w(0x24, raw_off)
        w(0x28, ts_off)
        w(0x2C, BRAM_BYTES)

    init()
    k, t0 = 0, time.monotonic()
    while True:
        if ctrl[CTRL_CLEAR_REQ] != ctrl[CTRL_CLEAR_ACK]:
            init()
            k, t0 = 0, time.monotonic()
            ctrl[CTRL_CLEAR_ACK] = ctrl[CTRL_CLEAR_REQ]
        now = time.monotonic()
        if now < t0 + k / rate:
            time.sleep(0.0005)
            continue
        slot = k % slots
        for i, v in enumerate(words(raw[k])):
            w(raw_off + 12 * slot + 4 * i, v)
        for i, v in enumerate(words(filt[k])):
            w(filt_off + 12 * slot + 4 * i, v)
        ts = int((now - t0) * 1e6)
        w(ts_off + 4 * slot, ts)
        w(0x0C, k + 1)
        w(0x14, ts)
        w(0x10, 1 | (int(ctrl[CTRL_SW1]) & 1) << 1)
        k += 1


if __name__ == "__main__":
    main()
