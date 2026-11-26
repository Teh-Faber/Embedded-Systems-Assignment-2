# ps

Bare-metal C for the Zynq PS: the IMU application. The UART protocol is in `../docs/imu_system.md`.

| File | Function |
|---|---|
| `ps.h` | Shared types and the API of the modules below |
| `ps_imu_main.c` | Main loop: IMU commands, streaming of samples, debug inference on windows from the PC, SW1 |
| `ps_imu.c` | Reads the IMU capture BRAM of the static PL (sequence lock, window copy, stable header reads), capture clear and SW1 over EMIO |
| `ps_accel.c` | The accelerator: register map, start and poll, model selection with DFX |
| `ps_dfx.c` | Loads a partial bitstream over PCAP and decouples the partition during the load |
| `ps_uart.c` | UART framing: receive requests, send replies |
| `ps_bitstreams.S` | Links `cnn_partial.bin` and `snn_partial.bin` from the include folder into the ELF |
| `bitstreams/` | Partial bitstreams from `pl/vivado/export_dfx.tcl`, one folder per filter design. Not in git |
| `host_test/` | Builds the application on a PC with stub drivers and runs `pc/imu_tool.py` against it |

## Build

Vitis builds the application as `app_component` (`../build_guide.md`, step 5). `app_component/src/UserConfig.cmake` lists the sources. Its include folder `../../ps/bitstreams/fir` (or `iir`) must match the XSA of the platform.

Host test, without a board (Linux or WSL, gcc, Python with numpy, scipy and pyserial):

```
python ps/host_test/run_host_test.py fir
python ps/host_test/run_host_test.py iir
```

It builds the real sources against `host_test/mock.c` (UART on a pseudo-terminal, fake accelerator, DFX that always works) and `host_test/pl_sim.py` (the capture BRAM, samples at 97.64 Hz). Then it runs `imu_tool.py` and `imu_path.py` and checks the results (23 checks).

## How a model switch works

`ps_accel.c` records which module is in the reconfigurable partition (`ready_model`). When a request needs the other module, `accel_select()` calls `dfx_load()`:

1. EMIO GPIO bit 0 goes high: the DFX decoupler isolates the partition and holds it in reset.
2. The devcfg DMA writes the partial bitstream (`XDcfg_Transfer`, non-secure PCAP write). The PS waits for `DMA_DONE` and `D_P_DONE`.
3. 1 ms in reset with the clock on, GPIO low, 1 ms for the reset release.

The PS sends the time of the whole switch to the PC as a type 4 frame before the result of the model. After start-up no module is loaded, so the first request for each model always does a switch. A request for both models runs the loaded module first. If an inference times out, the next request loads the module again. This also resets it.

`accel_run()` drives both modules: input buffer in DDR, pointer written to the IP, start, poll done, read the prediction and the logits. Only the register offsets and the logit width are different, see `model_registers` in `ps_accel.c`.

## When the IMU models are exported

1. **Check the register offsets.** Open `x<top>_hw.h` of each IP (`pl/ip/<top>/drivers/<top>_v1_0/src/`) and compare it with `model_registers` in `ps_accel.c`:

   | Field | In the header |
   |---|---|
   | `input_ptr` | `..._ADDR_<input argument>_DATA`, the lower word of the pointer |
   | `prediction` | `..._ADDR_<prediction argument>_DATA` |
   | `logits` | `..._ADDR_LOGITS_OUT_BASE` |
   | `logit_bits` | `..._WIDTH_LOGITS_OUT`: 16 or 32 |

   The values in the table now are those of the placeholder IPs.
2. Build both static designs again and export them (`../pl/README.md`). Update the XSA of the platform and do a clean build of the application.
3. Test with SW1 low:

   ```
   py imu_tool.py --port COM8 window --out w.csv
   py imu_tool.py --port COM8 infer --input w.csv --models both --classes <number of classes>
   ```

   Expected: a DFX time for each model at the first request, then a class and the scores of each model.

## Build options

| Define | Default | Meaning |
|---|---|---|
| `ACCEL_BASE_ADDR` | `0x40000000` | AXI-Lite address of the partition |
| `MODEL_TIMEOUT_MS` | `1000` | Inference timeout |
| `DFX_TIMEOUT_MS` | `2000` | Timeout of each PCAP wait |
| `DFX_SETTLE_US` | `1000` | Time in reset before and after the partition is coupled again |
| `DFX_DECOUPLE_PIN` | `54` | PS GPIO pin of the decouple signal (54 = EMIO bit 0) |
| `IMU_BRAM_BASE_ADDR` | `0x42000000` | AXI address of the IMU capture BRAM |
| `IMU_CLEAR_PIN`, `IMU_SW1_PIN` | `55`, `56` | PS GPIO pins of capture clear and SW1 (EMIO bits 1 and 2) |
| `UART_BAUD` | `4000000` | Baud rate of the PS UART |

## If it does not work

| Symptom | Look at |
|---|---|
| Status 10 (IMU not ready) for all IMU commands except info | The bitstream has no IMU capture BRAM, or the BRAM header is wrong. Program the full bitstream of `dfx_static_<filter>.xsa` |
| Status 3 (model unavailable) and a DFX time near 2 s | PCAP timeout. A `.bit` file instead of a `.bin` file, or the PL did not get the full bitstream first |
| Status 3 and a short DFX time | devcfg reported an error, or the length of the bitstream is not a multiple of 4 |
| The board stops answering at the first model request | A register access to the partition got no answer: wrong `ACCEL_BASE_ADDR`, or the decouple signal is not EMIO bit 0 |
| Status 4 (inference failed) | The module did not finish in time, or it returned a class that is not less than `n_classes`. Check the register offsets |

## Status (26 Nov 2026)

Built with Vitis 2026.1 and run on the board with both static designs: `info`, `stream`, `window` and the capture clear work. `record` is tested in the host test only. Not tested on the board yet: debug inference and a DFX switch from the IMU application. The DFX code is the same as in assignment 2, where a switch took about 42 ms. The live inference loop for SW1 high is not written yet.
