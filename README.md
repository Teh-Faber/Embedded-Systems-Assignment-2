# Embedded Systems - Final Assignment: IMU gesture recognition

PYNQ-Z2 with an LSM6DSO32 IMU on PMOD JB. The static PL reads the IMU, filters the samples (FIR or IIR) and keeps the newest window in a BRAM. The PS streams the samples to the PC for training data. The PS also runs debug inference: the PC sends a window, the accelerator in the reconfigurable partition classifies it and the PS sends the result back.

The design uses the work of assignment 1 (BRAM sequence lock), lab 6 (IMU, FIR Compiler) and assignment 2 (DFX with a CNN and an SNN in one partition).

## Layout

| Folder | Content |
|---|---|
| `filters/` | Filter design (`design_filters.py`) and the bit-exact fixed-point model (`filter_model.py`) |
| `pl/` | RTL, Vivado scripts, constraints, HLS IP for the partition, GHDL testbench (see `pl/README.md`) |
| `ps/` | Bare-metal C for the PS and a host test (see `ps/README.md`) |
| `app_component/` | Vitis application component. It uses the sources in `ps/` |
| `pc/` | `imu_tool.py`: board status, recording, filter check, debug inference. `imu_path.py`: 3D path view of recorded takes |
| `docs/` | `imu_system.md`: system, BRAM layout, UART protocol, test results |
| `data/` | Recordings from `imu_tool.py`. Not in git |

## Where to start

- From clone to recordings: `build_guide.md`
- System, protocol and test results: `docs/imu_system.md`
- PL and DFX: `pl/README.md`. PS: `ps/README.md`. HLS models: `pl/ip/README.md`

## Status (26 Nov 2026)

- Both static designs (FIR and IIR) are built and tested on the board. The hardware filters are bit-exact with `filters/filter_model.py`. Streams of 60 s have no lost samples.
- The sensor gives 97.6 Hz (nominal 104 Hz). The window has 200 samples (about 2 s).
- The partition holds the image CNN and SNN of assignment 2 (`pl/ip/ds_cnn`, `pl/ip/snn_mlp`). They are temporary placeholders until the IMU models exist.

## To do

- [ ] Record training data (`imu_tool.py record`)
- [ ] Choose the filter cutoff from the data, run `design_filters.py` again and rebuild
- [ ] Move the partition to clock regions X1Y0..X1Y2 (`pl/constraints/dfx_pblock.xdc`) and rebuild
- [ ] IMU CNN and SNN in HLS, export to `pl/ip/`, rebuild (`pl/ip/README.md`)
- [ ] Live inference with SW1 high (`ps/ps_imu_main.c`)
- [ ] Debug inference on the board
- [ ] Report and video

## Paths

The repo has no absolute paths. Scripts and testbenches find files relative to their own location, so you can clone the repo to any folder. Exception: `app_component/src/app.yaml` holds the absolute path of the Vitis platform on the computer that built it last (see `build_guide.md`, step 5).
