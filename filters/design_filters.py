#!/usr/bin/env python3
"""Design the FIR and IIR IMU low-pass filters and write every file the PL,
PS and PC need from them.

    python filters/design_filters.py            # defaults below
    python filters/design_filters.py --fs 104 --fir-taps 31 --fir-cutoff 9.4 --iir-cutoff 8

Writes (paths relative to the repository):
    filters/filters.json         every parameter and coefficient; read by
                                 filter_model.py, pc/imu_tool.py and the GHDL test
    filters/fir_lowpass.coe      FIR Compiler coefficient file (lab 6 flow; the
                                 same format MATLAB's Filter Designer exports)
    pl/hdl/iir_coeffs_pkg.vhd    IIR coefficients for iir_mc.vhd
    pl/vivado/imu_params.tcl     sampling rate, window length and IMU register
                                 values for dfx_project.tcl
    filters/filter_response.png  magnitude and group delay, float vs fixed point

The defaults are a starting point: both filters have their -3 dB point near
8 Hz, so FIR and IIR can be compared at the same bandwidth. The mini project
asks for the cutoff to be justified from training/validation data. Record raw
data first (pc/imu_tool.py stores raw and filtered samples), pick the cutoff,
run this script again and rebuild the bitstreams.
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
from scipy import signal

REPO = Path(__file__).resolve().parents[1]
FILTER_DIR = REPO / "filters"

# LSM6DSO32 register codes (datasheet DS13472, CTRL1_XL / CTRL2_G).
ODR_CODES = {26: 0b0010, 52: 0b0011, 104: 0b0100, 208: 0b0101, 416: 0b0110}
ACCEL_FS = {4: (0b00, 0.122), 8: (0b10, 0.244), 16: (0b11, 0.488), 32: (0b01, 0.976)}  # code, mg/LSB
GYRO_FS = {250: (0b00, 8.75), 500: (0b01, 17.5), 1000: (0b10, 35.0), 2000: (0b11, 70.0)}  # code, mdps/LSB

CHANNELS = ["a_x", "a_y", "a_z", "g_x", "g_y", "g_z"]

FIR_COEFF_BITS = 16
FIR_COEFF_FRAC = 15          # integer coefficients = round(h * 2^15), DC gain exactly 1
IIR_COEFF_BITS = 18          # DSP48E1 B port
IIR_COEFF_FRAC = 16          # Q2.16, range [-2, 2)
IIR_STATE_BITS = 25          # DSP48E1 A port (multiplier input)
IIR_STATE_FRAC = 8           # Q17.8: 6 dB of headroom above int16 inside the cascade


def metrics(b, a, fs, sos=None):
    """-3 dB point, attenuation and group delay of one filter."""
    w = np.linspace(0, fs / 2, 8193)[1:]
    if sos is not None:
        _, h = signal.sosfreqz(sos, worN=w, fs=fs)
    else:
        _, h = signal.freqz(b, a, worN=w, fs=fs)
    mag = 20 * np.log10(np.maximum(np.abs(h), 1e-12))
    f3 = float(w[np.argmax(mag < -3.0103)])
    att = {f"{f:g}Hz": round(float(np.interp(f, w, mag)), 2) for f in (12, 15, 20, 30) if f < fs / 2}
    num, den = (signal.sos2tf(sos) if sos is not None else (b, a))
    _, gd = signal.group_delay((num, den), w=np.array([0.5, 2.0, 5.0]), fs=fs)
    return {
        "f_3db_hz": round(f3, 3),
        "attenuation_db": att,
        "group_delay_samples": {f"{f:g}Hz": round(float(g), 3) for f, g in zip((0.5, 2, 5), gd)},
        "group_delay_ms": {f"{f:g}Hz": round(float(g) * 1000 / fs, 2) for f, g in zip((0.5, 2, 5), gd)},
    }


def design_fir(fs, taps, cutoff):
    h = signal.firwin(taps, cutoff, fs=fs, window="hamming")
    hq = np.round(h * (1 << FIR_COEFF_FRAC)).astype(np.int64)
    # Put the rounding error into the centre tap so the DC gain is exactly 1.
    hq[taps // 2] += (1 << FIR_COEFF_FRAC) - hq.sum()
    if np.abs(hq).max() >= 1 << (FIR_COEFF_BITS - 1):
        raise ValueError("FIR coefficient does not fit in 16 bits")
    # Worst-case accumulator: |x| <= 2^15 on every tap. The FIR Compiler sizes
    # its full-precision output from the coefficients; this is the same bound.
    acc_bits = 16 + math.ceil(math.log2(np.abs(hq).sum()))
    return h, hq, acc_bits


def design_iir(fs, order, cutoff):
    sos = signal.butter(order, cutoff, fs=fs, output="sos")
    # Low-Q section first, high-Q last (scipy already orders them this way),
    # and each section scaled to unity DC gain so no section boosts the signal
    # more than its own resonance.
    sos = sos[np.argsort([np.abs(np.roots(s[3:])).max() for s in sos])]
    sos_n = sos.copy()
    for s in sos_n:
        s[:3] *= s[3:].sum() / s[:3].sum()
    scale = 1 << IIR_COEFF_FRAC
    q = []
    for b0, b1, b2, _, a1, a2 in sos_n:
        q.append([int(round(v * scale)) for v in (b0, b1, b2, -a1, -a2)])
    q = np.array(q, dtype=np.int64)
    lim = 1 << (IIR_COEFF_BITS - 1)
    if np.abs(q).max() >= lim:
        raise ValueError("IIR coefficient does not fit in Q2.16")
    sos_q = np.column_stack([q[:, :3] / scale, np.ones(len(q)), -q[:, 3:] / scale])
    # Stability of the quantized filter.
    radii = [float(np.abs(np.roots(s[3:])).max()) for s in sos_q]
    if max(radii) >= 1.0:
        raise ValueError(f"quantized IIR is unstable (pole radius {max(radii)})")
    # Headroom: the L1 norm of the impulse response up to each section output
    # bounds that output for any input. Q17.8 holds +-2^16, the input is +-2^15.
    imp = np.zeros(4000)
    imp[0] = 1
    l1 = [float(np.abs(signal.sosfilt(sos_q[: k + 1], imp)).sum()) for k in range(len(sos_q))]
    if max(l1) >= 2.0:
        raise ValueError(f"IIR could overflow its Q17.8 state (L1 norms {l1})")
    return sos, sos_n, q, sos_q, radii, l1


def write_coe(path, hq, args):
    lines = [
        "; FIR low-pass for the IMU, generated by filters/design_filters.py",
        f"; fs = {args.fs} Hz, {len(hq)} taps, Hamming window, 6 dB cutoff {args.fir_cutoff} Hz",
        f"; integer coefficients, scale 2^{FIR_COEFF_FRAC} (sum = {int(hq.sum())})",
        "Radix = 10;",
        f"Coefficient_Width = {FIR_COEFF_BITS};",
        "CoefData = " + ",\n".join(str(int(v)) for v in hq) + ";",
    ]
    path.write_text("\n".join(lines) + "\n", newline="\n")


def write_vhdl_pkg(path, q, args):
    rows = "\n".join("        " + ", ".join(f"{int(v):7d}" for v in s)
                     + ("," if i < len(q) - 1 else " ") + f"   -- section {i}"
                     for i, s in enumerate(q))
    path.write_text(f"""-- IIR coefficients for iir_mc.vhd. Generated by filters/design_filters.py,
