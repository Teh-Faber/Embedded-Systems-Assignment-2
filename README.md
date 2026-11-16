# Embedded Systems - Assignment 2

Shape recognition on the PYNQ-Z2 with a CNN and an SNN accelerator.
PC sends an image over UART, the PS runs it through the HLS IP and sends the result back.

Deadline: 23.11.2026 23:59. Assignment text is in `docs/assignment_2.pdf`.

## Layout

| Folder | What is in it |
|---|---|
| `cnn/` | DS-CNN training scripts, HLS source, testbench, weights and `hls_config.cfg` |
| `snn/` | SNN MLP training script, HLS source, testbench, weights and `hls_config.cfg`. `lab_app/` is the PS app and PC script from lab 8 |
| `ps/` | Bare-metal C for the Zynq PS: UART framing, main loop, DFX over PCAP, UART test app (see `ps/README.md`) |
| `pc/` | Python scripts that send images and compare results |
| `pl/` | Exported IP, Vivado project, Vitis workspace, constraints (see `pl/README.md`) |
| `data/` | Datasets. Not in git, see below |
| `docs/` | Assignment, system and UART diagrams, protocol notes, lecture guides |

## Where to start

- System overview and UART frames: `docs/system-og-uart-diagrammer.md`
- Building the PS apps and running the PC scripts: `docs/uart_integration.md`
- DFX design and what to do once the HLS IPs exist: `pl/ip/README.md`, then `pl/README.md`, then `ps/README.md`
- HLS / Vivado / Vitis steps from the lectures: `docs/lectures/cnn/README.md` and `docs/lectures/snn/README.md`

PC side:

```
cd pc
py -m pip install -r requirements.txt
py send_images.py --port COM3 --dataset <path to shapes dataset> --models cnn --output cnn.jsonl
```

## State of things

Nothing has been re-tested after the files were moved here.

- Both networks still use the weights from the labs, trained on the cable dataset (Full Cable, Left End, Right End, No Cable). They need retraining on shapes.
- Images are 80x60 grayscale, 4800 bytes per frame. The SNN uses 384 features (12x16 grid), T=1.
- `ps/ps_main.c` drives both models through one reconfigurable partition and swaps them over PCAP (`ps/ps_dfx.c`). The static PL is defined by `pl/vivado/dfx_project.tcl`. None of this has been built or run yet, see the status sections in `pl/README.md` and `ps/README.md`.
- The PS gives both IPs the same Q8.8 image buffer. The SNN IP still expects 384 features from the PC, as in `snn/lab_app/helloworld.c`. `snn/lab_app/README.md` lists what has to move into the SNN IP so both setups match.
- `snn/` is our lab 8 version, not the original from the lecture repo. It has the rounding fix in `fc1` in `snn_mlp.cpp`.
- The lecture datasets are not in the repo (about 400k files each). Get them from the lecture repos if needed.

## Paths

No absolute paths. Scripts and testbenches find things relative to their own location, so the repo can be cloned anywhere.

Datasets go in `data/` in the repo root, with the folder names the code expects:

| Folder | Used by |
|---|---|
| `data/new_training_data/` | `snn/snn_mlp.py` |
| `data/new_training_data_aug/` | `cnn/train_cnn.py` |
| `data/new_testing_data/` | `cnn/augment_data.py` (writes `new_testing_data_aug/`) |
| `data/dwpw_cnn_testing_data_aug_txt/` | `cnn/ds_cnn_tb.cpp` |
| `data/fpga_test_features/` | `snn/snn_mlp_tb.cpp` |

Training output is written to `cnn/trained_model/` and `snn/snn_mlp_model/`.
The serial port is still set in the scripts or given with `--port`.

## To do

- [ ] Shapes dataset with fixed train/val/test split (circle, figure eight, square, triangle)
- [ ] Retrain CNN, export weights, check HLS csim against Python
- [ ] Retrain SNN, export weights, check HLS csim against Python
- [ ] Vivado design for each IP, decide memory arrangement for the SNN input
- [ ] SNN front end in the HLS IP (image in, same interface as the CNN)
- [ ] DFX: first Vivado build of `pl/vivado/dfx_project.tcl`, first run of the PS on the board
- [ ] Benchmark runs: accuracy, latency, throughput, UART time, utilization, power
- [ ] Report (3 pages) and video (15 min)

## Sources

- https://github.com/dbaniya/CNN_guest_lecture
- https://github.com/dbaniya/SNN_guest_lecture
