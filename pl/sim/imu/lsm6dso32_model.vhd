----------------------------------------------------------------------------------
-- lsm6dso32_model (simulation only)
--
-- SPI slave model of the LSM6DSO32, enough to test imu_reader/imu_capture:
--   * SPI mode 3: MISO changes on the falling edge, MOSI is sampled on the
--     rising edge; checks that SCLK is high whenever CS falls or rises
--   * register file with WHO_AM_I = 0x6C, CTRL3_C software reset, IF_INC
--     auto-increment for reads and writes
--   * once CTRL1_XL and CTRL2_G both have a non-zero ODR, a new sample every
--     ODR_PERIOD (after FIRST_DELAY): the next line of STIM_FILE goes into
--     0x22..0x2D and STATUS_REG gets XLDA | GDA. Reading OUTZ_H_G (0x27)
--     clears GDA, reading OUTZ_H_A (0x2D) clears XLDA. While CS is low the
--     output registers do not change (block data update).
--   * connected = '0' lets MISO float high, like an unplugged sensor
-- produced counts the samples put into the output registers.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

entity lsm6dso32_model is
    generic (
        STIM_FILE   : string;
        ODR_PERIOD  : time := 400 us;
        FIRST_DELAY : time := 300 us;
        MAX_STIM    : positive := 4096
    );
    port (
        cs_n      : in  std_logic;
        sclk      : in  std_logic;
        mosi      : in  std_logic;
        miso      : out std_logic;
        connected : in  std_logic;
        produced  : out natural;
        ctrl1_xl  : out std_logic_vector(7 downto 0);
        ctrl2_g   : out std_logic_vector(7 downto 0);
        ctrl3_c   : out std_logic_vector(7 downto 0);
        errors    : out natural
    );
end lsm6dso32_model;

