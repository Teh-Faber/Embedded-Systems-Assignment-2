# ip

Exported HLS IP, one folder per IP. `../vivado/dfx_project.tcl` uses this folder as its IP repository and looks for the IPs by top-level name.

| Folder | HLS component | Top function |
|---|---|---|
| `ds_cnn/` | `../../cnn/` | `ds_cnn` |
| `snn_mlp/` | `../../snn/` | `snn_mlp` |

## Generate an IP

Same steps for both, shown for the CNN. Run from the component folder in a shell where the Vitis settings script has been sourced (`settings64.bat` / `settings64.sh`).

```
cd cnn
vitis-run --mode hls --csim    --config hls_config.cfg --work_dir build
v++ -c    --mode hls           --config hls_config.cfg --work_dir build
vitis-run --mode hls --package --config hls_config.cfg --work_dir build
```

1. C simulation against the testbench. Needs the test data in `data/` (see the root README).
2. C synthesis. Check timing and utilization in `build/reports/`.
3. Packaging as a Vivado IP. The result is `build/hls/impl/ip/` (also zipped as `export.zip` next to it).

Copy the contents of `build/hls/impl/ip/` to `pl/ip/ds_cnn/`, so that `pl/ip/ds_cnn/component.xml` exists. For the SNN: `cd snn`, same commands, copy to `pl/ip/snn_mlp/`.

In the Vitis GUI the same three steps are **C Simulation**, **C Synthesis** and **Package** on the HLS component, with `hls_config.cfg` as its configuration.

`build/` is ignored by git. The commands were checked against Vitis 2024.2 only as far as the option names. The output paths are taken from the lecture guides.

## What the IP must look like

The DFX design swaps the two IPs in one partition, so they need the same ports:

- top function named `ds_cnn` or `snn_mlp`
- one `s_axilite` bundle with all arguments and `port=return`
- one `m_axi` port for the input image, read-only in both (`const` pointer), 16-bit elements
- no other ports
- timing met at 10 ns. The static design clocks the partition at 100 MHz

The register offsets do not have to match between the two IPs, but the PS needs to know them. After packaging, compare `drivers/<top>_v1_0/src/x<top>_hw.h` in the IP folder with the table in `ps/ps_main.c` (see `ps/README.md`).

Next step: `pl/README.md`.
