// ============================================================================
// tb_dfx_static.sv — system-level testbench for the DFX static design
//
// Simulates the whole block design dfx_static (ps7, sc_ctrl, decoupler,
// sc_mem, rst_static, rst_rp and the rp container with its active simulation
// module, rm_cnn by default). The PS is the Zynq-7000 VIP, which Vivado uses
// automatically as the simulation model of processing_system7. The VIP
//   - drives AXI4-Lite transactions out of M_AXI_GP0  (CPU register access)
//   - models DDR behind S_AXI_HP0                     (accelerator reads it)
//
// Tests
//   1. Bring-up      reset sequence; ap_idle set; INPUT_IMG register read-back
//                    through GP0 -> sc_ctrl -> decoupler -> rp.
//   2. Inference     for every vector: image into DDR, program the pointer,
//                    ap_start, poll ap_done, compare pred and the 4 logits
//                    bit-exact against the C golden model. Reports latency.
//   3. Decoupling    decouple = 1 puts the partition in reset (rst_rp);
//                    after re-coupling the registers are back at reset values
//                    and one more inference still gives the golden result.
//                    (EMIO GPIO is not driven by the VIP, so the testbench
//                    forces the net ps7_GPIO_O the way GPIO pin 54 would.)
//
// Vectors (from pl/sim/gen_vectors.py), read with $readmemh:
//   images.mem    4800 x 16-bit Q8.8 words per image
//   expected.mem  6 words per image: gt pred logit0 logit1 logit2 logit3
// pl/sim/setup_sim.tcl adds them to sim_1 so Vivado copies them next to xsim.
//
// Ends with "TB RESULT: PASS" or "TB RESULT: FAIL (<n> errors)".
// ============================================================================
`timescale 1ns / 1ps

module tb_dfx_static;

    // ------------------------------------------------------------------
    // Parameters
    // ------------------------------------------------------------------
    parameter int    NUM_VECTORS   = 0;            // 0 = all in expected.mem
    parameter int    TIMEOUT_US    = 20000;        // per inference
    parameter string IMAGES_FILE   = "images.mem";
    parameter string EXPECTED_FILE = "expected.mem";

    localparam logic [31:0] RP_BASE  = 32'h4000_0000;   // dfx_project.tcl rp_offset
    localparam logic [31:0] IMG_BASE = 32'h0010_0000;   // DDR buffer for the images
    localparam logic [31:0] IMG_STEP = 32'h0000_4000;   // 16 KB per image
    localparam int    N_IN       = 60 * 80;
    localparam int    N_CLASSES  = 4;
    localparam int    MAX_VEC    = 64;
    localparam real   FCLK_NS    = 10.0;               // FCLK0 = 100 MHz

    // ds_cnn s_axi_control registers (pl/ip/ds_cnn/.../xds_cnn_hw.h)
    localparam logic [31:0] REG_AP_CTRL     = 32'h00;
    localparam logic [31:0] REG_INPUT_IMG_L = 32'h10;
    localparam logic [31:0] REG_INPUT_IMG_H = 32'h14;
    localparam logic [31:0] REG_PRED_OUT    = 32'h1C;
    localparam logic [31:0] REG_PRED_CTRL   = 32'h20;
    localparam logic [31:0] REG_LOGITS      = 32'h30;

    // ------------------------------------------------------------------
    // PS clock and resets (FIXED_IO), DUT
    // ------------------------------------------------------------------
    logic ps_clk = 1'b0;
    logic ps_porb = 1'b0;
    logic ps_srstb = 1'b0;
    always #15 ps_clk = ~ps_clk;                   // 33.333 MHz PS_CLK

    wire ps_clk_w   = ps_clk;
    wire ps_porb_w  = ps_porb;
    wire ps_srstb_w = ps_srstb;

    dfx_static_wrapper dut (
        .FIXED_IO_ps_clk   (ps_clk_w),
        .FIXED_IO_ps_porb  (ps_porb_w),
        .FIXED_IO_ps_srstb (ps_srstb_w)
        // DDR_* and the rest of FIXED_IO are left open: the VIP models DDR.
    );

    // Hierarchical shortcuts into the block design
    `define PS   dut.dfx_static_i.ps7.inst
    `define BD   dut.dfx_static_i

    // Decouple: EMIO GPIO bit 0 (net ps7_GPIO_O in the generated block design)
    // drives decoupler/decouple and rst_rp/aux_reset_in. Both of those cells
    // are VHDL models, which xsim cannot force from SystemVerilog, so the
    // Verilog net that feeds them is forced instead.
    logic decouple = 1'b0;
    initial force `BD.ps7_GPIO_O = decouple;

    wire fclk     = `BD.ps7_FCLK_CLK0;
    wire rp_rst_n = `BD.rst_rp_peripheral_aresetn[0];

    // ------------------------------------------------------------------
    // Event times for the report figures (pl/sim/waves.tcl reads them from
    // wave_markers.tcl, written at the end of the run). All in ns.
    // ------------------------------------------------------------------
    realtime t_inf_start = 0, t_inf_done = 0;      // first inference
    realtime t_fetch_first = 0, t_fetch_last = 0;  // its image fetch on m_axi_gmem
    realtime t_dec_on = 0, t_rst_on = 0;           // decouple = 1, rp reset asserted
    realtime t_dec_off = 0, t_rst_off = 0;         // decouple = 0, rp reset released
    bit      capture = 0;

    always @(posedge fclk) if (capture) begin
        if (`BD.rp_m_axi_gmem_ARVALID === 1'b1 && t_fetch_first == 0) t_fetch_first = $realtime;
        if (`BD.rp_m_axi_gmem_RVALID === 1'b1 && `BD.rp_m_axi_gmem_RREADY === 1'b1) t_fetch_last = $realtime;
    end

    // ------------------------------------------------------------------
    // Vectors
    // ------------------------------------------------------------------
    logic [15:0] images   [0:MAX_VEC*N_IN-1];
    logic [15:0] expected [0:MAX_VEC*6-1];
    int n_vec;

    // ------------------------------------------------------------------
    // Bookkeeping
    // ------------------------------------------------------------------
    int errors = 0;
    int checks = 0;

    task automatic fail(input string msg);
        errors++;
        $display("[%0t] ERROR: %s", $time, msg);
    endtask

    task automatic check32(input string what, input logic [31:0] got, input logic [31:0] exp);
        checks++;
        if (got !== exp) fail($sformatf("%s: got 0x%08h, expected 0x%08h", what, got, exp));
    endtask

    // ------------------------------------------------------------------
    // AXI helpers (VIP M_AXI_GP0)
    // ------------------------------------------------------------------
    task automatic rp_write(input logic [31:0] off, input logic [31:0] data);
        logic [1:0] resp;
        `PS.write_data(RP_BASE + off, 8'd4, {992'b0, data}, resp);
        if (resp !== 2'b00) fail($sformatf("write 0x%08h: AXI response %0d", RP_BASE + off, resp));
    endtask

    task automatic rp_read(input logic [31:0] off, output logic [31:0] data);
        logic [1:0]    resp;
        logic [1023:0] rd;
        `PS.read_data(RP_BASE + off, 8'd4, rd, resp);
        data = rd[31:0];
        if (resp !== 2'b00) fail($sformatf("read 0x%08h: AXI response %0d", RP_BASE + off, resp));
    endtask

    // Image k -> DDR at addr, in 64-byte chunks, then read back two chunks.
    task automatic load_image(input int k, input logic [31:0] addr);
        logic [1023:0] chunk, back;
        for (int c = 0; c < N_IN / 32; c++) begin
            chunk = '0;
            for (int j = 0; j < 32; j++)
                chunk[16*j +: 16] = images[k*N_IN + c*32 + j];
            `PS.write_mem(chunk, addr + c*64, 8'd64);
        end
        for (int c = 0; c < N_IN / 32; c += N_IN / 32 - 1) begin
            `PS.read_mem(addr + c*64, 8'd64, back);
            for (int j = 0; j < 32; j++)
                if (back[16*j +: 16] !== images[k*N_IN + c*32 + j]) begin
                    fail($sformatf("DDR read-back of image %0d word %0d: got %h, expected %h",
                                   k, c*32 + j, back[16*j +: 16], images[k*N_IN + c*32 + j]));
                    return;
                end
        end
    endtask

    function automatic real q88(input logic [15:0] v);
        return $itor($signed(v)) / 256.0;
    endfunction

    // One inference on image k at addr; compares against the golden values.
    task automatic run_inference(input int k, input logic [31:0] addr, input string tag);
        logic [31:0] st, pred, pvld, w0, w1;
        logic [15:0] lg [N_CLASSES];
        realtime     t0, t1;
        bit          done = 0;
        bit          ok;

        rp_write(REG_INPUT_IMG_L, addr);
        rp_write(REG_INPUT_IMG_H, 32'h0);

        t0 = $realtime;
        if (tag == "infer" && k == 0) capture = 1;
        rp_write(REG_AP_CTRL, 32'h1);                              // ap_start

        fork
            begin
                do begin
                    rp_read(REG_AP_CTRL, st);
                end while (st[1] !== 1'b1);                         // ap_done (COR)
                done = 1;
            end
            #(TIMEOUT_US * 1us);
        join_any
        disable fork;
        t1 = $realtime;
        if (capture) begin
            capture     = 0;
            t_inf_start = t0;
            t_inf_done  = t1;
            $display("[%0t] image fetch on m_axi_gmem: %0.2f us (first AR to last R beat), compute %0.1f us",
                     $time, (t_fetch_last - t_fetch_first) / 1us, (t1 - t_fetch_last) / 1us);
        end

        if (!done) begin
            fail($sformatf("%s image %0d: no ap_done within %0d us (AP_CTRL=0x%08h)",
                           tag, k, TIMEOUT_US, st));
            return;
        end

        rp_read(REG_PRED_OUT,     pred);
        rp_read(REG_PRED_CTRL,    pvld);
        rp_read(REG_LOGITS,       w0);
        rp_read(REG_LOGITS + 4,   w1);
        lg[0] = w0[15:0]; lg[1] = w0[31:16]; lg[2] = w1[15:0]; lg[3] = w1[31:16];

        checks++;
        if (pvld[0] !== 1'b1) fail($sformatf("%s image %0d: pred_out_ap_vld not set", tag, k));

        ok = (pred[7:0] === expected[k*6+1][7:0]);
        check32($sformatf("%s image %0d pred", tag, k), pred, {24'b0, expected[k*6+1][7:0]});
        for (int c = 0; c < N_CLASSES; c++) begin
            check32($sformatf("%s image %0d logit%0d", tag, k, c), {16'b0, lg[c]}, {16'b0, expected[k*6+2+c]});
            ok &= (lg[c] === expected[k*6+2+c]);
        end

        $display("[%0t] %s image %0d: gt=%0d pred=%0d (golden %0d) logits=[%0.3f %0.3f %0.3f %0.3f] %0.1f us (~%0d cycles) %s",
                 $time, tag, k, expected[k*6][7:0], pred, expected[k*6+1][7:0],
                 q88(lg[0]), q88(lg[1]), q88(lg[2]), q88(lg[3]),
                 (t1 - t0) / 1us, int'((t1 - t0) / FCLK_NS), ok ? "OK" : "MISMATCH");
    endtask

    // Marker times for pl/sim/waves.tcl, written next to xsim (run directory).
    task automatic write_markers();
        int fd = $fopen("wave_markers.tcl", "w");
        if (fd == 0) begin $display("could not write wave_markers.tcl"); return; end
        $fdisplay(fd, "set wm(inf_start)   %0.0f", t_inf_start);
        $fdisplay(fd, "set wm(fetch_first) %0.0f", t_fetch_first);
        $fdisplay(fd, "set wm(fetch_last)  %0.0f", t_fetch_last);
        $fdisplay(fd, "set wm(inf_done)    %0.0f", t_inf_done);
        $fdisplay(fd, "set wm(dec_on)      %0.0f", t_dec_on);
        $fdisplay(fd, "set wm(rst_on)      %0.0f", t_rst_on);
        $fdisplay(fd, "set wm(dec_off)     %0.0f", t_dec_off);
        $fdisplay(fd, "set wm(rst_off)     %0.0f", t_rst_off);
        $fclose(fd);
    endtask

    // ------------------------------------------------------------------
    // Test sequence
    // ------------------------------------------------------------------
    initial begin
        logic [31:0] v;
        int          t_rst;

        // Vectors
        for (int i = 0; i < MAX_VEC*6; i++) expected[i] = 'x;
        $readmemh(IMAGES_FILE,   images);
        $readmemh(EXPECTED_FILE, expected);
        n_vec = 0;
        while (n_vec < MAX_VEC && expected[n_vec*6+5] !== 16'hxxxx) n_vec++;
        if (NUM_VECTORS > 0 && NUM_VECTORS < n_vec) n_vec = NUM_VECTORS;
        // Short run for pl/sim/waves.tcl: one image only, when the file
        // wave_short.flag exists in the xsim run directory (or +WAVE_SHORT).
        begin
            int fdf;
            fdf = $fopen("wave_short.flag", "r");
            if (fdf != 0 || $test$plusargs("WAVE_SHORT")) begin
                if (fdf != 0) $fclose(fdf);
                n_vec = 1;
                $display("[%0t] wave_short.flag found: short run with 1 image", $time);
            end
        end
        if (n_vec == 0) $fatal(1, "No vectors in %s. Run pl/sim/gen_vectors.py and pl/sim/setup_sim.tcl.", EXPECTED_FILE);
        $display("[%0t] %0d test vectors", $time, n_vec);

        // ---------------- 1. Bring-up ----------------
        $display("\n===== 1. Bring-up =====");
        repeat (20) @(posedge ps_clk);
        ps_porb = 1'b1;
        repeat (20) @(posedge ps_clk);
        ps_srstb = 1'b1;
        `PS.set_debug_level_info(0);
        `PS.fpga_soft_reset(32'h1);
        repeat (20) @(posedge ps_clk);
        `PS.fpga_soft_reset(32'h0);
        repeat (200) @(posedge fclk);                  // proc_sys_reset releases

        checks++;
        if (rp_rst_n !== 1'b1) fail("rp reset (rst_rp/peripheral_aresetn) not released after PS reset");

        rp_read(REG_AP_CTRL, v);
        checks++;
        if (v[2] !== 1'b1) fail($sformatf("ap_idle not set after reset (AP_CTRL=0x%08h)", v));
        else $display("[%0t] AP_CTRL=0x%08h: ap_idle set", $time, v);

        rp_write(REG_INPUT_IMG_L, 32'hA5A5_5A5C);
        rp_read (REG_INPUT_IMG_L, v);
        check32("INPUT_IMG_L read-back", v, 32'hA5A5_5A5C);
        rp_write(REG_INPUT_IMG_H, 32'h1234_5678);
        rp_read (REG_INPUT_IMG_H, v);
        check32("INPUT_IMG_H read-back", v, 32'h1234_5678);
        $display("[%0t] register path GP0 -> sc_ctrl -> decoupler -> rp: %s",
                 $time, errors == 0 ? "OK" : "FAILED");

        // ---------------- 2. Inference ----------------
        $display("\n===== 2. Inference (%0d images) =====", n_vec);
        for (int k = 0; k < n_vec; k++) begin
            load_image(k, IMG_BASE + k*IMG_STEP);
            run_inference(k, IMG_BASE + k*IMG_STEP, "infer");
        end

        // ---------------- 3. Decoupling ----------------
        $display("\n===== 3. Decouple / re-couple =====");
        rp_write(REG_INPUT_IMG_L, 32'hDEAD_BEE0);      // marker that reset must clear

        decouple = 1'b1;
        t_dec_on = $realtime;
        t_rst = 0;
        while (rp_rst_n !== 1'b0 && t_rst < 100) begin @(posedge fclk); t_rst++; end
        checks++;
        t_rst_on = $realtime;
        if (rp_rst_n !== 1'b0) fail("decouple=1 did not put the partition in reset");
        else $display("[%0t] decouple=1: rp in reset after %0d cycles", $time, t_rst);
        repeat (100) @(posedge fclk);

        decouple = 1'b0;
        t_dec_off = $realtime;
        t_rst = 0;
        while (rp_rst_n !== 1'b1 && t_rst < 1000) begin @(posedge fclk); t_rst++; end
        checks++;
        t_rst_off = $realtime;
        if (rp_rst_n !== 1'b1) fail("rp reset not released after decouple=0");
        else $display("[%0t] decouple=0: rp reset released after %0d cycles", $time, t_rst);
        repeat (20) @(posedge fclk);

        rp_read(REG_INPUT_IMG_L, v);
        check32("INPUT_IMG_L after re-couple (reset value)", v, 32'h0);
        rp_read(REG_AP_CTRL, v);
        checks++;
        if (v[2] !== 1'b1) fail($sformatf("ap_idle not set after re-couple (AP_CTRL=0x%08h)", v));

        run_inference(0, IMG_BASE, "recouple");

        // ---------------- Result ----------------
        $display("\n==================================================");
        if (errors == 0) $display("TB RESULT: PASS (%0d checks)", checks);
        else             $display("TB RESULT: FAIL (%0d errors in %0d checks)", errors, checks);
        $display("==================================================\n");
        write_markers();
        $finish;
    end

    // Safety net: the whole run
    initial begin
        wait (n_vec > 0);
        #((n_vec + 2) * TIMEOUT_US * 1us + 10ms);
        $display("TB RESULT: FAIL (global timeout)");
        $finish;
    end

endmodule
