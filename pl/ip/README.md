# ip

HLS IP for the reconfigurable partition, one folder per IP. `../vivado/dfx_project.tcl` uses this folder as its IP repository and finds the IPs by their top-level name.

## Temporary placeholders

| Folder | Top function | Origin |
|---|---|---|
| `ds_cnn/` | `ds_cnn` | Image CNN of assignment 2 |
| `snn_mlp/` | `snn_mlp` | Image SNN of assignment 2 |

These two IPs are temporary. They stay until the IMU models are exported, so that the DFX build gives partial bitstreams and the debug inference has a module to run. Their outputs for an IMU window have no meaning. Their HLS sources are no longer in the repo; they are in the git history (assignment 2). Delete both folders when the IMU models are in place.

## IMU models

The IMU CNN and SNN go into the same partition, with the same boundary. Their input is the filtered window from the static PL: int16[200][6], channels a_x a_y a_z g_x g_y g_z, oldest sample first. The suggested top-level function and the checks after the export are in `../../docs/imu_system.md`, section "Interface for the IMU models".

## Generate an IP

Run from the folder of the HLS component, in a shell with the Vitis settings script (`settings64.bat` or `settings64.sh`):

```
vitis-run --mode hls --csim    --config hls_config.cfg --work_dir build
v++ -c    --mode hls           --config hls_config.cfg --work_dir build
vitis-run --mode hls --package --config hls_config.cfg --work_dir build
```

1. C simulation with the testbench.
2. C synthesis. Check timing and utilization in `build/reports/`.
3. Packaging as a Vivado IP. The result is in `build/hls/impl/ip/`.

Copy the contents of `build/hls/impl/ip/` to `pl/ip/<top>/`, so that `pl/ip/<top>/component.xml` exists. In the Vitis GUI the same three steps are **C Simulation**, **C Synthesis** and **Package**.

## What the IP must look like

Both modules share one partition, so they need the same ports:

- one `s_axilite` bundle with all arguments and `port=return`
- one `m_axi` port for the input window, read-only (`const` pointer), 16-bit elements
- no other ports
- timing met at 10 ns. The static design clocks the partition at 100 MHz

Then build the project with the new IPs: `set modules {rm_cnn <CNN top> rm_snn <SNN top>}` before you source `dfx_project.tcl`. The register offsets do not have to be the same in the two IPs, but the PS must know them: compare `drivers/<top>_v1_0/src/x<top>_hw.h` with `model_registers` in `ps/ps_accel.c` (see `ps/README.md`).

Next step: `pl/README.md`.
