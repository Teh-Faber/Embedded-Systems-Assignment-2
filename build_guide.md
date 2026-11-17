# Build guide: from clone to results on the PYNQ-Z2

All commands run from the repo root unless a step says otherwise.
Tested on Ubuntu 24.04 with Vivado / Vitis / Vitis HLS **2026.1**.

---

## 0. One-time setup

**Licence.** DFX needs a Vivado **ENTERPRISE** licence; the free BASIC one stops with `[Vivado 12-29795] ... does not cover DFX`.
Put the `.lic` in `~/.Xilinx/` and move any BASIC licence out of that folder:
```bash
mkdir -p ~/xilinx_lic_backup && mv ~/.Xilinx/Xilinx.lic ~/xilinx_lic_backup/   # only if a BASIC licence is there
```
Vivado's first log line must say `ENTERPRISE license has been detected`.

**Board files.** PYNQ-Z2 board files must be installed (Vivado → Xilinx Store, or copy to `<Vivado>/data/boards/board_files/pynq-z2`).

**Serial port access** (once, then log out and in):
```bash
sudo usermod -aG dialout $USER
```

**Tools in the shell:**
```bash
source /tools/2026.1/Vitis/settings64.sh    # adjust to your install path
```

---

## 1. Clone

```bash
git clone git@github.com:Teh-Faber/Embedded-Systems-Assignment-2.git
cd Embedded-Systems-Assignment-2
```

---

## 2. Python environments

One venv per folder (TensorFlow and PyTorch need different NumPy versions).
```bash
python3 -m venv cnn/.venv && cnn/.venv/bin/pip install -r cnn/requirements.txt
python3 -m venv snn/.venv && snn/.venv/bin/pip install -r snn/requirements.txt
python3 -m venv pc/.venv  && pc/.venv/bin/pip install -r pc/requirements.txt
```

---

## 3. Dataset

`data/` is not in git. Generate it (2000/500/500 images per class, seed 42):
```bash
cnn/.venv/bin/python tools/generate_shapes.py     # -> data/shapes_dataset/{train,val,test}
cnn/.venv/bin/python tools/export_test_txt.py     # -> data/shapes_test_txt/ (HLS testbenches)
```

---

## 4. Training (only when a model changes)

Weights and exported IPs are already in the repo. Skip to step 6 if nothing changed.

```bash
# CNN (EPOCHS in train_cnn.py; export is rounded to Q8.8 automatically)
cnn/.venv/bin/python cnn/train_cnn.py
cp cnn/trained_model/weights_cstyle_folded/*.txt cnn/weights_cstyle_folded/

# SNN (keeps the epoch with the best validation accuracy)
snn/.venv/bin/python snn/snn_mlp.py
cp snn/snn_mlp_model/models/fpga_export/fpga_weights.h snn/fpga_weights.h
```
Results: `cnn/trained_model/reports/`, `snn/snn_mlp_model/reports/test_results.txt`.

---

## 5. HLS (only when a model changes)

Vitis GUI: `File → Open Workspace` → repo root. The two HLS components use `cnn/hls_config.cfg` and `snn/hls_config.cfg`.

For **each** component, in the Flow panel:
1. **C Simulation**: accuracy should be close to Python (CNN ≈ 83 %, SNN ≈ 80 %).
2. **C Synthesis**: keep the defaults (part `xc7z020clg400-1`; CNN 8 ns, SNN 10 ns). Estimated clock must be < 10 ns.
3. **Package**: writes `cnn/build/ds_cnn.zip` and `snn/build/snn_mlp.zip`.

Put the IPs where Vivado looks for them:
```bash
rm -rf pl/ip/ds_cnn  && unzip -q cnn/build/ds_cnn.zip  -d pl/ip/ds_cnn
rm -rf pl/ip/snn_mlp && unzip -q snn/build/snn_mlp.zip -d pl/ip/snn_mlp
ls pl/ip/ds_cnn/component.xml pl/ip/snn_mlp/component.xml    # both must exist
```

---

## 6. Vivado: DFX build

