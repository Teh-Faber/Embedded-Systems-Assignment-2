# FPGA image UART integration

Board sources are in `ps/`, PC scripts in `pc/`.

## Build targets

| Vitis application | Files to add | Purpose |
|---|---|---|
| UART protocol test | `ps_uart_test.c`, `ps_uart.c`, `ps.h` | Checks the framing and the received pixels on the board. |
| CNN integration | `ps_main.c`, `ps_uart.c`, `ps.h` | Runs the lab CNN and sends model results using the same UART module. |

Use a Vitis 2026.1 SDT Cortex-A9 standalone application. Each row is a separate build with one entry point. Use the working PYNQ-Z2 platform and its FSBL to initialize the PS. For CNN integration, load the lab CNN bitstream and use its matching BSP. The draft uses `XPAR_DS_CNN_0_BASEADDR` and the CNN register offsets from the lab. The UART defaults to `XPAR_XUARTPS_0_BASEADDR`, 4 Mbaud, 8N1.

## Run the UART protocol test

Start the test application on the board, close any serial terminal, and run the following from `pc/` in PowerShell. Replace the dataset path and COM port as needed. The dataset must contain the four test class folders. The script also accepts the path to the `test` folder itself.

```powershell
py -m pip install -r requirements.txt
py -m serial.tools.list_ports -v
py send_images.py --port COM3 --dataset "C:\path\to\shapes_dataset" --limit-per-class 1 --protocol-test --output protocol_test.jsonl
```

The run should report **2 rejected requests, 4 byte-for-byte echoes, and 8 model-format replies**. The two rejection tests send an invalid length and an invalid model mask. The echo compares all 4,800 bytes returned from the PS receive buffer with the original image. The fixture replies alternate CNN/SNN order and carry status 128, so they stay out of accuracy and timing summaries. The echo measures a round trip with 4,800 pixel bytes travelling in each direction.

## Run CNN inference and compare results

Start the CNN integration application and use `--models cnn` for the current build.

```powershell
py send_images.py --port COM3 --dataset "C:\path\to\shapes_dataset" --models cnn --limit-per-class 1 --output cnn_smoke.jsonl
```

The original lab weights can verify that a CNN result comes back. Use shape-trained weights to measure shape accuracy. The default class ID order is `Circle, Figure Eight, Square, Triangle`. If training used a different order, supply the same `--classes` order in each capture.

Once the SNN and DFX controller are integrated, use `--models both` to collect paired replies from one upload, or run `--models cnn` and then `--models snn` on the same unchanged test dataset for benchmarking. Use distinct output filenames for separate runs.

```powershell
py results.py paired.jsonl --output comparison.csv
py results.py cnn.jsonl snn.jsonl --output benchmark_comparison.csv
```

`send_images.py` prints labels, predictions, scores, timings, and errors while saving JSONL. Its round-trip timer stops before printing and saving the record. `results.py` writes one CSV row per image, preserves missing replies and board errors, and computes both confusion matrices and accuracy from the same set of valid pairs.

## Integration points

- `ps_uart.c` owns UART initialization, request validation, and reply serialization. `uart_receive_request()` fills an `ImageRequest` owned by the caller. `uart_send_result()` sends the result for one model.
- `ps_main.c` owns the application loop and model readiness. `prepare_cnn_input()` converts raw grayscale pixels to the lab's signed Q8.8 format and flushes the DDR buffer. `run_cnn()` starts the IP, applies a timeout, and returns the classification. After a CNN timeout the draft marks the hardware unavailable and preserves the input buffer until recovery or restart.
- Add DFX in `select_model()` or call the group's controller from there. Update `ready_model` when the selected module is ready. Set `DfxTiming` to emit one optional type 4 measurement before the associated result. Add SNN preparation and inference in the model dispatch. The UART module stays the same.

## Wire format

The request borrows the CNN lab's `A6 6A` start marker and length field. Replies borrow the labs' `A5` and `5A` markers. This protocol adds a version, message type, and image ID. The header is `body_length:u16, version:u8, type:u8, image_id:u32`. Multi-byte fields are little-endian; body length excludes the header and markers.

| Direction / type | Wire layout and body | Total bytes |
|---|---|---:|
| Request 1 | `A6 6A`, header, `model_mask:u8, pixels:u8[4800]` | 4,811 |
| Result 2 | `A5`, header, `model_id:u8, status:u8, class_id:u8, infer_us:u32, prep_us:u32, scores:i32[4]`, `5A` | 37 |
| Diagnostic 3 | `A5`, header, `status:u8`, `5A` | 11 |
| Echo 3 | `A5`, header, `status:u8=0, pixels:u8[4800]`, `5A` | 4,811 |
| DFX timing 4 | `A5`, header, `model_id:u8, dfx_us:u32`, `5A` | 15 |

Model masks are 0 for test echo, 1 for CNN, 2 for SNN, and 3 for both. With mask 3, each result has the same image ID and a distinct model ID. The PC accepts either result order. Diagnostic statuses are 1 for invalid length, 2 for invalid model mask, 5 for unsupported version, 6 for unsupported message type, 7 for a truncated request, and 8 when echo is unavailable. Result statuses are 0 for success, 3 for model unavailable, 4 for inference failure, and 128 for a test reply.

The checks validate framing and, in echo mode, pixel contents. In an ordinary model request, a changed pixel byte can still fit the frame format.
