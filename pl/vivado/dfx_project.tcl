# Creates the DFX Vivado project in pl/vivado/dfx/.
#
#   vivado -mode batch -source pl/vivado/dfx_project.tcl
#   (or in the Vivado Tcl console:  source <repo>/pl/vivado/dfx_project.tcl)
#
# The script builds the static block design and one reconfigurable module (RM)
# per HLS IP it finds in pl/ip/. It can be run before the IPs exist: the static
# part is then created with the partition left open. Run it again after an IP
# has been exported. It deletes and recreates the project every time.
#
# See pl/README.md for the design and for what to do after this script.

set script_dir [file dirname [file normalize [info script]]]
set pl_dir     [file dirname $script_dir]
set proj_name  dfx
set proj_dir   [file join $script_dir $proj_name]

set part      xc7z020clg400-1
set fclk_mhz  100
# AXI-Lite address of the partition. ps/ps_main.c uses the same value.
set rp_offset 0x40000000
set rp_range  64K
# RM name -> top-level name of the HLS IP that goes in it.
set modules   {rm_cnn ds_cnn rm_snn snn_mlp}

# ----------------------------------------------------------------------------
# Project
# ----------------------------------------------------------------------------
create_project -force $proj_name $proj_dir -part $part

set board [lindex [get_board_parts -quiet *pynq-z2*] end]
if {$board eq ""} {
    error "PYNQ-Z2 board files are not installed. Install them (Vivado Store or\
           <Vivado>/data/boards/board_files/pynq-z2) and run the script again."
}
set_property board_part $board [current_project]
set_property ip_repo_paths [file join $pl_dir ip] [current_project]
update_ip_catalog
set_property PR_FLOW 1 [current_project]

# ----------------------------------------------------------------------------
# Reconfigurable modules: one block design per HLS IP, all with the same ports
#
#   ap_clk, ap_rst_n     clock and active-low reset
#   s_axi_control        AXI4-Lite slave: start, status, input pointer, results
#   m_axi_gmem           AXI4 master: reads the image from PS DDR
#
# The IP's own interfaces are found by type, not by name, so the bundle names
# in the HLS source do not matter here.
# ----------------------------------------------------------------------------
proc create_rm {name ipdef fclk_mhz} {
    create_bd_design $name

    set clk [create_bd_port -dir I -type clk -freq_hz [expr {$fclk_mhz * 1000000}] ap_clk]
    set rst [create_bd_port -dir I -type rst ap_rst_n]
    set_property CONFIG.POLARITY ACTIVE_LOW $rst

    set ctrl [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 s_axi_control]
    # 32 bits so the module can hold the PS address 0x4000_0000 (DFX aperture check)
    set_property -dict [list CONFIG.PROTOCOL AXI4LITE CONFIG.ADDR_WIDTH 32 CONFIG.DATA_WIDTH 32] $ctrl
    set gmem [create_bd_intf_port -mode Master -vlnv xilinx.com:interface:aximm_rtl:1.0 m_axi_gmem]
    set_property -dict [list CONFIG.PROTOCOL AXI4 CONFIG.ADDR_WIDTH 32 CONFIG.DATA_WIDTH 32] $gmem
    set_property -dict [list CONFIG.ASSOCIATED_BUSIF s_axi_control:m_axi_gmem \
                             CONFIG.ASSOCIATED_RESET ap_rst_n] $clk

    set ip [create_bd_cell -type ip -vlnv $ipdef accel]
    set ip_slave  [get_bd_intf_pins -of_objects $ip -filter {MODE == Slave && VLNV =~ "*:aximm_rtl:*"}]
    set ip_master [get_bd_intf_pins -of_objects $ip -filter {MODE == Master && VLNV =~ "*:aximm_rtl:*"}]
    if {[llength $ip_slave] != 1 || [llength $ip_master] != 1} {
        error "$ipdef must have exactly one AXI4-Lite slave and one AXI master\
               (found slaves: $ip_slave, masters: $ip_master)."
    }
    connect_bd_intf_net $ctrl $ip_slave
    connect_bd_intf_net $ip_master $gmem
    connect_bd_net $clk [get_bd_pins -of_objects $ip -filter {TYPE == clk}]
    connect_bd_net $rst [get_bd_pins -of_objects $ip -filter {TYPE == rst}]

    assign_bd_address
    # Same addresses in every module as in the static design, so the DFX
    # aperture check passes: registers at rp_offset, master sees all of DDR.
    global rp_offset rp_range
    set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces /s_axi_control]]
    set_property range  $rp_range  $seg
    set_property offset $rp_offset $seg
    set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces /accel/Data_m_axi_gmem]]
    set_property range  512M       $seg
    set_property offset 0x00000000 $seg
    validate_bd_design
    save_bd_design
    close_bd_design [current_bd_design]
}