Use the **GUI** (batch mode does not find the board files). If a project is open: `File → Close Project`.
In the Tcl Console (absolute paths, the console runs in `~/.cache/vivado`):
```tcl
source /path/to/Embedded-Systems-Assignment-2/pl/vivado/dfx_project.tcl
launch_runs impl_1 child_0_impl_1 -to_step write_bitstream -jobs 8
wait_on_run child_0_impl_1
source /path/to/Embedded-Systems-Assignment-2/pl/vivado/export_dfx.tcl
```
Check:
- `dfx_project.tcl` prints `DFX Aperture DRC completed with 0 error(s), 0 critical warning(s)` and `DFX: project ready`.
- `export_dfx.tcl` ends with `DFX: partial bitstreams written for: cnn snn`.

Timing (1st value WNS and 5th value WHS must be ≥ 0):
```bash
for f in pl/vivado/dfx/dfx.runs/{impl_1,child_0_impl_1}/dfx_static_wrapper_timing_summary_routed.rpt; do
    grep -A6 "Design Timing Summary" "$f" | tail -1
done
```
Outputs: `ps/bitstreams/{cnn,snn}_partial.bin` and `pl/vivado/dfx_static.xsa`.

Harmless warnings: `BD 41-3281` (decoupler/ps7 between SmartConnects), `PSU-1..4` (DDR preset), `dfx_decoupler-1`, SmartConnect clock-domain.

---

## 7. Vitis: platform and app

**Platform** (first time): create a Platform component named exactly **`platform`** from `pl/vivado/dfx_static.xsa`: OS standalone, processor `ps7_cortexa9_0`, generate boot artifacts on. Build it.

**Platform** (after every new export): `platform` → Settings → `vitis-comp.json` → **Update XSA** → select `pl/vivado/dfx_static.xsa` → Build.

**App:** `app_component` is in the repo and uses the sources in `ps/` and the `.bin` files in `ps/bitstreams/`. After a new export, force the bitstreams to be relinked, then build:
```bash
touch ps/ps_bitstreams.S
```
Build `app_component`. The ELF is ~8.5 MB (both partial bitstreams inside).

**Run:** board jumper on **JTAG**, USB connected, power on → select `app_component` → **Run**.

---

## 8. PC: test on the board

```bash
source pc/.venv/bin/activate
python pc/send_images.py                          # 1 image per class, both models (smoke test)
python pc/send_images.py --limit-per-class 500    # full test set, 2000 images
```
Defaults (port `/dev/ttyUSB1`, dataset, output folder, images per class) are at the top of `send_images.py`. Windows: set `DEFAULT_PORT = "COM3"` (or the right COM port). Other options: `--models cnn|snn|both`, `--port`.

Expected: CNN ≈ 1039 µs, SNN ≈ 430 µs, DFX switch ≈ 42.45 ms, four independent logits per model.

Compare and plot:
```bash
python pc/results.py pc/results/<run>.jsonl --output pc/results/comparison.csv   # fails if the CSV exists
python pc/plot_results.py                                                        # PNGs next to the CSV
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `licence BASIC ... does not cover DFX` | Step 0: Enterprise `.lic` in `~/.Xilinx`, BASIC one moved out |
| `PYNQ-Z2 board files are not installed` | Run the script from the GUI Tcl Console, not batch mode |
| `couldn't read file ".../dfx_project.tcl"` | Use the absolute path in `source` |
| Build: `file not found: cnn_partial.bin` | Run `export_dfx.tcl` first; include path `../../ps/bitstreams` in `app_component/src/UserConfig.cmake` |
| Launch: `invalid command name "ps7_init"` | Copy the platform's `ps7_init.tcl` over `app_component/_ide/psinit/app_component.tcl` |
| `Permission denied: /dev/ttyUSB1` | Step 0: `dialout` group, log out and in |
| No reply / timeout | Wrong port (`ls /dev/ttyUSB*`, the board has two; UART is usually the second) or app not running |
| SNN result equals CNN result, same time for both | Old `.bin` files: re-run `export_dfx.tcl`, `touch ps/ps_bitstreams.S`, rebuild app |
| Board stops answering after a rebuild | Partial bitstreams from an older build: always Update XSA + rebuild app after a new export |
| `send_images.py`: file exists | Each run needs a new output file; the default name has a timestamp |
