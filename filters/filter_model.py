"""Bit-exact models of the PL filters, shared by the design script, the GHDL
test vectors and the PC tools.

Both filters work on int16 IMU samples, one column per channel, in the order
the PL uses: a_x, a_y, a_z, g_x, g_y, g_z. Channels are independent.

FIR (Xilinx FIR Compiler, lab 6 flow)
    acc = sum_k h[k] * x[n-k]                integer coefficients, scale 2^15
    y   = sat16((acc + 2^14) >> 15)          rounding done in imu_capture.vhd

IIR (iir_mc.vhd, cascade of direct form I biquads)
    state and section outputs are Q17.8 (25-bit signed)
    acc = b0*x0 + b1*x1 + b2*x2 + na1*y1 + na2*y2   coefficients Q2.16, 18-bit
          (na1 = -a1, na2 = -a2, stored negated so the DSP only adds)
    y0  = sat25((acc + 2^15) >> 16)
    out = sat16((y_last + 2^7) >> 8)

Python's >> on negative integers rounds towards minus infinity, like
shift_right() on a VHDL signed, so these functions match the RTL bit for bit.
The filter state starts at zero, as after a capture reset in the PL.
"""

import json
from pathlib import Path

import numpy as np

DEFAULT_JSON = Path(__file__).resolve().parent / "filters.json"


def _sat(v, bits):
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return np.minimum(np.maximum(v, lo), hi)


def load(path=DEFAULT_JSON):
    """Read the design written by design_filters.py."""
    with open(path) as f:
        return json.load(f)


def fir_q(x, design):
    """Fixed-point FIR, bit-exact with FIR Compiler + rounding in imu_capture."""
    fir = design["fir"]
    h = np.asarray(fir["coeffs_int"], dtype=np.int64)
    shift = fir["coeff_frac_bits"]
    x = np.asarray(x, dtype=np.int64)
    squeeze = x.ndim == 1
    if squeeze:
        x = x[:, None]
    n = x.shape[0]
    acc = np.zeros_like(x)
    for k, hk in enumerate(h):
        if hk:
            acc[k:] += hk * x[: n - k]
    y = _sat((acc + (1 << (shift - 1))) >> shift, 16)
    return y[:, 0] if squeeze else y


def iir_q(x, design):
    """Fixed-point IIR, bit-exact with iir_mc.vhd."""
    iir = design["iir"]
    frac_c = iir["coeff_frac_bits"]       # 16
    frac_s = iir["state_frac_bits"]       # 8
    sbits = iir["state_bits"]             # 25
    sections = [np.asarray(s, dtype=np.int64) for s in iir["sections_int"]]  # b0 b1 b2 na1 na2
    x = np.asarray(x, dtype=np.int64)
    squeeze = x.ndim == 1
    if squeeze:
        x = x[:, None]
    n, nch = x.shape
    out = np.zeros_like(x)
    for c in range(nch):
        # state per section: x1, x2 of its input, y1, y2 of its output
        st = [[0, 0, 0, 0] for _ in sections]
        for i in range(n):
            v = int(x[i, c]) << frac_s
            for s, (b0, b1, b2, na1, na2) in enumerate(sections):
                x1, x2, y1, y2 = st[s]
                acc = int(b0) * v + int(b1) * x1 + int(b2) * x2 + int(na1) * y1 + int(na2) * y2
                y = int(_sat(np.int64((acc + (1 << (frac_c - 1))) >> frac_c), sbits))
                st[s] = [v, x1, y, y1]
                v = y
            out[i, c] = int(_sat(np.int64((v + (1 << (frac_s - 1))) >> frac_s), 16))
    return out[:, 0] if squeeze else out


def apply(kind, x, design):
    """kind: 'raw', 'fir' or 'iir'."""
    if kind == "raw":
        return np.asarray(x, dtype=np.int64)
    if kind == "fir":
        return fir_q(x, design)
    if kind == "iir":
        return iir_q(x, design)
    raise ValueError(f"unknown filter {kind!r}")


def float_reference(kind, x, design):
    """Floating-point filter with the unquantized coefficients (for comparison)."""
    from scipy import signal
    x = np.asarray(x, dtype=float)
    if kind == "fir":
        return signal.lfilter(design["fir"]["coeffs_float"], [1.0], x, axis=0)
    if kind == "iir":
        return signal.sosfilt(np.asarray(design["iir"]["sos_float"]), x, axis=0)
    return x
