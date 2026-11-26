# filters

FIR and IIR low-pass filters for the IMU, used in the static PL (`pl/hdl/`, FIR Compiler in `pl/vivado/dfx_project.tcl`).

| File | Use |
|---|---|
| `design_filters.py` | Designs both filters, quantizes them and writes every file below. Options: sampling rate, window length (`--n-samples`, default 200), sensor ranges, FIR taps/cutoff, IIR order/cutoff (`--help`). |
| `filter_model.py` | Bit-exact fixed-point models of both PL filters (`fir_q`, `iir_q`, `apply`) and floating-point references. Used by the GHDL check, the PS host test and `pc/imu_tool.py check`, and to build raw/FIR/IIR training sets offline from raw recordings. |
| `filters.json` | All parameters, float and quantized coefficients, -3 dB point, attenuation, group delay, IIR pole radii and overflow bounds |
| `fir_lowpass.coe` | FIR Compiler coefficients, same format as MATLAB's Filter Designer export (lab 6) |
| `filter_response.png` | Magnitude and group delay, float vs fixed point |

Also generated: `../pl/hdl/iir_coeffs_pkg.vhd` and `../pl/vivado/imu_params.tcl`. After changing a filter, run the script, rebuild both bitstreams and re-run `pl/sim/imu/run_ghdl.sh`.

Fixed point: FIR coefficients 16 bit, scale 2^15, DC gain exactly 1, full-precision accumulator rounded to int16 in `imu_capture.vhd`. IIR: Q2.16 coefficients (18 bit, the DSP48's B port), Q17.8 state (25 bit, A port), sections scaled to unity DC gain with the low-Q section first. The design script refuses coefficients that do not fit, an unstable quantized IIR, and an IIR whose state could overflow (L1 norm ≥ 2).

Current design (fs = 104 Hz, the nominal sensor rate): both −3 dB at 8 Hz. The sensor on the test board runs at 97.6 Hz, so there the −3 dB points are near 7.5 Hz. FIR 31 taps, −46 dB at 15 Hz, 144 ms delay (linear phase). IIR 4th-order Butterworth, −24 dB at 15 Hz, 51–65 ms delay over 0.5–5 Hz.
