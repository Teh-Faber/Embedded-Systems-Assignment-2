----------------------------------------------------------------------------------
-- tb_imu_capture (GHDL / xsim)
--
--   lsm6dso32_model <--SPI--> imu_capture <--AXIS--> filter <--+
--                                  |                            |
--                                  +--> BRAM model --> monitor --> log file
--
-- FILTER selects the filter cell: "fir" (fir_compiler_model, the stand-in for
-- the FIR Compiler IP), "iir" (iir_mc, the real RTL) or "none" (bypass).
--
-- Scenario:
--   1. reset, run until the ring has wrapped WRAPS times
--   2. SW1 on: sw1_sync and STATUS bit 1 follow, SW1 off again
--   3. capture_clear: COUNT, timestamps and filter state start again
--   4. sensor unplugged for three watchdog periods: STATUS shows the sensor as
--      not ok and counts retries; plugged in again: samples continue
--   5. header words, sensor configuration and SPI protocol errors are checked
-- Every committed sample (each write of COUNT) goes to LOG_FILE with its raw
-- and filtered values. check_tb.py compares them with filters/filter_model.py
-- bit for bit, and checks that no sensor sample was skipped or repeated.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.fir_tb_pkg.all;
use std.textio.all;
use std.env.all;

entity tb_imu_capture is
    generic (
        FILTER    : string   := "fir";
        N_SAMPLES : positive := 16;
        WRAPS     : positive := 3;
        STIM_FILE : string   := "build/stim.txt";
        LOG_FILE  : string   := "build/log.txt"
    );
end tb_imu_capture;

architecture sim of tb_imu_capture is
    constant CLK_PERIOD  : time := 10 ns;
    constant WATCHDOG_MS : positive := 3;
    constant ODR_PERIOD  : time := 400 us;

    function filter_id(f : string) return natural is
    begin
        if f = "fir" then return 1; elsif f = "iir" then return 2; else return 0; end if;
    end function;
    function tdata_w(f : string) return positive is
    begin
        if f = "fir" then return 32; else return 16; end if;
    end function;
    function shift(f : string) return natural is
    begin
        if f = "fir" then return 15; else return 0; end if;
    end function;

    -- zero samples after a filter reset; dfx_project.tcl sets taps - 1 for the FIR
    function flush(f : string) return natural is
    begin
        if f = "fir" then return FIR_TB_COEFFS'length - 1; else return 0; end if;
    end function;

    constant FID   : natural  := filter_id(FILTER);
    constant TW    : positive := tdata_w(FILTER);
    constant SLOTS : positive := N_SAMPLES + 1;
    constant RING  : natural  := ((12 * SLOTS + 255) / 256) * 256;
    constant F_OFF : natural  := 256;
    constant R_OFF : natural  := F_OFF + RING;
    constant T_OFF : natural  := R_OFF + RING;

    signal clk           : std_logic := '0';
    signal aresetn       : std_logic := '0';
    signal capture_clear : std_logic := '0';
    signal sw1           : std_logic := '0';
    signal sw1_sync      : std_logic;
    signal led           : std_logic_vector(3 downto 0);
    signal cs_n, sclk, mosi, miso : std_logic;
    signal connected     : std_logic := '1';

    signal raw_tdata     : std_logic_vector(15 downto 0);
    signal raw_tvalid, raw_tready, raw_tlast : std_logic;
    signal filt_tdata    : std_logic_vector(TW - 1 downto 0) := (others => '0');
    signal filt_tvalid, filt_tlast : std_logic := '0';
    signal filt_tready   : std_logic;
    signal filt_aresetn  : std_logic;

    signal bram_clk, bram_rst, bram_en : std_logic;
    signal bram_we   : std_logic_vector(3 downto 0);
    signal bram_addr : std_logic_vector(31 downto 0);
    signal bram_din  : std_logic_vector(31 downto 0);

    type mem_t is array (0 to 4095) of std_logic_vector(31 downto 0);
    signal mem : mem_t := (others => x"DEADBEEF");

    signal produced  : natural;
    signal model_err : natural;
    signal ctrl1, ctrl2, ctrl3 : std_logic_vector(7 downto 0);
    signal committed : natural := 0;      -- samples since the last capture reset
    signal resets    : natural := 0;      -- header initialisations seen
    signal mon_err   : natural := 0;
    signal done      : boolean := false;

    file logf : text open write_mode is LOG_FILE;

    function hex(v : std_logic_vector(31 downto 0)) return string is
        constant H : string(1 to 16) := "0123456789ABCDEF";
        variable s : string(1 to 8);
    begin
        for i in 0 to 7 loop
            s(i + 1) := H(to_integer(unsigned(v(31 - 4 * i downto 28 - 4 * i))) + 1);
        end loop;
        return s;
    end function;
