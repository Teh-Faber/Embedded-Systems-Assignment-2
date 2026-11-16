# Lab files

Kept as they were after lab 7 and lab 8. Not used by the assignment build, only for looking things up.
Paths inside the config files and scripts point to the original lab folders.

## lab_7_cnn

- `helloworld.c`, `send_images_uart.py`: PS app and PC script from the CNN lecture repo
- `hls_config.cfg`: HLS component config
- `csynth.rpt`: HLS synthesis report for `ds_cnn`
- `ds_cnn.zip`: exported IP

## lab_8_snn

- `helloworld.c`: PS app, ported to the newer Vitis (base addresses instead of device IDs)
- `send_images_uart_snn.py`: PC script
- `hls/`: config, synthesis report and exported IP for `snn_mlp`
- `vivado/`: block design script, constraints, XSA, timing and utilization reports
- `vitis/`: scripts that create the platform and app
- `model/`: training output (checkpoints, exported weights, plots). Cable dataset, 10 epochs.
