# Exports what the PS needs from the implemented DFX project.
#
#   vivado -mode batch -source pl/vivado/export_dfx.tcl
#   (or in the Vivado Tcl console with the project open:  source <repo>/pl/vivado/export_dfx.tcl)
#
# Run it after Generate Bitstream has finished for all implementation runs.
#
#   ps/bitstreams/cnn_partial.bin   partial bitstream of the CNN module
#   ps/bitstreams/snn_partial.bin   partial bitstream of the SNN module
#   pl/vivado/dfx_static.xsa        hardware platform for Vitis, with the full bitstream

set script_dir [file dirname [file normalize [info script]]]
set repo_dir   [file dirname [file dirname $script_dir]]
set out_dir    [file join $repo_dir ps bitstreams]
set rp_cell    static_i/rp
file mkdir $out_dir

if {[llength [get_projects -quiet]] == 0} {
    open_project [file join $script_dir dfx dfx.xpr]
}

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
    # -cell writes only the partial bitstream of the partition.
    write_bitstream -force -bin_file -cell $rp_cell [file join $out_dir ${model}_partial]
    lappend exported $model
    close_design
}

# The XSA carries the full bitstream of impl_1 (static design plus its module).
if {[catch {
    open_run impl_1
    write_hw_platform -fixed -include_bit -force -file [file join $script_dir dfx_static.xsa]
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