-- do not edit by hand: change the script arguments and run it again.
--
-- Butterworth low-pass, order {args.iir_order}, -3 dB at {args.iir_cutoff} Hz, fs = {args.fs} Hz.
-- One row per biquad: b0, b1, b2, -a1, -a2 in Q2.{IIR_COEFF_FRAC} ({IIR_COEFF_BITS}-bit signed).
-- Each section has unity DC gain; the low-Q section comes first.
library ieee;
use ieee.std_logic_1164.all;

package iir_coeffs_pkg is
    constant IIR_SECTIONS   : positive := {len(q)};
    constant IIR_COEFF_BITS : positive := {IIR_COEFF_BITS};
    constant IIR_COEFF_FRAC : natural  := {IIR_COEFF_FRAC};
    constant IIR_STATE_BITS : positive := {IIR_STATE_BITS};
    constant IIR_STATE_FRAC : natural  := {IIR_STATE_FRAC};

    type iir_coeff_array is array (natural range <>) of integer;
    constant IIR_COEFFS : iir_coeff_array(0 to 5 * IIR_SECTIONS - 1) := (
{rows}
    );
end package iir_coeffs_pkg;
""", newline="\n")


def write_tcl(path, d):
    imu = d["imu"]
    path.write_text(f"""# Generated by filters/design_filters.py. Do not edit: run the script again.
