# ps

Bare-metal C for the Zynq PS. UART protocol and PC scripts are described in `../docs/uart_integration.md`.

| File | Use |
|---|---|
| `ps.h` | Shared types and the API of the modules below |
| `ps_uart.c` | UART framing: receive requests, send results |
| `ps_main.c` | Main loop. Selects the model, runs the accelerator, sends the result |
| `ps_dfx.c` | DFX: loads a partial bitstream over PCAP and decouples the partition meanwhile |
| `ps_bitstreams.S` | Links `bitstreams/cnn_partial.bin` and `bitstreams/snn_partial.bin` into the ELF |
| `ps_uart_test.c` | Separate test application for the UART protocol, no PL needed |

## How a model switch works

`ps_main.c` keeps track of which module is in the reconfigurable partition (`ready_model`). When a request asks for the other one, `select_model()` calls `dfx_load()`:

1. EMIO GPIO bit 0 high: the DFX decoupler isolates the partition and the partition is held in reset.
2. The partial bitstream is written through the devcfg DMA (`XDcfg_Transfer`, non-secure PCAP write) and the PS waits for `DMA_DONE` and `D_P_DONE`.
3. 1 ms in reset with the clock running, GPIO low, 1 ms for the reset to release.

The time for the whole switch is sent to the PC as a type 4 frame before the model's result. After start-up nothing is loaded from the PS's point of view, so the first request for each model always switches. A request for both models runs the loaded one first. If an inference times out, the next request loads the module again, which also resets it.

Both modules are driven by the same `run_model()`: Q8.8 image in a DDR buffer, pointer written to the IP, start, poll done, read prediction and logits. Only the register offsets and the logit width differ, see `model_registers` in `ps_main.c`.

## When the HLS IPs are generated

Do the PL steps first (`../pl/README.md`). They leave three files:

- `bitstreams/cnn_partial.bin` and `bitstreams/snn_partial.bin`
- `../pl/vivado/dfx_static.xsa`

Then:

1. **Check the register offsets.** Open `x<top>_hw.h` for each IP (`pl/ip/<top>/drivers/<top>_v1_0/src/`) and compare with `model_registers` in `ps_main.c`:

   | Field | In the header |
   |---|---|
   | `input_ptr` | `..._ADDR_<image argument>_DATA`, the lower word of the pointer |
   | `prediction` | `..._ADDR_<prediction argument>_DATA` |
   | `logits` | `..._ADDR_LOGITS_OUT_BASE` |
   | `logit_bits` | `..._WIDTH_LOGITS_OUT`: 16 or 32 |

   The CNN row is from the lab IP. The SNN row is a guess until the IP has been exported.
2. **Platform.** In Vitis, create a platform from `dfx_static.xsa`: standalone, `ps7_cortexa9_0`, boot components on. If a platform already exists, update its XSA and rebuild it. The BSP gets the `devcfg` and `gpiops` drivers from the XSA.
3. **Application.** Empty C application on that platform. Add `ps_main.c`, `ps_uart.c`, `ps_dfx.c`, `ps_bitstreams.S` and `ps.h`.
4. **Include path.** Add the `ps/bitstreams` folder to the application's include directories (application settings, or `USER_INCLUDE_DIRECTORIES` in `UserConfig.cmake`). The assembler finds the `.bin` files through it. If the build says it cannot open `cnn_partial.bin`, this is the setting that is missing.
5. **Build and run.** The launch configuration programs the FPGA with the full bitstream from the XSA, then starts the ELF. The ELF is several MB because of the bitstreams.
6. **Test from the PC**, in `pc/`:

   ```
   py send_images.py --port COM3 --dataset <shapes dataset> --models both --limit-per-class 1 --output dfx_smoke.jsonl
   ```

   Expected: for the first image a DFX time for each model, then one switch per image. `--models cnn` followed by `--models snn` gives one switch per run.

Whenever Vivado has implemented the design again: export again, update the platform with the new XSA and rebuild the application. A partial bitstream from an older build does not match the new static design and can stop the PL.

## Build options

| Define | Default | Meaning |
|---|---|---|
| `PS_DFX` | `1` | `0` builds for a fixed bitstream with only the CNN (the lab design). Then leave out `ps_dfx.c` and `ps_bitstreams.S`. |
| `ACCEL_BASE_ADDR` | `0x40000000` | AXI-Lite address of the partition. With `PS_DFX=0` it defaults to `XPAR_DS_CNN_0_BASEADDR`. |
| `MODEL_TIMEOUT_MS` | `1000` | Inference timeout |
| `DFX_TIMEOUT_MS` | `2000` | Timeout for each PCAP wait |
| `DFX_DECOUPLE_PIN` | `54` | PS GPIO pin of the decouple signal (54 = EMIO bit 0) |

## If it does not work

| Symptom | Look at |
|---|---|
| Status 3 (model unavailable) and a DFX time near 2 s | PCAP timeout. `.bit` used instead of `.bin`, or the PL was not programmed with the full bitstream first. |
| Status 3 and a short DFX time | devcfg reported an error or the bitstream length is not a multiple of 4. |
| Board stops answering on the first model request | A register access to the partition got no answer: wrong `ACCEL_BASE_ADDR`, or the decouple signal is not EMIO bit 0. |
| Status 4 (inference failed) | The module did not finish in time or returned a class above 3. Check the register offsets. |
| Results that look random for the SNN | The lab `snn_mlp` expects 384 features, not the image. See `../snn/lab_app/README.md`. |

## Status

Not compiled against the real BSP and not run on the board yet. `ps_main.c` and `ps_dfx.c` pass a syntax check against stub headers. The driver calls are modelled on the Zynq FSBL's PCAP code. Things to confirm on the first build:

- that Vitis compiles the `.S` file as part of the application
- the SNN register offsets
- that the SNN IP takes the same Q8.8 image buffer as the CNN. Until the feature extraction has moved into the IP, the SNN will load and run, but its answers are meaningless
