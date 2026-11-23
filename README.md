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

## From clone to running on the board

Tools: Vivado, Vitis and Vitis HLS 2026.1, Vivado ENTERPRISE licence (DFX), PYNQ-Z2 board files.
Commands run from the repo root.

1. **Dataset.** In a Python env with `numpy` and `opencv-python`:
   ```
   python tools/generate_shapes.py      # data/shapes_dataset/{train,val,test}
   python tools/export_test_txt.py      # data/shapes_test_txt/ for the HLS testbenches
   ```
2. **Training (only if a model changes).** Weights and exported IPs are in the repo.
   `cnn/train_cnn.py` and `snn/snn_mlp.py`, each in its own venv (`cnn/.venv`, `snn/.venv`).
   Then re-run the HLS components and export the IPs to `pl/ip/`.
3. **Vivado.** Open the Vivado GUI (batch mode doesn't find the board files) and in the Tcl Console:
   ```
   source <repo>/pl/vivado/dfx_project.tcl
   ```
   Then Generate Bitstream, and when both runs are done:
   ```
   source <repo>/pl/vivado/export_dfx.tcl
   ```
   This writes `ps/bitstreams/{cnn,snn}_partial.bin` and `pl/vivado/dfx_static.xsa`.
4. **Vitis.** Use the repo root as workspace.
   - Create a platform component named `platform` from `pl/vivado/dfx_static.xsa` (standalone, `ps7_cortexa9_0`) and build it.
     After a new export: platform `vitis-comp.json` → Update XSA → build again.
   - Build `app_component` (already in the repo, uses the files in `ps/`).
     After a new export, run `touch ps/ps_bitstreams.S` first so the new `.bin` files get linked in.
   - Board in JTAG boot mode, then Run.
5. **PC.**
   ```
   cd pc
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python send_images.py                        # 1 image per class, both models
   python send_images.py --limit-per-class 500  # full test set
   ```
   Defaults (port, dataset, output folder) are at the top of `send_images.py`. On Windows set `DEFAULT_PORT` to e.g. `COM3`.
   Results go to `pc/results/`.

## State of things

- Both networks are trained on the shapes dataset (Circle, Figure Eight, Square, Triangle).
- Images are 80x60 grayscale, 4800 bytes per frame. The SNN uses 384 features (12x16 grid), T=1.
- DFX works on the board: CNN and SNN share one reconfigurable partition and are swapped over PCAP (`ps/ps_dfx.c`). A switch takes about 42 ms. Inference: CNN 1039 us, SNN 430 us.
- The PS gives both IPs the same Q8.8 image buffer. The SNN IP computes its 384 features itself (Sobel, bit-exact with `snn/snn_mlp.py`).
- The CNN is less accurate in HLS (46.5 % in csim) than in Python (58 %). Not yet investigated.
- `snn/` is our lab 8 version, not the original from the lecture repo. It has the rounding fix in `fc1` in `snn_mlp.cpp`.
- The lecture datasets are not in the repo (about 400k files each). Get them from the lecture repos if needed.

## Paths

No absolute paths. Scripts and testbenches find things relative to their own location, so the repo can be cloned anywhere.

Datasets go in `data/` in the repo root, with the folder names the code expects:

| Folder | Used by |
|---|---|
| `data/shapes_dataset/` | `cnn/train_cnn.py`, `snn/snn_mlp.py`, `pc/send_images.py` |
| `data/shapes_test_txt/` | `cnn/ds_cnn_tb.cpp`, `snn/snn_mlp_tb.cpp` |

Both are made by the scripts in `tools/` (step 1 above).
Training output is written to `cnn/trained_model/` and `snn/snn_mlp_model/`.
The serial port is set at the top of `pc/send_images.py` or given with `--port`.

## Sources

- https://github.com/dbaniya/CNN_guest_lecture
- https://github.com/dbaniya/SNN_guest_lecture
