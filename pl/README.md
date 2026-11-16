# pl

Everything after HLS: exported IP, the Vivado project and the Vitis workspace.

| Folder | Use |
|---|---|
| `ip/` | Exported HLS IP, one folder per IP (`ip/ds_cnn/`, `ip/snn_mlp/`). See `ip/README.md`. |
| `vivado/` | `dfx_project.tcl` creates the DFX project in `vivado/dfx/`. `export_dfx.tcl` writes the partial bitstreams and the XSA. |
| `vitis/` | Vitis workspace in `vitis/ws/`: platform from the XSA and the app. |
| `constraints/` | `dfx_pblock.xdc` is the floorplan of the reconfigurable partition. `PYNQ-Z2 v1.0.xdc` is the board's pin list, not used by the DFX design. |

Generated project files are ignored by git (see `.gitignore`). Commit scripts, XDC and the XSA, not the project folders.
The HLS components themselves live in `../cnn/` and `../snn/` next to their `hls_config.cfg`.

## DFX design

One reconfigurable partition (RP) holds either the CNN or the SNN. The PS swaps them by writing a partial bitstream over PCAP. Nothing in the PL takes part in the reconfiguration itself.

```
            M_AXI_GP0                          s_axi_control
  PS7  ───────────────► sc_ctrl ──► ┌─────────┐ ───────────► ┌──────────────┐
                                    │   DFX   │              │  rp          │
            S_AXI_HP0               │decoupler│  m_axi_gmem  │  rm_cnn  or  │
       ◄─────────────── sc_mem  ◄── └─────────┘ ◄─────────── │  rm_snn      │
                                         ▲                   └──────────────┘
            EMIO GPIO bit 0              │ decouple                 ▲ ap_rst_n
       ──────────────────────────────────┴──► rst_rp (aux reset) ───┘
```

| Part | What it does |
|---|---|
| `ps7` | PYNQ-Z2 board preset. FCLK0 at 100 MHz clocks everything. GP0 master, HP0 slave, 1 EMIO GPIO bit. UART0 on MIO as before. |
| `sc_ctrl`, `sc_mem` | SmartConnects: PS to the accelerator's registers, accelerator to DDR. |
| `decoupler` | DFX Decoupler with two interfaces. Isolates the RP while it is being reconfigured. |
| `rst_static`, `rst_rp` | Reset for the static AXI logic, and a separate reset for the RP that is also asserted while decoupled. |
| `rp` | Block design container with DFX enabled. Its modules are the block designs `rm_cnn` and `rm_snn`, each with one HLS IP named `accel`. |

Contract with the PS (`../ps/ps_dfx.c`, `../ps/ps_main.c`):

- EMIO GPIO bit 0 (PS GPIO pin 54) high: RP decoupled and in reset. Low: coupled, reset released about 16 clock cycles later.
- Accelerator registers at `0x4000_0000`, 64K, for whichever module is loaded.
- The accelerator's AXI master sees all of DDR through HP0.
- No interrupt. The PS polls `ap_done`.

RP boundary, the same for every module: `ap_clk`, `ap_rst_n`, `s_axi_control` (AXI4-Lite slave), `m_axi_gmem` (AXI4 master). `dfx_project.tcl` finds the IP's interfaces by type, so the bundle names in the HLS source do not have to be these.

Floorplan: the RP has the two lower clock region rows of the device, the static logic the upper row next to the PS. See `constraints/dfx_pblock.xdc`.

## When the HLS IPs are generated

1. Export the IPs to `ip/ds_cnn/` and `ip/snn_mlp/` (`ip/README.md`). One IP is enough for a first build. The script skips what is missing.
2. Create the project. Needs the PYNQ-Z2 board files in Vivado.

   ```
   vivado -mode batch -source pl/vivado/dfx_project.tcl
   ```

   or `source` the script from the Tcl console. It deletes `vivado/dfx/` and builds it again, so run it again whenever an IP is exported again. Read the lines starting with `DFX:` in the output.
3. Open `vivado/dfx/dfx.xpr`. Check:
   - **Address Editor**: the accelerator is at `0x4000_0000`.
   - **Tools > Dynamic Function eXchange Wizard**: one configuration per module, `impl_1` plus one child run. If the script warned that it could not create them, do it here: **automatically create configurations**, then **automatically create configuration runs** (Standard DFX).
4. **Generate Bitstream**. This runs synthesis of the static design and both modules, `impl_1` (static + first module), then the child run (same static, second module).
5. Check the reports of both implementation runs: timing met, and RP utilization for each module. If a module does not fit, change the pblock (see the comments in the XDC).
6. Export:

   ```
   vivado -mode batch -source pl/vivado/export_dfx.tcl
   ```

   This writes `ps/bitstreams/cnn_partial.bin`, `ps/bitstreams/snn_partial.bin` and `vivado/dfx_static.xsa` (with the full bitstream of `impl_1`).
7. Continue in `../ps/README.md`.

The full bitstream and the two partial bitstreams belong together. A partial bitstream only works on the static design it was implemented with, so after any new implementation run, export all three again and rebuild the PS application.

## Status and known risks

The scripts and the pblock have not been run in Vivado yet. They were written on a machine without Vivado and only checked for Tcl syntax. Expect to fix things on the first run. The places most likely to need it:

- **Decoupler configuration** (`CONFIG.ALL_PARAMS` in `dfx_project.tcl`). If it fails, configure the `decoupler` cell in the GUI: two interfaces, both `xilinx.com:interface:aximm_rtl:1.0`. `control`: protocol AXI4-Lite, RP side is the slave. `gmem`: protocol AXI4, RP side is the master. No AXI-Lite control, no status signal.
- **RP boundary mismatch.** Vivado requires identical ports on all modules. If one IP's AXI master is read-only and the other's is read/write, the boundaries differ. Make both read-only in HLS (`const` pointer).
- **Configuration names.** The reconfigurable module names are generated by Vivado. The wizard in step 3 is the fallback.
- **Pblock ranges.** Site ranges are from the xc7z020 grid, not from an implemented design.
- **Clock.** `cnn/hls_config.cfg` targets 8 ns, `snn/hls_config.cfg` 10 ns. The static design runs at 100 MHz. Change `fclk_mhz` in the script if that should be different.
