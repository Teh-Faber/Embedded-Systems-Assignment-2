# IMU system: static PL, capture and debug inference

The static part of the DFX design reads the LSM6DSO32 on PMOD JB, filters the samples and keeps the newest window of filtered samples in a BRAM ring. The PS streams the samples to the PC (training data) or runs the accelerator on a window that the PC sends (debug inference). The CNN and SNN accelerators are in the reconfigurable partition (DFX, from assignment 2). Until the IMU models exist, the partition holds the image models of assignment 2 as temporary placeholders (`pl/ip/README.md`).

There are two static designs, identical except for the filter:

| Design | Filter | Built from |
|---|---|---|
| `dfx_fir` | Xilinx FIR Compiler, 31-tap low-pass (lab 6 flow, `.coe` file) | `filters/fir_lowpass.coe` |
| `dfx_iir` | `pl/hdl/iir_mc.vhd`, 4th-order Butterworth as two biquads (Vivado has no IIR IP) | `pl/hdl/iir_coeffs_pkg.vhd` |

Both filters are designed by `filters/design_filters.py` and have their −3 dB point at 8 Hz, so they can be compared at the same bandwidth: the FIR has linear phase and 144 ms delay, the IIR about 52 ms with non-linear phase. These are starting values. The mini project asks for the cutoff to be justified from training/validation data: record first (raw data is always stored), then choose, run the script again and rebuild.

## Block diagram

```
 PMOD JB          static PL (100 MHz, FCLK0)                                          PS
 ┌────────┐  SPI  ┌────────────────────────────── imu_capture ─────────────┐
 │LSM6DSO32├──────►imu_reader ─sample─► serializer ─m_axis_raw─►┌────────┐  │
 └────────┘ 1 MHz │ config, WHO_AM_I,   (6 beats, tlast)        │ filter │  │
                  │ status poll, burst                          │FIR/IIR │  │
                  │ read, watchdog      collector ◄─s_axis_filt─┤        │  │
                  │      │ raw, ts        │ filtered            └────────┘  │
                  │      └───────► BRAM writer ◄──┘                         │
                  └──────────────────────┬──────────────────────────────────┘
                                         │ port B                 GP0
                                    imu_bram (16 KiB) ◄─ port A ─ imu_bram_ctrl ◄─ sc_ctrl M01 ◄─ ps7
                                                                  (0x4200_0000)
 SW1 ─► debounce ─► emio_io ─► EMIO 2 (pin 56) ─────────────────────────────────────────► ps7
                    emio_io ◄─ EMIO 1 (pin 55) capture clear ◄──────────────────────────── ps7
                    emio_io ◄─ EMIO 0 (pin 54) decouple ─► decoupler, rst_rp (assignment 2)
                                                         sc_ctrl M00 ─► decoupler ─► rp (CNN | SNN)
```

## Sampling and the window

| Item | Value | Where it is set |
|---|---|---|
| Sensor output data rate | 104 Hz nominal (CTRL1_XL = 0x40, CTRL2_G = 0x44); 97.6 Hz measured on the test board | `design_filters.py --fs` |
| Ranges | ±4 g (0.122 mg/LSB), ±500 dps (17.5 mdps/LSB) | `--accel-fs-g`, `--gyro-fs-dps` |
| Window | 200 samples (about 2 s at 97.6 Hz) | `--n-samples` (0: `--fs` × `--window-s`) |
| Channel order | a_x, a_y, a_z, g_x, g_y, g_z, int16 raw sensor units | fixed |
| Ring length | 201 slots (window + 1, see below) | derived |

The PL polls the sensor's STATUS_REG every 100 µs and reads the 12 data bytes in one burst when both accel and gyro have new data. Every sensor sample is read exactly once at the sensor's own rate, so there are no duplicates or drops when the sensor's clock and the FPGA's differ. The sensor's internal digital low-pass at ODR/2 is the anti-aliasing filter (lab 6, part 4), since nothing is decimated in the PL. With BDU set, accel and gyro of one sample belong together. The gyro range is ±500 dps instead of assignment 1's ±250, because fast wrist turns while drawing exceed 250 dps.

The sensor's own oscillator sets the sample rate. The LSM6DSO32 on the test board gives 97.6 Hz, not 104 Hz (measured on 26 Nov 2026 over 60 s against the PC clock; the PL timestamps agree with the PC clock to 0.01 %). No samples are lost: the interval is a constant 10.24 ms. Use the timestamps in the recordings when the real time matters. The filters are designed for 104 Hz, so on this sensor all their frequencies are 6 % lower (FIR and IIR −3 dB near 7.5 Hz).

