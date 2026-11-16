# Rebuilds the SNN block design from vivado_block_diagram.png (steps 22-26).
# Run: vivado -mode batch -source build_snn.tcl
set root   [file normalize [file dirname [info script]]]
set ip_dir "C:/Users/Bluex/Desktop/FPGA/lab_8_SNN/hls_ws/snn_mlp/hls/impl/ip"
set proj   snn_vivado

set_param board.repoPaths [list "C:/Users/Bluex/AppData/Roaming/Xilinx/Vivado/2026.1/xhub/board_store/xilinx_board_store/XilinxBoardStore/Vivado/2026.1/boards"]
create_project $proj $root/$proj -part xc7z020clg400-1 -force
set_property board_part tul.com.tw:pynq-z2:part0:1.0 [current_project]
set_property target_language VHDL [current_project]
set_property ip_repo_paths $ip_dir [current_project]
update_ip_catalog

create_bd_design design_1

# --- Zynq PS + board preset (Block Automation) ---
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7 processing_system7_0]
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
    -config {make_external "FIXED_IO, DDR" apply_board_preset "1" Master "Disable" Slave "Disable"} $ps

# --- IP blocks ---
set snn  [create_bd_cell -type ip -vlnv xilinx.com:hls:snn_mlp:1.0 snn_mlp_0]
set sc   [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect smartconnect_0]
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {3}] $sc
set bc   [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl axi_bram_ctrl_0]
set_property -dict [list CONFIG.SINGLE_PORT_BRAM {1} CONFIG.DATA_WIDTH {32}] $bc
set bram [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen blk_mem_gen_0]
set gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio axi_gpio_0]
set_property -dict [list CONFIG.C_GPIO_WIDTH {1}] $gpio
set rst  [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset proc_sys_reset_0]

# --- AXI connections ---
connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] [get_bd_intf_pins smartconnect_0/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins snn_mlp_0/m_axi_gmem]          [get_bd_intf_pins smartconnect_0/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins smartconnect_0/M00_AXI]        [get_bd_intf_pins axi_bram_ctrl_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins smartconnect_0/M01_AXI]        [get_bd_intf_pins snn_mlp_0/s_axi_CTRL]
connect_bd_intf_net [get_bd_intf_pins smartconnect_0/M02_AXI]        [get_bd_intf_pins axi_gpio_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_bram_ctrl_0/BRAM_PORTA]    [get_bd_intf_pins blk_mem_gen_0/BRAM_PORTA]

# --- External GPIO (timing pin on RPi header pin 11) ---
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:gpio_rtl:1.0 gpio_rtl
connect_bd_intf_net [get_bd_intf_pins axi_gpio_0/GPIO] [get_bd_intf_ports gpio_rtl]

# --- Clock (FCLK_CLK0) and reset ---
set clk [get_bd_pins processing_system7_0/FCLK_CLK0]
connect_bd_net $clk [get_bd_pins processing_system7_0/M_AXI_GP0_ACLK] \
    [get_bd_pins snn_mlp_0/ap_clk] [get_bd_pins smartconnect_0/aclk] \
    [get_bd_pins axi_bram_ctrl_0/s_axi_aclk] [get_bd_pins axi_gpio_0/s_axi_aclk] \
    [get_bd_pins proc_sys_reset_0/slowest_sync_clk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_RESET0_N] [get_bd_pins proc_sys_reset_0/ext_reset_in]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] \
    [get_bd_pins snn_mlp_0/ap_rst_n] [get_bd_pins smartconnect_0/aresetn] \
    [get_bd_pins axi_bram_ctrl_0/s_axi_aresetn] [get_bd_pins axi_gpio_0/s_axi_aresetn]

# --- Address map (PS and SNN master both see the BRAM) ---
set ps_space  [get_bd_addr_spaces processing_system7_0/Data]
set snn_space [get_bd_addr_spaces snn_mlp_0/Data_m_axi_gmem]
set bram_seg  [get_bd_addr_segs axi_bram_ctrl_0/S_AXI/Mem0]
set ctrl_seg  [get_bd_addr_segs snn_mlp_0/s_axi_CTRL/Reg]
set gpio_seg  [get_bd_addr_segs axi_gpio_0/S_AXI/Reg]
assign_bd_address -offset 0x40000000 -range 8K  -target_address_space $ps_space  $bram_seg
assign_bd_address -offset 0x40000000 -range 8K  -target_address_space $snn_space $bram_seg
assign_bd_address -offset 0x40010000 -range 64K -target_address_space $ps_space  $ctrl_seg
assign_bd_address -offset 0x41200000 -range 64K -target_address_space $ps_space  $gpio_seg
# The SNN master only needs the BRAM; keep it out of its own registers and the GPIO.
foreach seg [list $ctrl_seg $gpio_seg] {
    if {[catch {exclude_bd_addr_seg -target_address_space $snn_space $seg} msg]} { puts "exclude note: $msg" }
}
puts "=== ADDRESS MAP ==="
foreach seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces]] {
    puts "  $seg  offset=[get_property OFFSET $seg]  range=[get_property RANGE $seg]  excluded=[get_property EXCLUDED $seg]"
}

validate_bd_design
regenerate_bd_layout
save_bd_design

# --- HDL wrapper as top (step 23) ---
set bd_file [get_files design_1.bd]
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper
set_property top design_1_wrapper [current_fileset]

# --- XDC for GPIO timing pin (step 24) ---
set xdc $root/snn_gpio.xdc
set fh [open $xdc w]
puts $fh {set_property PACKAGE_PIN U7 [get_ports {gpio_rtl_tri_io[0]}]}
puts $fh {set_property IOSTANDARD LVCMOS33 [get_ports {gpio_rtl_tri_io[0]}]}
close $fh
add_files -fileset constrs_1 -norecurse $xdc

# --- Synthesis, implementation, bitstream (step 25) ---
launch_runs impl_1 -to_step write_bitstream -jobs 4
wait_on_run impl_1
puts "SYNTH STATUS: [get_property STATUS [get_runs synth_1]]"
puts "IMPL STATUS:  [get_property STATUS [get_runs impl_1]]"

open_run impl_1
report_utilization -file $root/utilization.rpt
report_timing_summary -file $root/timing.rpt

# --- Export hardware with bitstream (step 26) ---
write_hw_platform -fixed -include_bit -force $root/snn_design.xsa
puts "XSA WRITTEN: $root/snn_design.xsa"
