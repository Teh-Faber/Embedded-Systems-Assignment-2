# Build guide: from clone to recordings on the PYNQ-Z2

Tools: Vivado and Vitis 2026.1. Tested on Windows with the PYNQ-Z2 and the LSM6DSO32 on 26 Nov 2026.
Run the commands from the repo root unless a step gives another folder.

---

## 0. One-time setup

**Licence.** DFX needs a Vivado **ENTERPRISE** licence. The free BASIC licence stops with `[Vivado 12-29795] ... does not cover DFX`. Put the `.lic` file in `~/.Xilinx/` (Windows: `C:\Users\<user>\.Xilinx\`) and move any BASIC licence out of that folder. The first line of the Vivado log must say `ENTERPRISE license has been detected`.

**Board files.** Install the PYNQ-Z2 board files (Vivado: Xilinx Store).

**Sensor.** Connect the LSM6DSO32 to PMOD JB, top row, as in lab 6: JB1 CS, JB2 SDO, JB3 SDA, JB4 SCL, plus 3V3 and GND.

**Serial port.** Windows: find the COM port of the board in Device Manager (COM8 on the test PC). Linux: `sudo usermod -aG dialout $USER`, then log out and in.

---

## 1. Clone

```
git clone -b Final-Assignment https://github.com/Teh-Faber/Embedded-Systems-Assignments.git
cd Embedded-Systems-Assignments
```

---

## 2. Python

```
python -m venv pc/.venv
pc/.venv/bin/pip install -r pc/requirements.txt          # Windows: pc\.venv\Scripts\pip install -r pc\requirements.txt
```

Run Python from a normal terminal. Do not run it from the Vivado Tcl console: Vivado sets `PYTHONHOME` and `PYTHONPATH` for its own Python.

---

## 3. Filters (only when you change them)

```
python filters/design_filters.py            # defaults: 104 Hz, window 200 samples, FIR 31 taps, IIR order 4
python filters/design_filters.py --help
```

The script writes `filters/filters.json`, `filters/fir_lowpass.coe`, `pl/hdl/iir_coeffs_pkg.vhd` and `pl/vivado/imu_params.tcl`. After a change, rebuild both designs (step 4).

Optional check of the RTL (Linux or WSL with GHDL 4): `cd pl/sim/imu && ./run_ghdl.sh`.

---

## 4. Vivado: one DFX project per filter

Use the Vivado **GUI**. Close any open project (`close_project`). In the Tcl Console, with absolute paths:

```tcl
set dfx_filter fir
source <repo>/pl/vivado/dfx_project.tcl
launch_runs impl_1 child_0_impl_1 -to_step write_bitstream -jobs 8
wait_on_run child_0_impl_1
source <repo>/pl/vivado/export_dfx.tcl
close_project
```

Do the same with `set dfx_filter iir`.

Check:
- `dfx_project.tcl` prints `DFX: project ready`.
- `export_dfx.tcl` ends with `DFX: partial bitstreams written for: cnn snn`.
- Timing: in `pl/vivado/dfx_<filter>/dfx_<filter>.runs/impl_1/dfx_static_wrapper_timing_summary_routed.rpt` and in the same file in `child_0_impl_1`, WNS and WHS are 0 or more.

Outputs: `ps/bitstreams/<filter>/{cnn,snn}_partial.bin` and `pl/vivado/dfx_static_<filter>.xsa`.

Warnings that you can ignore: `PSU-1..4` (DDR preset of the board), `dfx_decoupler-1`, the SmartConnect clock domain warning for `sc_ctrl`, `BD 41-759` for the decoupler pins (the script connects them after the first validation), `BD 41-1347` for `filter/aresetn` (`imu_capture` makes this reset synchronously).

Batch mode does not load the board store of the GUI. To use batch mode, set `board.repoPaths` before you source the script, for example `set_param board.repoPaths [list C:/Users/<user>/AppData/Roaming/Xilinx/Vivado/2026.1/xhub/board_store/xilinx_board_store]`.

---

## 5. Vitis: platform and application

1. **Workspace.** Open the repo root as the workspace. If Vitis asks to update the workspace, click **Update**.
2. **Platform** (first time). Create a platform component with the name **`platform`** from `pl/vivado/dfx_static_fir.xsa` (or `_iir`): OS standalone, processor `ps7_cortexa9_0`. Build it.
3. **Platform** (after each new export, or to change the filter). `platform` → Settings → `vitis-comp.json` → **Update XSA** → select the new XSA → Build.
4. **Application.** `app_component` is in the repo. Check two settings:
   - `app_component/src/UserConfig.cmake`: the include folder `../../ps/bitstreams/fir` (or `iir`) must match the XSA of the platform. The partial bitstreams only work with their own static design.
   - `app_component/src/app.yaml`: `domain_path` must point to `<repo>/platform/export/platform/sw/standalone_ps7_cortexa9_0` on your computer.
5. **Build.** After a new export, do a clean build of `app_component` (or `touch ps/ps_bitstreams.S`), so that the new `.bin` files are linked in. The ELF is about 8.5 MB.
6. **Run.** Set the boot jumper to **JTAG**, connect USB, power on, select `app_component`, then **Run**. The launch programs the full bitstream and starts the ELF.

---

## 6. PC: test and record

```
cd pc
py imu_tool.py --port COM8 info
py imu_tool.py --port COM8 stream --seconds 10 --out test.csv
py imu_tool.py check test.csv
```

Expected: `who_am_i 0x6C`, `imu_ok True`, `n_samples 200`, `no samples lost`, `PASS`. LD0 blinks, LD1 is on, LD2 shows SW1, LD3 is on (partition decoupled, no model loaded).

Record training data (details in `docs/imu_system.md`). Enter starts a take (`--auto`: countdown). After the take, Enter saves it, Space records it again, q stops.

```
py imu_tool.py --port COM8 record --label circle --takes 20
py imu_tool.py check ../data/imu/circle
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `licence BASIC ... does not cover DFX` | Step 0: ENTERPRISE licence in `~/.Xilinx`, BASIC licence moved out |
| `PYNQ-Z2 board files are not installed` | Run the script in the GUI Tcl Console, or set `board.repoPaths` in batch mode (step 4) |
| `couldn't read file ".../dfx_project.tcl"` | Give `source` the absolute path |
| `create_project` cannot remove `dfx_<filter>.runs` | A run of the old project still uses the folder. Wait until it stops, or close the project |
| Build: `file not found: cnn_partial.bin` | Run `export_dfx.tcl` first. Check the include folder in `UserConfig.cmake` |
| Build cannot find the domain or the BSP | `domain_path` in `app_component/src/app.yaml` (step 5) |
| Launch: `invalid command name "ps7_init"` | Copy the platform's `ps7_init.tcl` over `app_component/_ide/psinit/app_component.tcl` |
| `ModuleNotFoundError: No module named 'serial'` | The Python that `py` starts has no pyserial. Use the venv of step 2, or install `pc/requirements.txt` into that Python (`py -m pip install -r pc/requirements.txt`) |
| `imu_tool.py`: no reply from the board | Wrong COM port, or the application does not run |
| `who_am_i 0xFF`, `imu_ok False` | No sensor, or wrong wiring. The MISO line has a pull-up, so an open bus reads 0xFF |
| `imu_tool.py`: samples lost | The PC did not read the serial port fast enough. Close other programs and record again |
| Python errors in the Vivado Tcl console | Vivado sets `PYTHONHOME`. Run Python from a normal terminal |
| Board stops answering after a rebuild | Partial bitstreams from an older build. After each export: Update XSA, then a clean build of the application |
| `Permission denied: /dev/ttyUSB1` | Linux: step 0, `dialout` group |