# Read by dfx_project.tcl.
set imu_fs_hz          {imu['odr_hz']}
set imu_window_s       {d['window_s']}
set imu_n_samples      {d['n_samples']}
set imu_ctrl1_xl       {imu['ctrl1_xl']}
set imu_ctrl2_g        {imu['ctrl2_g']}
set fir_coe_file       [file join $repo_dir filters fir_lowpass.coe]
set fir_taps           {d['fir']['taps']}
set fir_coeff_frac     {d['fir']['coeff_frac_bits']}
set fir_acc_bits_est   {d['fir']['acc_bits']}
""", newline="\n")


def plot(path, d, fs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    w = np.linspace(0.01, 0.999 * fs / 2, 2048)
    hf = np.asarray(d["fir"]["coeffs_float"])
    hq = np.asarray(d["fir"]["coeffs_int"]) / (1 << FIR_COEFF_FRAC)
    sos_f = np.asarray(d["iir"]["sos_float"])
    sos_q = np.asarray(d["iir"]["sos_quantized"])
    curves = [
        ("FIR float", signal.freqz(hf, 1, worN=w, fs=fs)[1], (hf, [1.0]), "C0", "-"),
        ("FIR 16-bit", signal.freqz(hq, 1, worN=w, fs=fs)[1], (hq, [1.0]), "C0", "--"),
        ("IIR float", signal.sosfreqz(sos_f, worN=w, fs=fs)[1], signal.sos2tf(sos_f), "C1", "-"),
        ("IIR Q2.16", signal.sosfreqz(sos_q, worN=w, fs=fs)[1], signal.sos2tf(sos_q), "C1", "--"),
    ]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7, 6), sharex=True)
    for name, h, (b, a), color, style in curves:
        ax1.plot(w, 20 * np.log10(np.maximum(np.abs(h), 1e-9)), color=color, ls=style, label=name)
        gd = signal.group_delay((b, a), w=w, fs=fs)[1]
        ax2.plot(w, gd * 1000 / fs, color=color, ls=style, label=name)
    ax1.set_ylim(-90, 5)
    ax1.set_ylabel("Magnitude (dB)")
    ax1.grid(True, alpha=0.3)
    ax1.legend()
    ax2.set_ylabel("Group delay (ms)")
    ax2.set_xlabel("Frequency (Hz)")
    ax2.set_ylim(0, 250)
    ax2.grid(True, alpha=0.3)
    fig.suptitle(f"IMU low-pass filters, fs = {fs} Hz")
    fig.tight_layout()
    fig.savefig(path, dpi=130)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fs", type=int, default=104, choices=sorted(ODR_CODES),
                   help="IMU output data rate in Hz (also the filter sampling rate)")
    p.add_argument("--window-s", type=float, default=2.0, help="window length in seconds")
    # The LSM6DSO32 on the test board runs at 97.6 Hz instead of the nominal
    # 104 Hz (measured on 26 Nov 2026 against the PC clock). 200 samples are
    # about 2 s at that rate. 0 = round(fs * window_s).
    p.add_argument("--n-samples", type=int, default=200,
                   help="window length in samples (default 200; 0 = round(fs * window_s))")
    p.add_argument("--accel-fs-g", type=int, default=4, choices=sorted(ACCEL_FS))
    p.add_argument("--gyro-fs-dps", type=int, default=500, choices=sorted(GYRO_FS))
    p.add_argument("--fir-taps", type=int, default=31)
    p.add_argument("--fir-cutoff", type=float, default=9.4, help="6 dB point of the windowed FIR, Hz")
    p.add_argument("--iir-order", type=int, default=4, choices=(2, 4, 6))
    p.add_argument("--iir-cutoff", type=float, default=8.0, help="-3 dB point of the Butterworth, Hz")
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args()
    if args.fir_taps % 2 == 0:
        p.error("--fir-taps must be odd (type I linear phase, integer delay)")
    n = args.n_samples if args.n_samples > 0 else round(args.fs * args.window_s)
    if not 1 <= n <= 512:
        p.error("the window must be 1..512 samples (IMU_MAX_SAMPLES in ps/ps.h)")

    fs = args.fs
    h, hq, acc_bits = design_fir(fs, args.fir_taps, args.fir_cutoff)
    sos, sos_n, q, sos_q, radii, l1 = design_iir(fs, args.iir_order, args.iir_cutoff)
    odr = ODR_CODES[fs]
    xl_code, xl_mg = ACCEL_FS[args.accel_fs_g]
    g_code, g_mdps = GYRO_FS[args.gyro_fs_dps]

    d = {
        "generated_by": "filters/design_filters.py",
        "args": vars(args),
        "fs_hz": fs,
        "window_s": args.window_s,
        "n_samples": args.n_samples if args.n_samples > 0 else int(round(fs * args.window_s)),
        "channels": CHANNELS,
        "imu": {
            "sensor": "LSM6DSO32",
            "odr_hz": fs,
            "ctrl1_xl": f"0x{(odr << 4) | (xl_code << 2):02X}",
            "ctrl2_g": f"0x{(odr << 4) | (g_code << 2):02X}",
            "ctrl3_c": "0x44",
            "accel_range_g": args.accel_fs_g,
            "accel_mg_per_lsb": xl_mg,
            "gyro_range_dps": args.gyro_fs_dps,
            "gyro_mdps_per_lsb": g_mdps,
        },
        "fir": {
            "type": "lowpass, Hamming window",
            "taps": args.fir_taps,
            "cutoff_6db_hz": args.fir_cutoff,
            "coeffs_float": [float(v) for v in h],
            "coeffs_int": [int(v) for v in hq],
            "coeff_bits": FIR_COEFF_BITS,
            "coeff_frac_bits": FIR_COEFF_FRAC,
            "acc_bits": acc_bits,
            "delay_samples": (args.fir_taps - 1) / 2,
            "float": metrics(h, [1.0], fs),
            "quantized": metrics(hq / (1 << FIR_COEFF_FRAC), [1.0], fs),
        },
        "iir": {
            "type": f"Butterworth lowpass, order {args.iir_order}, direct form I biquads",
            "order": args.iir_order,
            "cutoff_3db_hz": args.iir_cutoff,
            "sos_float": sos.tolist(),
            "sos_quantized": sos_q.tolist(),
            "sections_int": q.tolist(),
            "sections_int_order": ["b0", "b1", "b2", "-a1", "-a2"],
            "coeff_bits": IIR_COEFF_BITS,
            "coeff_frac_bits": IIR_COEFF_FRAC,
            "state_bits": IIR_STATE_BITS,
            "state_frac_bits": IIR_STATE_FRAC,
            "pole_radius_quantized": radii,
            "l1_norm_per_section": l1,
            "float": metrics(None, None, fs, sos=sos),
            "quantized": metrics(None, None, fs, sos=sos_q),
        },
    }

    FILTER_DIR.mkdir(exist_ok=True)
    (FILTER_DIR / "filters.json").write_text(json.dumps(d, indent=2) + "\n", newline="\n")
    write_coe(FILTER_DIR / "fir_lowpass.coe", hq, args)
    write_vhdl_pkg(REPO / "pl" / "hdl" / "iir_coeffs_pkg.vhd", q, args)
    write_tcl(REPO / "pl" / "vivado" / "imu_params.tcl", d)
    if not args.no_plot:
        plot(FILTER_DIR / "filter_response.png", d, fs)

    print(f"fs {fs} Hz, window {d['n_samples']} samples, "
          f"CTRL1_XL {d['imu']['ctrl1_xl']}, CTRL2_G {d['imu']['ctrl2_g']}")
    for k in ("fir", "iir"):
        m = d[k]["quantized"]
        print(f"{k.upper()}: -3 dB {m['f_3db_hz']} Hz, attenuation {m['attenuation_db']}, "
              f"delay {m['group_delay_ms']} ms")
    print(f"FIR accumulator {acc_bits} bits; IIR pole radius {max(radii):.4f}, L1 norms {[round(v, 3) for v in l1]}")


if __name__ == "__main__":
    main()
