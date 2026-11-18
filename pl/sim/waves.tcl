# Wave windows for the report figures.
#
# With the simulation from setup_sim.tcl open, in the Vivado Tcl console:
#   source <repo>/pl/sim/waves.tcl
#
# It loads the testbench, restarts it at time 0 with the signals below in two
# wave windows, and runs a short simulation (1 image + the decouple test,
# ~2 ms of sim time). Expect two short runs, a few minutes each. It opens:
#   fig1_inference   one whole inference: register access, image fetch, done
#   fig2_decouple    decouple -> partition reset -> re-couple -> registers reset
# Each tab has named markers at the events. To frame a figure: Zoom Fit, then
# drag a box between two markers on the time ruler.
#
# Set wv_all 1 before sourcing to keep all 8 images (full ~17 min run).

if {![info exists wv_all]} { set wv_all 0 }

set TB  /tb_dfx_static
set BD  $TB/dut/dfx_static_i
set ACC $BD/rp/accel/inst

set proj_dir [get_property DIRECTORY [current_project]]
set sim_run  [file join $proj_dir [current_project].sim sim_1 behav xsim]
set marker_file [file join $sim_run wave_markers.tcl]

# ---------------------------------------------------------------- re-run
# A flag file in the xsim run directory makes the testbench run one image.
# (Launch options are not used for this: on Windows Vivado's .bat wrappers
# mangle them, and relaunch_sim ignored them.)
set fs   [get_filesets sim_1]
set flag [file join $sim_run wave_short.flag]
set_property -name xsim.elaborate.xelab.more_options -value ""  -objects $fs
set_property -name xsim.simulate.xsim.more_options   -value ""  -objects $fs
set_property -name xsim.simulate.runtime             -value all -objects $fs
if {$wv_all} { file delete -force $flag } else { close [open $flag w] }

# Load the current testbench. This also runs it once (short with the flag).
if {[catch {current_sim} cs] || $cs eq ""} {
    launch_simulation -simset sim_1 -mode behavioral
} else {
    relaunch_sim
}
# Back to time 0; the waves added below are then logged for the whole run.
restart
file delete -force $marker_file

# add one wave; falls back if this Vivado lacks -name/-color
proc w {cfg sig args} {
    if {[catch {add_wave -into $cfg {*}$args $sig}]} {
        set plain {}
        foreach {k v} $args { if {$k eq "-radix"} { lappend plain $k $v } }
        add_wave -into $cfg {*}$plain $sig
    }
}
proc div {cfg text} { add_wave_divider -into $cfg $text }

# ---------------------------------------------------------------- fig 1
set c1 [create_wave_config fig1_inference]
div $c1 "Partition reset"
w $c1 $BD/rst_rp_peripheral_aresetn -name rp_ap_rst_n
div $c1 "Accelerator handshake (ap_ctrl_hs)"
w $c1 $ACC/ap_start -name ap_start -color green
w $c1 $ACC/ap_idle  -name ap_idle
w $c1 $ACC/ap_done  -name ap_done  -color red
div $c1 "PS -> rp registers (AXI4-Lite, rp side of decoupler)"
w $c1 $BD/decoupler_rp_control_AWVALID -name ctrl_AWVALID
w $c1 $BD/decoupler_rp_control_AWADDR  -name ctrl_AWADDR -radix hex
w $c1 $BD/decoupler_rp_control_WDATA   -name ctrl_WDATA  -radix hex
w $c1 $BD/decoupler_rp_control_ARVALID -name ctrl_ARVALID
w $c1 $BD/decoupler_rp_control_ARADDR  -name ctrl_ARADDR -radix hex
w $c1 $BD/decoupler_rp_control_RDATA   -name ctrl_RDATA  -radix hex
div $c1 "rp -> DDR image fetch (AXI4 m_axi_gmem)"
w $c1 $BD/rp_m_axi_gmem_ARVALID -name gmem_ARVALID -color yellow
w $c1 $BD/rp_m_axi_gmem_ARREADY -name gmem_ARREADY
w $c1 $BD/rp_m_axi_gmem_ARADDR  -name gmem_ARADDR -radix hex
w $c1 $BD/rp_m_axi_gmem_ARLEN   -name gmem_ARLEN  -radix unsigned
w $c1 $BD/rp_m_axi_gmem_RVALID  -name gmem_RVALID -color yellow
w $c1 $BD/rp_m_axi_gmem_RREADY  -name gmem_RREADY
w $c1 $BD/rp_m_axi_gmem_RLAST   -name gmem_RLAST
w $c1 $BD/rp_m_axi_gmem_RDATA   -name gmem_RDATA  -radix hex