begin
    clk <= not clk after CLK_PERIOD / 2 when not done else '0';

    dut : entity work.imu_capture
        generic map (
            IMU_ODR_HZ => 104, N_SAMPLES => N_SAMPLES, CTRL1_XL => 16#40#, CTRL2_G => 16#44#,
            FILTER_ID => FID, FILT_TDATA_W => TW, FILT_DATA_W => TW, FILT_SHIFT => shift(FILTER),
            FILT_FLUSH => flush(FILTER),
            MEM_BYTES => 16384, BOOT_US => 50, SWRESET_US => 10, POLL_US => 20,
            WATCHDOG_MS => WATCHDOG_MS, DISCARD => 2, FILT_TIMEOUT_US => 200,
            REFRESH_US => 200, DEBOUNCE_US => 20
        )
        port map (
            aclk => clk, aresetn => aresetn, capture_clear => capture_clear, decouple => '0',
            sw1 => sw1, sw1_sync => sw1_sync, led => led,
            imu_cs_n => cs_n, imu_sclk => sclk, imu_mosi => mosi, imu_miso => miso,
            m_axis_raw_tdata => raw_tdata, m_axis_raw_tvalid => raw_tvalid,
            m_axis_raw_tready => raw_tready, m_axis_raw_tlast => raw_tlast,
            s_axis_filt_tdata => filt_tdata, s_axis_filt_tvalid => filt_tvalid,
            s_axis_filt_tready => filt_tready, s_axis_filt_tlast => filt_tlast,
            filt_aresetn => filt_aresetn,
            bram_clk => bram_clk, bram_rst => bram_rst, bram_en => bram_en, bram_we => bram_we,
            bram_addr => bram_addr, bram_din => bram_din, bram_dout => (others => '0')
        );

    g_fir : if FILTER = "fir" generate
        u_fir : entity work.fir_compiler_model
            port map (
                aclk => clk, aresetn => filt_aresetn,
                s_axis_data_tdata => raw_tdata, s_axis_data_tvalid => raw_tvalid,
                s_axis_data_tready => raw_tready, s_axis_data_tlast => raw_tlast,
                m_axis_data_tdata => filt_tdata, m_axis_data_tvalid => filt_tvalid,
                m_axis_data_tlast => filt_tlast
            );
    end generate;
    g_iir : if FILTER = "iir" generate
        u_iir : entity work.iir_mc
            port map (
                aclk => clk, aresetn => filt_aresetn,
                s_axis_tdata => raw_tdata, s_axis_tvalid => raw_tvalid,
                s_axis_tready => raw_tready, s_axis_tlast => raw_tlast,
                m_axis_tdata => filt_tdata, m_axis_tvalid => filt_tvalid,
                m_axis_tready => filt_tready, m_axis_tlast => filt_tlast
            );
    end generate;
    g_none : if FILTER /= "fir" and FILTER /= "iir" generate
        raw_tready <= '1';
    end generate;

    imu : entity work.lsm6dso32_model
        generic map (STIM_FILE => STIM_FILE, ODR_PERIOD => ODR_PERIOD, FIRST_DELAY => 300 us)
        port map (
            cs_n => cs_n, sclk => sclk, mosi => mosi, miso => miso, connected => connected,
            produced => produced, ctrl1_xl => ctrl1, ctrl2_g => ctrl2, ctrl3_c => ctrl3,
            errors => model_err
        );

    -- BRAM model (Block Memory Generator in BRAM controller mode: byte
    -- addresses, 32-bit words, byte write enables) and write monitor
    bram : process (bram_clk)
        variable l    : line;
        variable w    : natural;
        variable cnt  : natural;
        variable slot : natural;
        variable last_cnt : natural := 0;
        variable last_ts  : integer := -1;
        variable word : std_logic_vector(31 downto 0);
    begin
        if rising_edge(bram_clk) then
            if bram_en = '1' then
                w := to_integer(unsigned(bram_addr(13 downto 2)));
                word := mem(w);
                for b in 0 to 3 loop
                    if bram_we(b) = '1' then
                        word(8 * b + 7 downto 8 * b) := bram_din(8 * b + 7 downto 8 * b);
                    end if;
                end loop;
                mem(w) <= word;

                if bram_we = "1111" and w = 0 and bram_din = x"494D5531" then
                    write(l, string'("R"));
                    writeline(logf, l);
                    resets   <= resets + 1;
                    last_cnt := 0;
                    last_ts  := -1;
                    committed <= 0;
                elsif bram_we = "1111" and w = 3 and bram_din /= x"00000000" then
                    -- COUNT: the sample's words are already in mem
                    cnt  := to_integer(unsigned(bram_din));
                    slot := (cnt - 1) mod SLOTS;
                    if cnt /= last_cnt + 1 then
                        report "COUNT jumped from " & integer'image(last_cnt) & " to " & integer'image(cnt) severity error;
                        mon_err <= mon_err + 1;
                    end if;
                    word := mem((T_OFF + 4 * slot) / 4);
                    if to_integer(unsigned(word(30 downto 0))) <= last_ts then
                        report "timestamp not increasing" severity error;
                        mon_err <= mon_err + 1;
                    end if;
                    last_ts  := to_integer(unsigned(word(30 downto 0)));
                    last_cnt := cnt;
                    write(l, string'("S "));
                    write(l, cnt);
                    write(l, string'(" "));
                    write(l, last_ts);
                    for k in 0 to 5 loop
                        word := mem((R_OFF + 12 * slot + 4 * (k / 2)) / 4);
                        write(l, string'(" "));
                        write(l, to_integer(signed(word(16 * (k mod 2) + 15 downto 16 * (k mod 2)))));
                    end loop;
                    for k in 0 to 5 loop
                        word := mem((F_OFF + 12 * slot + 4 * (k / 2)) / 4);
                        write(l, string'(" "));
                        write(l, to_integer(signed(word(16 * (k mod 2) + 15 downto 16 * (k mod 2)))));
                    end loop;
                    writeline(logf, l);
                    committed <= cnt;
                end if;
            end if;
        end if;
    end process;

    scenario : process
        variable l      : line;
        variable errors : natural := 0;
        variable before : natural;
        variable st     : std_logic_vector(31 downto 0);

        procedure check(cond : boolean; msg : string) is
        begin
            if not cond then
                report "CHECK FAILED: " & msg severity error;
                errors := errors + 1;
            end if;
        end procedure;

        procedure event(msg : string) is
        begin
            write(l, string'("E ") & msg);
            writeline(logf, l);
            report msg;
        end procedure;

        procedure wait_samples(n : natural; limit : time) is
            variable t0 : time := now;
        begin
            while committed < n and now - t0 < limit loop
                wait for 100 us;
            end loop;
            check(committed >= n, "only " & integer'image(committed) & " of " & integer'image(n) & " samples arrived");
        end procedure;
    begin
        aresetn <= '0';
        wait for 200 ns;
        wait until rising_edge(clk);
        aresetn <= '1';

        -- 1. run until the ring has wrapped
        event("start");
        wait_samples(WRAPS * N_SAMPLES + 5, (WRAPS * N_SAMPLES + 20) * ODR_PERIOD + 5 ms);
        check(led(1) = '1', "LED 1 (sensor ok) is off");
        st := mem(4);
        check(st(0) = '1', "STATUS: sensor not ok");
        check(st(31 downto 16) = x"0000", "STATUS: filter errors or overruns: " & hex(st));

        -- 2. SW1
        sw1 <= '1';
        wait for 100 us;
        check(sw1_sync = '1', "sw1_sync did not follow SW1");
        check(led(2) = '1', "LED 2 did not follow SW1");
        wait for 2 * ODR_PERIOD;
        st := mem(4);
        check(st(1) = '1', "STATUS bit 1 did not follow SW1");
        sw1 <= '0';
        wait for 100 us;
        check(sw1_sync = '0', "sw1_sync did not return to 0");

        -- 3. capture clear
        before := resets;
        event("clear");
        capture_clear <= '1';
        wait for 2 us;
        capture_clear <= '0';
        wait for 10 us;
        check(resets = before + 1, "capture_clear did not re-initialise the header");
        check(committed = 0, "COUNT not cleared");
        wait_samples(2 * N_SAMPLES, (2 * N_SAMPLES + 10) * ODR_PERIOD);

        -- 4. sensor unplugged, then plugged in again
        event("disconnect");
        connected <= '0';
        wait for 3 * WATCHDOG_MS * 1 ms;
        st := mem(4);
        check(st(0) = '0', "STATUS still shows the sensor as ok while unplugged");
        check(unsigned(st(15 downto 8)) > 0, "no retries counted while unplugged");
        check(led(1) = '0', "LED 1 still on while unplugged");
        before := committed;
        event("reconnect");
        connected <= '1';
        wait_samples(before + N_SAMPLES, 2 * WATCHDOG_MS * 1 ms + (N_SAMPLES + 10) * ODR_PERIOD);
        st := mem(4);
        check(st(0) = '1', "sensor not ok again after reconnecting");

        -- 5. header, sensor configuration, SPI protocol
        check(mem(0) = x"494D5531", "MAGIC " & hex(mem(0)));
        check(mem(1) = std_logic_vector(to_unsigned(N_SAMPLES * 65536 + FID * 256 + 1, 32)), "LAYOUT " & hex(mem(1)));
        check(mem(2) = std_logic_vector(to_unsigned(12 * 16777216 + 6 * 65536 + 104, 32)), "FORMAT " & hex(mem(2)));
        check(mem(7) = std_logic_vector(to_unsigned(SLOTS, 32)), "SLOTS " & hex(mem(7)));
        check(mem(8) = std_logic_vector(to_unsigned(F_OFF, 32)), "FILT_OFF " & hex(mem(8)));
        check(mem(9) = std_logic_vector(to_unsigned(R_OFF, 32)), "RAW_OFF " & hex(mem(9)));
        check(mem(10) = std_logic_vector(to_unsigned(T_OFF, 32)), "TS_OFF " & hex(mem(10)));
        check(mem(11) = std_logic_vector(to_unsigned(16384, 32)), "MEM_BYTES " & hex(mem(11)));
        check(mem(6)(7 downto 0) = x"6C", "SENSOR: WHO_AM_I " & hex(mem(6)));
        check(ctrl1 = x"40", "CTRL1_XL written wrong");
        check(ctrl2 = x"44", "CTRL2_G written wrong");
        check(ctrl3 = x"44", "CTRL3_C written wrong");
        check(model_err = 0, "SPI protocol errors in the sensor model: " & integer'image(model_err));
        check(mon_err = 0, "monitor errors: " & integer'image(mon_err));

        write(l, string'("END ") & integer'image(errors + mon_err + model_err));
        writeline(logf, l);
        if errors = 0 then
            report "TB RESULT: PASS (" & FILTER & ", N_SAMPLES=" & integer'image(N_SAMPLES) & ")";
        else
            report "TB RESULT: FAIL, " & integer'image(errors) & " failed checks" severity error;
        end if;
        done <= true;
        wait for 1 us;
        stop;
    end process;
end sim;
