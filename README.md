# Embedded Systems - Assignment 2

Shape recognition on the PYNQ-Z2 with a CNN and an SNN accelerator.
PC sends an image over UART, the PS runs it through the HLS IP and sends the result back.

Deadline: 23.11.2026 23:59. Assignment text is in `docs/assignment_2.pdf`.

## Layout

| Folder | What is in it |
|---|---|
| `cnn/` | DS-CNN training scripts, HLS source, testbench, weights and `hls_config.cfg` |
| `snn/` | SNN MLP training script, HLS source, testbench, weights and `hls_config.cfg`. `lab_app/` is the PS app and PC script from lab 8 |
| `ps/` | Bare-metal C for the Zynq PS: UART framing, main loop, UART test app |
| `pc/` | Python scripts that send images and compare results |
| `pl/` | Exported IP, Vivado project, Vitis workspace, constraints (see `pl/README.md`) |
| `data/` | Datasets. Not in git, see below |
| `docs/` | Assignment, system and UART diagrams, protocol notes, lecture guides |

## Where to start

- System overview and UART frames: `docs/system-og-uart-diagrammer.md`
- Building the PS apps and running the PC scripts: `docs/uart_integration.md`
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
- `ps/ps_main.c` only drives the CNN. SNN and DFX hook in at `select_model()`. `snn/lab_app/helloworld.c` shows how the PS drives the SNN IP (features into BRAM, start, read result). `snn/lab_app/README.md` lists what has to move into the SNN IP so both setups match.
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
- [ ] SNN path in `ps_main.c`
- [ ] DFX
- [ ] Benchmark runs: accuracy, latency, throughput, UART time, utilization, power
- [ ] Report (3 pages) and video (15 min)

## Sources

- https://github.com/dbaniya/CNN_guest_lecture
- https://github.com/dbaniya/SNN_guest_lecture