`design_filters.py` writes `pl/vivado/imu_params.tcl`; `dfx_project.tcl` reads it, so the sampling rate, window length and filters can't get out of step.

## BRAM layout

The PL is the only writer. The PS reads through the AXI BRAM controller at `0x4200_0000`.

| Offset | Name | Content |
|---|---|---|
| 0x00 | MAGIC | `0x494D5531` ("IMU1") |
| 0x04 | LAYOUT | [7:0] version 1, [15:8] filter (0 none, 1 FIR, 2 IIR), [31:16] window length N |
| 0x08 | FORMAT | [15:0] sample rate (Hz), [23:16] channels (6), [31:24] bytes per record (12) |
| 0x0C | COUNT | samples since the last clear |
| 0x10 | STATUS | [0] sensor ok, [1] SW1, [2] decoupled, [15:8] sensor retries, [23:16] filter errors, [31:24] overruns |
| 0x14 | LAST_TS | timestamp of the newest sample, µs since the clear |
| 0x18 | SENSOR | [7:0] WHO_AM_I read (0x6C), [15:8] last STATUS_REG |
| 0x1C | SLOTS | ring length, N + 1 |
| 0x20, 0x24, 0x28 | FILT_OFF, RAW_OFF, TS_OFF | byte offsets of the filtered, raw and timestamp rings |
| 0x2C | MEM_BYTES | 16384 |

Sample k is in slot `k mod SLOTS` of each ring: 12 bytes (int16[6]) filtered, 12 bytes raw, 4 bytes timestamp. The PL writes all of a sample's words, then COUNT = k + 1. As in assignment 1, COUNT is the sequence lock: read COUNT, copy, read COUNT again. The ring has one slot more than the window, because while COUNT = c the PL may be writing sample c, and that slot is never one of the window's (samples c − N … c − 1). So a window copied with COUNT unchanged is always consistent; `ps_imu.c` does exactly this.

`capture clear` (EMIO 1) restarts COUNT, the timestamps, the ring position and the filter state, but leaves the sensor configured. The FIR Compiler keeps its sample history through its reset (seen on the board), so after each filter reset `imu_capture` sends taps − 1 = 30 zero samples through it and drops the results (generic `FILT_FLUSH`). Both filters then start from zero, as `filter_model.py` does. Recordings start with a clear, so the filter state never carries over from an unrelated recording, as the mini project requires.

## SW1 and the LEDs

| | |
|---|---|
| SW1 low | Debug mode: the PC sends a window, the PS copies it to DDR, runs the requested model(s) and returns the result. |
| SW1 high | Live mode: inference belongs to the filtered IMU windows. PC inference requests are refused (status 9). The live loop itself isn't written yet; it will call `imu_read_window()` (the same data the `window` command returns) and `accel_run()`. |
| LD0 | heartbeat, toggles every half second while samples arrive |
| LD1 | sensor configured and answering |
| LD2 | SW1 |
| LD3 | partition decoupled |

Samples are captured and can be streamed in both modes.

## Build

The steps from clone to recordings are in `../build_guide.md`. In short: `filters/design_filters.py` (only when the filters change), one Vivado project per filter (`dfx_project.tcl`, then `export_dfx.tcl`), a Vitis platform from `pl/vivado/dfx_static_<filter>.xsa` and `app_component` with `ps/bitstreams/<filter>` on its include path.

Connect the sensor as in lab 6 (PMOD JB top row: CS, SDO, SDA, SCL). The board's USB UART is the PS UART at 4 Mbaud.

## UART protocol

All multi-byte fields are little-endian.

| Part | Layout |
|---|---|
| Request | `A6 6A`, header, body |
| Reply | `A5`, header, body, `5A` |
| Header | `length:u16` (body only), `version:u8` (1), `type:u8`, `id:u32` |

A reply has the id of its request. Sample frames (type 19) count their own id.

