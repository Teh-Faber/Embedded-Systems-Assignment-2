# lab_app

The lab 8 SNN setup: `helloworld.c` runs on the PS, `send_images_uart_snn.py` on the PC.
It works, but it is built differently from the CNN setup. This note lists the differences and what has to move into the SNN HLS IP so that the PS and PC side are the same for both networks and only the IP is swapped (PCAP DFX).

## What the lab app does

```
PC:  image -> resize 80x60 -> Sobel -> ON/OFF spikes -> 12x16 grid -> 384 int16 features
     UART:  AA 55 + 768 bytes
PS:  features -> AXI BRAM -> write BRAM address to IP -> start -> poll done
     UART:  A5 pred time_us logits[4] 5A   (23 bytes)
PL:  snn_mlp reads 384 features over m_axi, 384 -> 64 -> 32 -> 4 with LIF, T=1
```

So the image to spike conversion is done in Python on the PC. The IP only sees features.

## Changes in helloworld.c

Compared to the lecture version, ours is ported to the SDT flow in newer Vitis:

- `xiltimer.h` instead of `xtime_l.h`
- drivers are looked up by base address, not device ID (`XPAR_XUARTPS_0_BASEADDR`, `XPAR_SNN_MLP_0_BASEADDR`, `XPAR_AXI_BRAM_CTRL_0_BASEADDR`, `XPAR_AXI_GPIO_0_BASEADDR`)
- UART0 instead of UART1

Things it has that `ps/ps_main.c` does not:

- its own UART frames (`AA 55` in, 23 byte reply out)
- an AXI BRAM controller as input memory, filled with `Xil_Out16`
- an AXI GPIO pin toggled around the inference for scope timing

## CNN setup vs SNN lab setup

| | CNN (`ps/ps_main.c`) | SNN (lab app) |
|---|---|---|
| PC sends | 4800 raw pixels | 384 features |
| Preprocessing | PS: pixel to Q8.8, `4*(p/255)-2` | PC: Sobel, spikes, grid |
| Input memory | PS DDR buffer, IP reads over m_axi | AXI BRAM |
| m_axi port | `input_img`, depth 4800 | `feature_mem`, depth 384 |
| AXI-Lite bundle | `control` | `CTRL` |
| Prediction | `int *pred_out` | `ap_uint<8> *prediction` |
| Logits | 4 x 16 bit, packed in two registers | 4 x 32 bit |
| Extra PL blocks | none | BRAM controller, BRAM, GPIO |
| UART protocol | `ps_uart.c` frames | lab frames |

## Target

Both IPs look the same from the PS:

- same input: the Q8.8 image buffer in DDR that `prepare_cnn_input()` already makes
- same m_axi port and same AXI-Lite registers (input pointer, prediction, logits)
- same block design around the slot: no BRAM, no GPIO

Then `ps_main.c` has one run function, and `select_model()` only loads the other partial bitstream over PCAP. DFX needs this anyway: all modules in one reconfigurable partition must have the same ports.

## What moves into the SNN IP

The two Python functions `image_to_spikes()` and `spikes_to_sparse_regional_features()` become a front end in `snn_mlp.cpp`, before `snn_core()`.

1. **Read image.** 4800 values over m_axi into a local 60x80 buffer, like `ds_cnn` does. No resize, the image is already 80x60.
2. **Sobel.** `sobel_x + sobel_y` is one 3x3 kernel:

   ```
   -2 -2  0
   -2  0  2
    0  2  2
   ```

   OpenCV uses reflected borders (`BORDER_REFLECT_101`), so row -1 is row 1.
3. **Split.** `pos = max(s, 0)`, `neg = max(-s, 0)`.
4. **Threshold.** Python keeps values above the 90th percentile, separately for `pos` and `neg`. The `/255` and `/max` in Python do not change the order, so they can be dropped. With 4800 pixels the rule becomes: spike where the value is greater than the 4320th smallest value. In hardware: histogram of the values, walk it until the count reaches 4320, use that bin as threshold. Two passes over the image.
5. **Grid.** 12x16 cells of 5x5 pixels. Count ON and OFF spikes per cell (0 to 25).
6. **Scale.** Feature is `round(count / 25 * 128)`. A 26 entry table.
7. **Order.** `features[(gy*16 + gx)*2]` is ON, `+1` is OFF. Same as the PC script.

The rest of the IP (`fc1_lif`, `fc2_lif`, `fc3_lif`, argmax) stays as it is.

Interface changes in the top function:

- `feature_mem` (depth 384) becomes `input_img` (depth 4800), same type as in `ds_cnn`
- bundle `CTRL` becomes `control`
- `prediction` becomes `int *pred_out`
- logits get the same width and layout as the CNN's. The SNN sums are 32 bit, so either saturate them to 16 bit or widen the CNN's. Has to be decided.

After synthesis, compare the register offsets in the two generated `x*_hw.h` files. They must match (`0x10` input pointer, `0x1C` prediction, `0x30` logits in the CNN).

## Open points

- The IP gets Q8.8 pixels, Python works on `p/255`. Sobel does not care about the offset and the percentile does not care about the scale, but the Q8.8 rounding can move a few pixels across the threshold. Safest is to make `snn_mlp.py` compute the features from the same Q8.8 values and retrain.
- Step 4 is derived from how `np.percentile` works, not tested yet.
- `snn_mlp_tb.cpp` reads feature files. It needs image files instead, and the feature stage should be checked on its own against `extract_features()` in the PC script.
- PCAP: where the partial bitstreams are stored (in the ELF or on SD) and how the slot is held in reset while it is reconfigured.

## When this is done

- `helloworld.c` and `send_images_uart_snn.py` are no longer needed
- `pc/send_images.py` sends the same raw pixels for both models
- `ps_main.c` runs both models through the same code
