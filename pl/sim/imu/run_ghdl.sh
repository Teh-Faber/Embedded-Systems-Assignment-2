#!/usr/bin/env bash
# Simulate imu_capture with each filter in GHDL and check the results.
#
#   cd pl/sim/imu && ./run_ghdl.sh            # N_SAMPLES = 16 (ring wraps quickly)
#   N=200 WRAPS=1 ./run_ghdl.sh fir iir       # the real window length
#
# Needs GHDL (4.x) and Python with numpy/scipy. Vivado users can run the same
# testbench in xsim; the FIR then still uses fir_compiler_model.vhd.
set -euo pipefail
cd "$(dirname "$0")"
HDL=../../hdl
N=${N:-16}
WRAPS=${WRAPS:-3}
FILTERS=${*:-fir iir none}

python3 gen_tb.py
mkdir -p build/work
OPTS="--std=08 --workdir=build/work -frelaxed"
ghdl -a $OPTS $HDL/iir_coeffs_pkg.vhd $HDL/imu_spi.vhd $HDL/imu_reader.vhd $HDL/iir_mc.vhd \
         $HDL/imu_capture.vhd $HDL/emio_io.vhd build/fir_tb_pkg.vhd fir_compiler_model.vhd \
         lsm6dso32_model.vhd tb_imu_capture.vhd
ghdl -e $OPTS tb_imu_capture

status=0
for f in $FILTERS; do
    log=build/log_${f}_N${N}.txt
    echo "=== $f, N_SAMPLES=$N"
    ghdl -r $OPTS tb_imu_capture -gFILTER=$f -gN_SAMPLES=$N -gWRAPS=$WRAPS -gLOG_FILE=$log \
        --ieee-asserts=disable-at-0 2>&1 | grep -v "^.*(report note): \(start\|clear\|disconnect\|reconnect\)$" | tail -n 20
    python3 check_tb.py $log $f || status=1
done
exit $status