| Type | Direction | Body | Notes |
|---|---|---|---|
| 16 IMU command | PC → PS | `cmd:u8 [, arg:u8]` | 0 info, 1 start streaming (arg 1: clear the capture first), 2 stop streaming, 3 clear, 4 window |
| 3 diagnostic | PS → PC | `status:u8` | acknowledges commands 1–3, or gives an error |
| 18 IMU info | PS → PC | `app_version:u8, filter:u8, n_samples:u16, slots:u16, odr_hz:u16, count:u32, status:u32, sensor:u32, last_ts_us:u32, sw1:u8, loaded_model:u8, imu_ready:u8, streaming:u8` | 28 bytes. `status` and `sensor` are the BRAM header words |
| 19 IMU samples | PS → PC | `first_index:u32, lost:u32, n:u8`, then n × `ts_us:u32, raw:i16[6], filt:i16[6]` | up to 16 records per frame. `lost` counts the samples that the PL overwrote before the PS could send them |
| 20 IMU window | PS → PC | `end_index:u32, n:u16, filt:i16[n][6]` | the newest n filtered samples, oldest first. `end_index` is the index after the newest |
| 17 IMU inference | PC → PS | `model_mask:u8, n_classes:u8, n_samples:u16, window:i16[n_samples][6]` | model mask 1 CNN, 2 SNN, 3 both. Refused with status 9 while SW1 is high |
| 4 DFX timing | PS → PC | `model_id:u8, dfx_us:u32` | before a result that needed a module switch |
| 21 IMU result | PS → PC | `model_id:u8, status:u8, class_id:u8, n_scores:u8, infer_us:u32, prep_us:u32, window_end:u32, scores:i32[n_scores]` | `prep_us` is the copy into DDR. `window_end` is 0xFFFFFFFF for a window from the PC |

Status codes: 0 ok, 1 invalid length, 2 invalid model mask, 3 model unavailable, 4 inference failed or timed out, 5 unsupported version, 6 unsupported message type, 7 truncated request, 9 SW1 is high (live mode), 10 IMU capture not ready (no capture BRAM in the bitstream, or fewer samples than one window), 11 unknown IMU command.

Channel order everywhere: a_x, a_y, a_z, g_x, g_y, g_z, int16 in raw sensor units. Timestamps are from the PL, in microseconds since the last capture clear.

## Record training data

```
cd pc
py imu_tool.py --port COM8 info
py imu_tool.py --port COM8 record --label circle --takes 20
py imu_tool.py --port COM8 record --label idle --takes 20 --auto
py imu_tool.py check ../data/imu/circle
```

Use the COM port of the board on your PC (COM8 on the test PC). `record` clears the capture and discards a warm-up of `--warmup` seconds (default 1, the filter transient). Then, for each take:

| Step | Key |
|---|---|
| Start the take | Enter. With `--auto` a 3-2-1 countdown starts by itself |
| Perform the shape | the take is `--seconds` long (default 2.5) |
| Save the take | Enter |
| Record the take again | Space |
| Stop the run | q. The takes that are saved stay |

`--takes` is the number of takes to save. All durations are real time, measured with the PL timestamps. At 97.6 Hz a take of 2.5 s has about 245 samples. If a take lost samples on the way to the PC, the tool says so; record that take again.

Files, in `data/imu/<label>/`:

- `<label>_NNN.csv`: one saved take: index, PL timestamp (µs), raw a_x…g_z, filtered a_x…g_z. A new take gets the lowest free number, so the numbers of deleted takes are used again.
- `runs/<date>_<time>/stream.csv`: every sample of one run since the clear, also the takes that were recorded again
- `runs/<date>_<time>/run.json`: label, nominal and measured sample rate, filter and its coefficients, sensor ranges and units, the saved takes (file, first index, samples), the discarded takes, lost samples

The whole stream is kept for two reasons. `check` recomputes the filter from the raw columns with `filters/filter_model.py` and confirms the board is bit-exact (`check data/imu/<label>` checks all runs of the shape). And the raw columns let you rebuild raw/FIR/IIR training sets offline from one recording (`filter_model.apply()`), whichever design was on the board while recording. Split train/validation/test by run before windowing, as the mini project asks.

To inspect the takes of a shape, `py imu_path.py ../data/imu/square --open` writes `data/imu/square/path.html`: the 3D path of each take (gyro and accelerometer integrated over the motion of the take, from rest to rest), with a plane view and an overlay of all takes. The view "closed shape" also makes the path end where it starts. This removes the drift from an accelerometer offset while the board turns, which bends the sides; use it only for closed shapes. The page starts with the open view; switch with the checkbox, or start closed with `--view closed`. The info line gives the end-to-start gap without this correction. To remove a bad take, delete its CSV file; the view leaves it out. The paths drift, so use them to find bad takes (wrong shape, shape cut off by the end of the take), not as measurements.

`stream --seconds 60 --out file.csv` records 60 s in one file, for example to choose the cutoff from the noise spectrum.

## Debug inference

```
py imu_tool.py --port COM8 window --out w.csv                          # newest filtered window
py imu_tool.py --port COM8 infer --input w.csv --models both --classes 5
py imu_tool.py --port COM8 infer --models cnn                          # the board's current window
```

