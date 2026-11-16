# Lab files

From lab 7 and lab 8. Not used by the assignment build, only for looking things up.
Paths in the scripts are relative to the script. The HLS configs moved to `cnn/` and `snn/`.

## lab_7_cnn

- `helloworld.c`, `send_images_uart.py`: PS app and PC script from the CNN lecture repo
- `csynth.rpt`: HLS synthesis report for `ds_cnn`
- `ds_cnn.zip`: exported IP

## lab_8_snn

- `helloworld.c`: PS app, ported to the newer Vitis (base addresses instead of device IDs)
- `send_images_uart_snn.py`: PC script
- `hls/`: synthesis report and exported IP for `snn_mlp`. Unzip `snn_mlp.zip` to `hls/ip/` before running the Vivado script.
- `vivado/`: block design script, constraints, XSA, timing and utilization reports
- `vitis/`: scripts that create the platform and app in `vitis/ws/`
- `model/`: training output (checkpoints, exported weights, plots). Cable dataset, 10 epochs.
