# Pins of the static IMU front end (imu_capture.vhd), PYNQ-Z2.
#
# LSM6DSO32 breakout on PMOD JB, top row, wired as in lab 6:
#   JB1 = CS, JB2 = SDO (MISO), JB3 = SDA (MOSI), JB4 = SCL (SCLK)
set_property -dict {PACKAGE_PIN W14 IOSTANDARD LVCMOS33} [get_ports imu_cs_n]
set_property -dict {PACKAGE_PIN Y14 IOSTANDARD LVCMOS33} [get_ports imu_miso]
set_property -dict {PACKAGE_PIN T11 IOSTANDARD LVCMOS33} [get_ports imu_mosi]
set_property -dict {PACKAGE_PIN T10 IOSTANDARD LVCMOS33} [get_ports imu_sclk]
# With no sensor the bus reads 0xFF: WHO_AM_I fails and STATUS_REG is
# rejected, so an unplugged sensor never produces samples.
set_property PULLUP true [get_ports imu_miso]

# SW1 selects live inference (high) or PC-driven debug inference (low)
set_property -dict {PACKAGE_PIN M19 IOSTANDARD LVCMOS33} [get_ports sw1]

# LD0 heartbeat (toggles every half second while samples arrive)
# LD1 sensor configured and answering, LD2 SW1, LD3 partition decoupled
set_property -dict {PACKAGE_PIN R14 IOSTANDARD LVCMOS33} [get_ports {led[0]}]
set_property -dict {PACKAGE_PIN P14 IOSTANDARD LVCMOS33} [get_ports {led[1]}]
set_property -dict {PACKAGE_PIN N16 IOSTANDARD LVCMOS33} [get_ports {led[2]}]
set_property -dict {PACKAGE_PIN M14 IOSTANDARD LVCMOS33} [get_ports {led[3]}]

# SPI runs at 1 MHz from registers and MISO/SW1 go through two-flop
# synchronizers, so none of these pins needs I/O timing.
set_false_path -from [get_ports {imu_miso sw1}]
set_false_path -to [get_ports {imu_cs_n imu_sclk imu_mosi {led[*]}}]