The window goes to the accelerator byte for byte as int16[N][6], oldest first. Replaying a window taken with `window` checks the hardware model against its software reference on identical inputs.

## Interface for the IMU models (HLS)

Keep the RP boundary of assignment 2 (`pl/ip/README.md`), so both models fit the same partition and the PS needs no changes:

```cpp
void imu_cnn(const ap_int<16> *input,      // int16[200][6], a_x a_y a_z g_x g_y g_z, oldest first
             int *pred_out,                // class index
             data_t logits_out[N_CLASSES]) // one score per class
{
#pragma HLS INTERFACE m_axi     port=input offset=slave bundle=gmem depth=1248
#pragma HLS INTERFACE s_axilite port=input      bundle=control
#pragma HLS INTERFACE s_axilite port=pred_out   bundle=control
#pragma HLS INTERFACE s_axilite port=logits_out bundle=control
#pragma HLS INTERFACE s_axilite port=return     bundle=control
```

- The input is the filtered window in raw sensor units. Put the normalization (per-channel mean and scale from the training set) and, for the SNN, the spike encoding inside the IP. The live path then passes the BRAM window on unchanged, and the debug path tests exactly the same thing.
- After export, compare `x<top>_hw.h` with `model_registers` in `ps/ps_accel.c` (input pointer, prediction and logits offsets, logit width). The logits offset can change with the number of classes, because HLS aligns arrays to their size.
- Read-only `m_axi` (`const`) in both models, otherwise the partition boundaries differ.
- Build with `set modules {rm_cnn imu_cnn rm_snn imu_snn}` before sourcing `dfx_project.tcl`.

## Verification done without the board

| What | How | Result |
|---|---|---|
| Filter design | `design_filters.py`: quantized IIR poles inside the unit circle (radius 0.836), L1 norm of every section below 2 (no overflow of the Q17.8 state), FIR DC gain exactly 1 | pass |
| PL capture and both filters | `pl/sim/imu/run_ghdl.sh`: LSM6DSO32 SPI model (mode 3, register file, BDU, data-ready flags), FIR Compiler stand-in with back-pressure that keeps its history through reset like the real core, the real `iir_mc`. Ring wraps, capture clear, sensor unplugged and plugged in, SW1. Every committed sample checked bit-exact against `filter_model.py`; no skipped or repeated sensor samples | pass for FIR, IIR and no filter, N = 16 and N = 200 |
| PS and PC | `ps/host_test/run_host_test.py`: the real PS sources built on the PC against stub drivers, a PL stand-in at 97.64 Hz and a fake accelerator, driven by `imu_tool.py` | 23 checks pass |
| Vivado scripts | run in `tclsh` with every Vivado command stubbed, both filters and the no-IP case | no Tcl errors |

## Verification on the board (26 Nov 2026)

PYNQ-Z2 with the LSM6DSO32 on PMOD JB, `imu_tool.py --port COM8`, board flat on the desk.

| What | Result |
|---|---|
| Vivado build of both designs | timing met. FIR: WNS +1.14 ns (CNN configuration), +0.42 ns (SNN). IIR: +1.28 ns, +0.88 ns |
| Sensor (`info`) | WHO_AM_I 0x6C, sensor ok, 0 retries, 0 filter errors, 0 overruns |
| Sample rate | 97.6 Hz, see "Sampling and the window" |
| Stream after a clear (`stream`) | FIR and IIR: 1040 samples (10.7 s) and 6240 samples (63.9 s), no samples lost |
| Hardware filter (`check`) | FIR and IIR: every filtered value equal to `filter_model.py`, from the first sample after the clear |
| Window (`window`) | 200 samples, oldest first |

Faults found on the board, and the fixes:

1. The FIR Compiler keeps its sample history through its reset, so the first 30 values after a clear did not agree with the model. Fix: `FILT_FLUSH` in `imu_capture.vhd`.
2. The IIR design missed timing by 0.1 ns: two roundings and a saturation in one clock cycle in `iir_mc.vhd`. Fix: the int16 output has its own cycle (`S_ROUND`).
3. A PS read of COUNT in the same clock cycle as the PL write of COUNT returned invalid data (both BRAM ports are WRITE_FIRST). The stream then started again from an old index. Fix: `rd_hdr()` in `ps_imu.c` reads a header word until two reads agree.

Not tested on the board yet: the PC → PS → accelerator debug inference (`imu_tool.py infer`, SW1 low) and the IMU models.
