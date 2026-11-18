# Adds the system testbench to the DFX project and starts the simulation.
#
# Open pl/vivado/dfx/dfx.xpr, then in the Tcl console:
#   source <repo>/pl/sim/setup_sim.tcl
#
# Optional, before sourcing:
#   set tb_rm      rm_snn   ;# module simulated in the partition (default rm_cnn)
#   set tb_launch  0        ;# only set up, don't launch
#
# Safe to source again: files already in sim_1 are not added twice.
# dfx_project.tcl recreates the project, so source this again after running it.

set sim_dir [file dirname [file normalize [info script]]]
if {![info exists tb_rm]}     { set tb_rm rm_cnn }
if {![info exists tb_launch]} { set tb_launch 1 }

if {[catch {current_project}]} {
    error "Open pl/vivado/dfx/dfx.xpr first."
}

# Testbench and vectors. The .mem files are copied next to xsim by Vivado,
# so the testbench opens them by name.
set files [list \
    [file join $sim_dir tb_dfx_static.sv] \
    [file join $sim_dir vectors images.mem] \
    [file join $sim_dir vectors expected.mem]]
foreach f $files {
    if {![file exists $f]} {
        error "Missing $f. Generate the vectors with pl/sim/gen_vectors.py (see pl/sim/README.md)."
    }
    if {[llength [get_files -quiet -of_objects [get_filesets sim_1] $f]] == 0} {
        add_files -fileset sim_1 -norecurse $f
    }
}
set_property file_type SystemVerilog [get_files -of_objects [get_filesets sim_1] tb_dfx_static.sv]

set_property top tb_dfx_static [get_filesets sim_1]
set_property top_lib xil_defaultlib [get_filesets sim_1]
set_property -name xsim.simulate.runtime          -value all   -objects [get_filesets sim_1]
set_property -name xsim.simulate.log_all_signals  -value false -objects [get_filesets sim_1]

# Which reconfigurable module the rp container simulates.
open_bd_design [get_files dfx_static.bd]
if {[catch {set_property CONFIG.ACTIVE_SIM_BD $tb_rm.bd [get_bd_cells /rp]} msg]} {
    puts "SIM: WARNING: could not select $tb_rm for simulation: $msg"
    puts "SIM: select it in the rp cell's Customize Block dialog (Simulation source)."
} else {
    puts "SIM: rp simulates $tb_rm"
}
save_bd_design
generate_target simulation [get_files dfx_static.bd]
update_compile_order -fileset sim_1

puts "SIM: testbench ready (top tb_dfx_static)."
if {$tb_launch} {
    launch_simulation -simset sim_1 -mode behavioral
}
