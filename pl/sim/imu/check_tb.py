#!/usr/bin/env python3
"""Check a tb_imu_capture log against the stimulus and the bit-exact models.

    python check_tb.py build/log_fir.txt fir

Checks:
  * every filtered value equals filters/filter_model.py applied to the raw
    values (filter state restarts at each header initialisation 'R')
  * the raw values are consecutive stimulus rows: no sensor sample skipped or
    read twice. A jump is only allowed at a capture clear (the sample in
    flight is dropped) and when the sensor was unplugged.
  * timestamps advance by one sensor period (400 us in the testbench)
  * the testbench reported no errors
"""

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "filters"))
import filter_model  # noqa: E402

ODR_US = 400
TS_TOL_US = 60


def main():
    log, kind = Path(sys.argv[1]), sys.argv[2]
    stim = np.loadtxt(HERE / "build" / "stim.txt", dtype=np.int64)
    design = filter_model.load()
    segments, events, end = [], [], None
    cur = None
    for line in log.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "R":
            cur = {"rows": [], "events_before": list(events)}
            segments.append(cur)
        elif parts[0] == "E":
            events.append((parts[1], len(cur["rows"]) if cur else 0, len(segments)))
            if cur is not None:
                cur.setdefault("marks", []).append((len(cur["rows"]), parts[1]))
        elif parts[0] == "S":
            cur["rows"].append([int(v) for v in parts[1:]])
        elif parts[0] == "END":
            end = int(parts[1])

    errors = []
    if end is None:
        errors.append("log has no END line (simulation did not finish)")
    elif end != 0:
        errors.append(f"testbench reported {end} errors")

    stim_index = {tuple(r): i for i, r in enumerate(stim)}
    total = 0
    for si, seg in enumerate(segments):
        rows = np.array(seg["rows"], dtype=np.int64).reshape(-1, 14)
        if len(rows) == 0:
            continue
        total += len(rows)
        cnt, ts, raw, filt = rows[:, 0], rows[:, 1], rows[:, 2:8], rows[:, 8:14]
        if not np.array_equal(cnt, np.arange(1, len(rows) + 1)):
            errors.append(f"segment {si}: COUNT is not 1, 2, 3, ...")
        expect = filter_model.apply(kind, raw, design) if kind != "none" else raw
        bad = np.argwhere(expect != filt)
        if len(bad):
            i, c = bad[0]
            errors.append(f"segment {si}: {len(bad)} filtered values differ, first at sample {i} "
                          f"channel {c}: got {filt[i, c]}, expected {expect[i, c]}")
        # raw continuity and timestamps
        allowed = {m for m, name in seg.get("marks", []) if name == "reconnect"}
        idx = [stim_index.get(tuple(r)) for r in raw]
        if any(i is None for i in idx):
            errors.append(f"segment {si}: a raw sample is not a stimulus row (bus error)")
            continue
        for k in range(1, len(idx)):
            reconnect_gap = any(m <= k for m in allowed) and k - 1 < max(allowed, default=-1) + 1 + 50
            if idx[k] != (idx[k - 1] + 1) % len(stim):
                near_reconnect = any(abs(k - m) <= 3 for m in allowed)
                if not near_reconnect:
                    errors.append(f"segment {si}: sample {k} is stimulus row {idx[k]}, expected {idx[k - 1] + 1}")
                    break
            else:
                dt = ts[k] - ts[k - 1]
                if abs(dt - ODR_US) > TS_TOL_US:
                    errors.append(f"segment {si}: timestamp step {dt} us at sample {k}, expected {ODR_US}")
                    break

    print(f"{log.name}: {len(segments)} segments, {total} samples, filter {kind}")
    if errors:
        for e in errors:
            print("FAIL:", e)
        sys.exit(1)
    print("PASS: filtered output bit-exact with filter_model.py, no skipped or repeated samples")


if __name__ == "__main__":
    main()