set rm_names {}
foreach {rm top} $modules {
    set ipdef [lindex [get_ipdefs -quiet -filter "NAME == $top"] end]
    if {$ipdef eq ""} {
        puts "DFX: no IP named '$top' in [file join $pl_dir ip]. $rm is skipped."
        continue
    }
    puts "DFX: $rm uses $ipdef"
    create_rm $rm $ipdef $fclk_mhz
    lappend rm_names $rm
}

# ----------------------------------------------------------------------------
# Static block design
# ----------------------------------------------------------------------------
create_bd_design dfx_static

# PS with the board preset (DDR, UART0 on MIO 14/15, clocks), then the ports
# the accelerator needs and one EMIO GPIO bit that decouples the partition.
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7 ps7]
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
    -config {make_external "FIXED_IO, DDR" apply_board_preset "1" Master "Disable" Slave "Disable"} $ps
set_property -dict [list \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ $fclk_mhz \
    CONFIG.PCW_USE_M_AXI_GP0 1 \
    CONFIG.PCW_USE_S_AXI_HP0 1 \
    CONFIG.PCW_GPIO_EMIO_GPIO_ENABLE 1 \
    CONFIG.PCW_GPIO_EMIO_GPIO_IO 1 \
] $ps

# rst_static follows the PS reset. rst_rp is also asserted while the partition
# is decoupled, and releases synchronously a few cycles after it is coupled.
set rst_static [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_static]
set rst_rp     [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_rp]
set_property CONFIG.C_AUX_RESET_HIGH 1 $rst_rp

set sc_ctrl [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect sc_ctrl]
set sc_mem  [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect sc_mem]
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI 1] $sc_ctrl
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI 1] $sc_mem

# DFX decoupler with two interfaces and a plain "decouple" input.
#   control: AXI4-Lite, the partition is the slave
#   gmem:    AXI4, the partition is the master
# The pins are s_<name> towards the static design and rp_<name> towards the
# partition. Which MODE value gives which direction is checked below, and the
# two are swapped if this Vivado version defines it the other way round.
set dec [create_bd_cell -type ip -vlnv xilinx.com:ip:dfx_decoupler decoupler]
proc configure_decoupler {dec control_mode gmem_mode} {
    set intf [list \
        control [list ID 0 VLNV xilinx.com:interface:aximm_rtl:1.0 PROTOCOL axi4lite MODE $control_mode] \
        gmem    [list ID 1 VLNV xilinx.com:interface:aximm_rtl:1.0 PROTOCOL AXI4 MODE $gmem_mode]]
    set_property CONFIG.ALL_PARAMS \
        [list HAS_AXI_LITE 0 HAS_SIGNAL_CONTROL 1 HAS_SIGNAL_STATUS 0 INTF $intf] $dec
}
configure_decoupler $dec slave master
if {[get_property MODE [get_bd_intf_pins decoupler/rp_control]] ne "Master"} {
    configure_decoupler $dec master slave
}
if {[get_property MODE [get_bd_intf_pins decoupler/rp_control]] ne "Master" ||
    [get_property MODE [get_bd_intf_pins decoupler/rp_gmem]] ne "Slave"} {
    error "The DFX decoupler did not get the expected interface directions.\
           Configure it in the GUI as described in pl/README.md."
}

# Clocks and resets
set clk [get_bd_pins ps7/FCLK_CLK0]
connect_bd_net $clk \
    [get_bd_pins ps7/M_AXI_GP0_ACLK] [get_bd_pins ps7/S_AXI_HP0_ACLK] \
    [get_bd_pins rst_static/slowest_sync_clk] [get_bd_pins rst_rp/slowest_sync_clk] \
    [get_bd_pins sc_ctrl/aclk] [get_bd_pins sc_mem/aclk]
connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N] \
    [get_bd_pins rst_static/ext_reset_in] [get_bd_pins rst_rp/ext_reset_in]
connect_bd_net [get_bd_pins rst_static/interconnect_aresetn] \
    [get_bd_pins sc_ctrl/aresetn] [get_bd_pins sc_mem/aresetn]

# Decouple: EMIO GPIO bit 0 (PS GPIO pin 54)
connect_bd_net [get_bd_pins ps7/GPIO_O] \
    [get_bd_pins decoupler/decouple] [get_bd_pins rst_rp/aux_reset_in]

# AXI on the static side of the decoupler
connect_bd_intf_net [get_bd_intf_pins ps7/M_AXI_GP0]    [get_bd_intf_pins sc_ctrl/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins sc_ctrl/M00_AXI]  [get_bd_intf_pins decoupler/s_control]
connect_bd_intf_net [get_bd_intf_pins decoupler/s_gmem] [get_bd_intf_pins sc_mem/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins sc_mem/M00_AXI]   [get_bd_intf_pins ps7/S_AXI_HP0]

if {[llength $rm_names] == 0} {
    save_bd_design
    puts "DFX: static block design created without the partition."
    puts "DFX: export an HLS IP to pl/ip/ (see pl/ip/README.md) and run this script again."
    return
}