architecture sim of lsm6dso32_model is
    type regs_t is array (0 to 127) of std_logic_vector(7 downto 0);
    type sample_t is array (0 to 5) of integer;
    type stim_t is array (0 to MAX_STIM - 1) of sample_t;

    impure function load(path : string) return stim_t is
        file f     : text open read_mode is path;
        variable l : line;
        variable r : stim_t := (others => (others => 0));
        variable i : natural := 0;
    begin
        while not endfile(f) and i < MAX_STIM loop
            readline(f, l);
            for c in 0 to 5 loop
                read(l, r(i)(c));
            end loop;
            i := i + 1;
        end loop;
        return r;
    end function;

    impure function count_lines(path : string) return natural is
        file f     : text open read_mode is path;
        variable l : line;
        variable i : natural := 0;
    begin
        while not endfile(f) loop
            readline(f, l);
            i := i + 1;
        end loop;
        return i;
    end function;

    constant STIM   : stim_t  := load(STIM_FILE);
    constant N_STIM : natural := count_lines(STIM_FILE);

    function defaults return regs_t is
        variable r : regs_t := (others => x"00");
    begin
        r(16#0F#) := x"6C";      -- WHO_AM_I
        r(16#12#) := x"04";      -- CTRL3_C: IF_INC
        return r;
    end function;

    signal regs      : regs_t := defaults;
    signal gen_cnt   : natural := 0;         -- samples made by the sensor clock
    signal gen_smp   : sample_t := (others => 0);
    signal prod_cnt  : natural := 0;
    signal err_cnt   : natural := 0;
    signal miso_r    : std_logic := 'Z';
    signal running   : boolean := false;
begin
    miso     <= miso_r when connected = '1' else 'H';
    produced <= prod_cnt;
    ctrl1_xl <= regs(16#10#);
    ctrl2_g  <= regs(16#11#);
    ctrl3_c  <= regs(16#12#);
    errors   <= err_cnt;

    running <= regs(16#10#)(7 downto 4) /= "0000" and regs(16#11#)(7 downto 4) /= "0000";

    -- Sample generator: the sensor's own clock, independent of the FPGA.
    gen : process
        variable k : natural := 0;
    begin
        wait until running;
        wait for FIRST_DELAY;
        while true loop
            if running then
                gen_smp <= STIM(k mod N_STIM);
                k := k + 1;
                gen_cnt <= k;
            end if;
            wait for ODR_PERIOD;
        end loop;
    end process;

    -- SPI slave and register file
    spi : process
        variable cmd     : std_logic_vector(7 downto 0);
        variable data    : std_logic_vector(7 downto 0);
        variable addr    : natural;
        variable is_read : boolean;
        variable first   : boolean;
        variable v       : std_logic_vector(15 downto 0);
        variable r       : regs_t;
        variable applied : natural := 0;
        procedure apply_sample(s : sample_t; signal rg : inout regs_t; signal cnt : inout natural) is
            variable x : regs_t;
        begin
            x := rg;
            for c in 0 to 5 loop
                v := std_logic_vector(to_signed(s(c), 16));
                -- channel order a_x a_y a_z g_x g_y g_z; gyro registers come first
                if c < 3 then
                    x(16#28# + 2 * c)     := v(7 downto 0);
                    x(16#28# + 2 * c + 1) := v(15 downto 8);
                else
                    x(16#22# + 2 * (c - 3))     := v(7 downto 0);
                    x(16#22# + 2 * (c - 3) + 1) := v(15 downto 8);
                end if;
            end loop;
            x(16#1E#) := x(16#1E#) or x"03";
            rg  <= x;
            cnt <= cnt + 1;
        end procedure;
    begin
        -- wait for CS low, applying samples while the bus is idle
        while cs_n /= '0' loop
            if gen_cnt /= applied then
                if gen_cnt - applied > 1 then
                    report "model: a sample was overwritten before it could be applied" severity warning;
                end if;
                applied := gen_cnt;
                apply_sample(gen_smp, regs, prod_cnt);
            end if;
            wait until cs_n = '0' or gen_cnt'event;
        end loop;
        if sclk /= '1' then
            report "SPI: CS fell while SCLK was low (not mode 3)" severity error;
            err_cnt <= err_cnt + 1;
        end if;
        -- command byte
        for b in 7 downto 0 loop
            wait until rising_edge(sclk) or cs_n = '1';
            exit when cs_n = '1';
            cmd(b) := mosi;
        end loop;
        if cs_n = '0' then
            is_read := cmd(7) = '1';
            addr    := to_integer(unsigned(cmd(6 downto 0)));
            first   := true;
            loop
                if is_read then
                    data := regs(addr);
                    for b in 7 downto 0 loop
                        wait until falling_edge(sclk) or cs_n = '1';
                        exit when cs_n = '1';
                        miso_r <= data(b);
                        wait until rising_edge(sclk) or cs_n = '1';
                        exit when cs_n = '1';
                    end loop;
                    exit when cs_n = '1';
                    -- reading the last data register clears its ready flag
                    r := regs;
                    if addr = 16#27# then
                        r(16#1E#)(1) := '0';
                    elsif addr = 16#2D# then
                        r(16#1E#)(0) := '0';
                    end if;
                    regs <= r;
                else
                    for b in 7 downto 0 loop
                        wait until rising_edge(sclk) or cs_n = '1';
                        exit when cs_n = '1';
                        data(b) := mosi;
                    end loop;
                    exit when cs_n = '1';
                    r := regs;
                    if addr = 16#12# and data(0) = '1' then
                        r := defaults;          -- software reset
                    elsif addr /= 16#0F# and addr /= 16#1E# then
                        r(addr) := data;
                    end if;
                    regs <= r;
                end if;
                if regs(16#12#)(2) = '1' then
                    addr := (addr + 1) mod 128;
                end if;
                first := false;
            end loop;
        end if;
        -- CS high again
        if cs_n /= '1' then
            wait until cs_n = '1';
        end if;
        miso_r <= 'Z';
        if sclk /= '1' then
            report "SPI: CS rose while SCLK was low (not mode 3)" severity error;
            err_cnt <= err_cnt + 1;
        end if;
        wait for 0 ns;
    end process;
end sim;