# ---------------------------------------------------------------- fig 2
set c2 [create_wave_config fig2_decouple]
w $c2 $BD/ps7_FCLK_CLK0 -name FCLK0_100MHz
div $c2 "Decouple control (EMIO GPIO 54)"
w $c2 $BD/ps7_GPIO_O -name decouple -color red
w $c2 $BD/rst_rp_peripheral_aresetn -name rp_ap_rst_n -color orange
div $c2 "Accelerator state"
w $c2 $ACC/ap_idle -name ap_idle
div $c2 "Register access, static side (sc_ctrl -> decoupler)"
w $c2 $BD/sc_ctrl_M00_AXI_AWVALID -name static_AWVALID
w $c2 $BD/sc_ctrl_M00_AXI_ARVALID -name static_ARVALID
div $c2 "Register access, partition side (decoupler -> rp)"
w $c2 $BD/decoupler_rp_control_AWVALID -name rp_AWVALID
w $c2 $BD/decoupler_rp_control_AWADDR  -name rp_AWADDR -radix hex
w $c2 $BD/decoupler_rp_control_WDATA   -name rp_WDATA  -radix hex
w $c2 $BD/decoupler_rp_control_ARVALID -name rp_ARVALID
w $c2 $BD/decoupler_rp_control_ARADDR  -name rp_ARADDR -radix hex
w $c2 $BD/decoupler_rp_control_RVALID  -name rp_RVALID
w $c2 $BD/decoupler_rp_control_RDATA   -name rp_RDATA  -radix hex

# ---------------------------------------------------------------- run
run all
file delete -force $flag

# ---------------------------------------------------------------- markers
if {![file exists $marker_file]} {
    puts "WAVES: $marker_file not found - did the run reach TB RESULT?"
    return
}
source $marker_file
proc mk {cfg name t} { if {$t > 0} { add_wave_marker -into $cfg -name $name ${t}ns } }

mk $c1 "ap_start"          $wm(inf_start)
mk $c1 "fetch start"       $wm(fetch_first)
mk $c1 "fetch end"         $wm(fetch_last)
mk $c1 "ap_done seen"      $wm(inf_done)

mk $c2 "decouple=1"        $wm(dec_on)
mk $c2 "rp reset asserted" $wm(rst_on)
mk $c2 "decouple=0"        $wm(dec_off)
mk $c2 "rp reset released" $wm(rst_off)

# Save both views so they can be reopened without re-running
foreach {cfg name} [list $c1 fig1_inference $c2 fig2_decouple] {
    catch {save_wave_config -object $cfg [file join [file dirname [info script]] $name.wcfg]}
}

puts [format "WAVES: fig1 inference %.1f us (fetch %.2f us), fig2 decouple %.0f-%.0f ns" \
    [expr {($wm(inf_done) - $wm(inf_start)) / 1000.0}] \
    [expr {($wm(fetch_last) - $wm(fetch_first)) / 1000.0}] \
    [expr {$wm(dec_on) - 200}] [expr {$wm(rst_off) + 2000}]]
puts "WAVES: in each tab: Zoom Fit, then drag between markers to frame the figure."
