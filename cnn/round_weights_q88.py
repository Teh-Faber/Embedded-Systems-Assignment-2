#!/usr/bin/env python3
"""One-off: round the exported CNN weights to the Q8.8 grid (multiples of 1/256).

ap_fixed<16,8> truncates float literals, which costs ~8 % accuracy in HLS.
New exports from train_cnn.py are already rounded; this fixes the existing files.
Running it twice changes nothing.
"""
import re
from pathlib import Path

WEIGHTS_DIR = Path(__file__).resolve().parent / "weights_cstyle_folded"
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")  # every value in the C-style array

for f in sorted(WEIGHTS_DIR.glob("*.txt")):
    text = f.read_text()
    f.write_text(NUMBER.sub(lambda m: repr(round(float(m.group()) * 256) / 256), text))
    print(f"rounded {f.name}")
