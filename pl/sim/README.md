# pl/sim

System-level simulation of the DFX static design (`dfx_static`) with the CNN in the partition.

| File | Use |
|---|---|
| `tb_dfx_static.sv` | Testbench. The PS is the Zynq-7000 VIP: it issues the register accesses over GP0 and models DDR behind HP0. |
| `setup_sim.tcl` | Adds the testbench and vectors to `sim_1`, selects the module in `rp`, launches xsim. |
| `vectors/` | `images.mem` (Q8.8 images), `expected.mem` (golden `pred` and logits), `images.txt` (what each vector is). |
| `gen_vectors.py`, `golden_main.cpp` | Make new vectors. The golden model is `cnn/ds_cnn.cpp` compiled with g++, so results are bit-exact with the RTL. |

## Run

1. Open `pl/vivado/dfx/dfx.xpr`.
2. Tcl console: `source <repo>/pl/sim/setup_sim.tcl`
3. The log ends with `TB RESULT: PASS` or `TB RESULT: FAIL (...)`. Each inference prints pred, logits, latency and `OK`/`MISMATCH`.

## What it checks

1. **Bring-up**: resets release, `ap_idle` is set, the `input_img` registers read back through GP0 → `sc_ctrl` → decoupler → `rp`.
2. **Inference**, per vector: image into DDR, pointer in `input_img`, `ap_start`, poll `ap_done`, then `pred_out`, `pred_out_ap_vld` and the four logits against the golden model, bit for bit.
3. **Decouple/re-couple**: `decouple = 1` resets the partition (`rst_rp`); after `decouple = 0` the registers are back at reset values and an inference still matches. The VIP does not drive EMIO GPIO, so the testbench forces the decouple net that GPIO pin 54 drives on hardware.

The golden values compare against what the CNN predicts, not the ground-truth class: vector 7 is a triangle that the model calls a square, in C simulation and on hardware alike.

## Options

Before `source`:

- `set tb_rm rm_snn`: simulate the SNN in the partition. Needs vectors for the SNN; the CNN vectors will mismatch.
- `set tb_launch 0`: set up only.

Testbench parameters (Simulation Settings → `xsim.elaborate.xelab.more_options`, e.g. `-generic_top NUM_VECTORS=2`):

- `NUM_VECTORS`: how many vectors to run (0 = all, default).
- `TIMEOUT_US`: per-inference timeout (default 20000).

## New vectors

```
g++ -O2 -std=c++14 -I <Vitis>/include -I cnn cnn/ds_cnn.cpp pl/sim/golden_main.cpp -o golden
python pl/sim/gen_vectors.py --per-class 2 --golden ./golden
```

Uses `data/shapes_dataset/test/` when it exists, otherwise draws images with `tools/generate_shapes.py`. Regenerate the vectors whenever `cnn/ds_cnn.cpp` or the weights change.