# The partition: a block design container that can hold any of the RMs.
set bd_files {}
foreach rm $rm_names {
    lappend bd_files $rm.bd
}
set bd_list [join $bd_files :]
set rp [create_bd_cell -type container -reference [lindex $rm_names 0] rp]
set_property -dict [list \
    CONFIG.ENABLE_DFX true \
    CONFIG.LIST_SYNTH_BD $bd_list \
    CONFIG.LIST_SIM_BD $bd_list \
] $rp

connect_bd_net $clk [get_bd_pins rp/ap_clk]
connect_bd_net [get_bd_pins rst_rp/peripheral_aresetn] [get_bd_pins rp/ap_rst_n]
connect_bd_intf_net [get_bd_intf_pins decoupler/rp_control] [get_bd_intf_pins rp/s_axi_control]
connect_bd_intf_net [get_bd_intf_pins rp/m_axi_gmem]        [get_bd_intf_pins decoupler/rp_gmem]

# Manual DFX apertures, one per interface (as Vivado writes them from the
# rp > Addressing dialog). The first must match rp_offset / rp_range.
set_property APERTURES { {{0x4000_0000 64K}}} [get_bd_intf_pins /rp/s_axi_control]
set_property APERTURES { {{0x0 512M}}} [get_bd_intf_pins /rp/m_axi_gmem]

# Addresses: the accelerator registers at a fixed address for the PS, and all
# of DDR visible to the accelerator's AXI master.
assign_bd_address
set rp_seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces ps7/Data]]
if {[llength $rp_seg] == 1} {
    set_property range  $rp_range  $rp_seg
    set_property offset $rp_offset $rp_seg
} else {
    puts "DFX: WARNING: expected one address segment under ps7/Data, found: $rp_seg"
    puts "DFX: set the accelerator to $rp_offset in the Address Editor."
}

# -assign_dfx_addressing pushes the apertures into every module copy in rp.
validate_bd_design -assign_dfx_addressing

# The decoupler's clock/reset pins only exist after the first validation.
# Unconnected they are tied to 0, so connect them now and validate again.
connect_bd_net $clk [get_bd_pins decoupler/control_aclk] [get_bd_pins decoupler/gmem_aclk]
connect_bd_net [get_bd_pins rst_static/interconnect_aresetn] \
    [get_bd_pins decoupler/control_arstn] [get_bd_pins decoupler/gmem_arstn]
validate_bd_design -assign_dfx_addressing
save_bd_design

# ----------------------------------------------------------------------------
# Wrapper, constraints, output products
# ----------------------------------------------------------------------------
set bd_file [get_files dfx_static.bd]
make_wrapper -files $bd_file -top
set wrapper [glob -nocomplain \
    [file join $proj_dir $proj_name.gen  sources_1 bd dfx_static hdl dfx_static_wrapper.v] \
    [file join $proj_dir $proj_name.srcs sources_1 bd dfx_static hdl dfx_static_wrapper.v]]
add_files -norecurse [lindex $wrapper 0]
set_property top dfx_static_wrapper [current_fileset]
update_compile_order -fileset sources_1

add_files -fileset constrs_1 -norecurse [file join $pl_dir constraints dfx_pblock.xdc]
generate_target all $bd_file

# The PS loads .bin files over PCAP.
set_property STEPS.WRITE_BITSTREAM.ARGS.BIN_FILE true [get_runs impl_1]

# ----------------------------------------------------------------------------
# DFX configurations: one per RM. The first is implemented by impl_1 together
# with the static design. Each of the others gets a child run that reuses the
# placed and routed static design.
# ----------------------------------------------------------------------------
if {[catch {
    set rms [get_reconfig_modules]
    if {[llength $rms] != [llength $rm_names]} {
        error "expected [llength $rm_names] reconfigurable modules, found: $rms"
    }
    set index 0
    foreach rm $rms {
        set config config_$rm
        create_pr_configuration -name $config -partitions [list dfx_static_i/rp:$rm]
        if {$index == 0} {
            set_property PR_CONFIGURATION $config [get_runs impl_1]
        } else {
            set child child_[expr {$index - 1}]_impl_1
            create_run $child -parent_run impl_1 -pr_config $config \
                -flow [get_property FLOW [get_runs impl_1]]
            set_property STEPS.WRITE_BITSTREAM.ARGS.BIN_FILE true [get_runs $child]
        }
        incr index
    }
} message]} {
    puts "DFX: WARNING: the configurations were not created automatically: $message"
    puts "DFX: create them with Tools > Dynamic Function eXchange Wizard (pl/README.md)."
} else {
    puts "DFX: configurations created for: $rms"
}

puts "DFX: project ready in $proj_dir"
puts "DFX: next: Generate Bitstream, then source [file join $script_dir export_dfx.tcl]"
