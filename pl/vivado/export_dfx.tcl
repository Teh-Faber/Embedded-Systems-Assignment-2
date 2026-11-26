# Exports what the PS needs from an implemented DFX project (FIR or IIR design).
#
#   vivado -mode batch -source pl/vivado/export_dfx.tcl -tclargs fir
#   vivado -mode batch -source pl/vivado/export_dfx.tcl -tclargs iir
#   (or in the Vivado Tcl console with the project open:  source <repo>/pl/vivado/export_dfx.tcl)
#
# Run it after Generate Bitstream has finished for all implementation runs.
# <f> is fir or iir:
#
#   ps/bitstreams/<f>/cnn_partial.bin   partial bitstream of the CNN module
#   ps/bitstreams/<f>/snn_partial.bin   partial bitstream of the SNN module
#   pl/vivado/dfx_static_<f>.xsa        hardware platform for Vitis, with the full bitstream
#
# A partial bitstream only fits the static design it was implemented with, so
# each filter design has its own set.

set script_dir [file dirname [file normalize [info script]]]
set repo_dir   [file dirname [file dirname $script_dir]]
set rp_cell    dfx_static_i/rp

if {[llength [get_projects -quiet]] == 0} {
    if {[info exists argv] && [llength $argv] > 0} {
        set dfx_filter [lindex $argv 0]
    }
    if {![info exists dfx_filter]} {
        set dfx_filter fir
    }
    open_project [file join $script_dir dfx_$dfx_filter dfx_$dfx_filter.xpr]
}
# The open project decides which design is exported: dfx_fir or dfx_iir.
if {![regexp {^dfx_(fir|iir)$} [get_property NAME [current_project]] -> dfx_filter]} {
    error "Open project [get_property NAME [current_project]] is not dfx_fir or dfx_iir."
}
set out_dir [file join $repo_dir ps bitstreams $dfx_filter]
file mkdir $out_dir
puts "DFX: exporting the $dfx_filter design to $out_dir"

set exported {}
foreach run [get_runs -filter {IS_IMPLEMENTATION == 1}] {
    if {[get_property PROGRESS $run] ne "100%"} {
        puts "DFX: $run is not finished and is skipped."
        continue
    }
    open_run $run
    # The module in this run is named after its block design: rm_cnn or rm_snn.
    set names [get_property REF_NAME [get_cells $rp_cell]]
    append names " " [get_property REF_NAME [get_cells -quiet $rp_cell/accel]]
    if {![regexp {rm_(cnn|snn)} $names -> model]} {
        puts "DFX: cannot tell which module is in $run ($names). Skipped."
        close_design
        continue
    }
    # -cell writes only the partial bitstream of the partition (.bit).
    write_bitstream -force -cell $rp_cell [file join $out_dir ${model}_partial]
    # PCAP on Zynq-7000 needs this .bin format (UG909, "Formatting BIN Files
    # for Delivery to Internal Configuration Ports"). write_bitstream -bin_file
    # gives a .bin that PCAP ignores.
    write_cfgmem -force -format BIN -interface SMAPx32 -disablebitswap \
        -loadbit "up 0x0 [file join $out_dir ${model}_partial.bit]" \
        -file [file join $out_dir ${model}_partial.bin]
    lappend exported $model
    close_design
}

# The XSA carries the full bitstream of impl_1 (static design plus its module).
if {[catch {
    open_run impl_1
    write_hw_platform -fixed -include_bit -force -file [file join $script_dir dfx_static_$dfx_filter.xsa]
    close_design
} message]} {
    puts "DFX: WARNING: XSA not written: $message"
    puts "DFX: use File > Export > Export Hardware (include bitstream) instead."
}

puts "DFX: partial bitstreams written for: $exported"
foreach model {cnn snn} {
    if {$model ni $exported} {
        puts "DFX: WARNING: no partial bitstream for $model."
    }
}
