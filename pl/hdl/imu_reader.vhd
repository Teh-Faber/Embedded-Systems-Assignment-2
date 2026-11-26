----------------------------------------------------------------------------------
-- imu_reader
--
-- Drives imu_spi to configure the LSM6DSO32 and read one 6-axis sample each
-- time the sensor has a new one.
--
--   1. wait BOOT_US after reset (sensor turn-on)
--   2. CTRL3_C = 0x01 (software reset), wait SWRESET_US
--   3. read WHO_AM_I; anything but 0x6C: wait, count a retry, go to 2
--   4. CTRL3_C = 0x44 (BDU = 1, IF_INC = 1), CTRL1_XL, CTRL2_G
--   5. every POLL_US read STATUS_REG. When XLDA and GDA are both set, read
--      0x22..0x2D in one 12-byte burst and output the sample. The first
--      DISCARD samples after configuration are dropped (gyro turn-on).
--   6. no sample for WATCHDOG_MS: count a retry and go back to 2, so a
--      sensor that was unplugged or browned out is configured again.
--
-- Polling the status register instead of sampling on a fabric timer means
-- every sensor sample is read exactly once, at the sensor's own ODR: no
-- duplicates and no drops when the sensor clock and the FPGA clock differ.
-- Waiting for both flags before reading keeps accel and gyro of one sample
-- together (reading the data registers clears the flags).
--
-- sample is a, then g: a_x a_y a_z g_x g_y g_z, 16 bits each, a_x in the
-- top bits. sample_seen pulses when the status read finds new data, which is
-- the time the sample is timestamped with.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity imu_reader is
    generic (
        CLK_HZ      : positive := 100000000;
        SCLK_HZ     : positive := 1000000;
        CTRL1_XL    : natural  := 16#40#;   -- 104 Hz, +-4 g
        CTRL2_G     : natural  := 16#44#;   -- 104 Hz, +-500 dps
        BOOT_US     : positive := 20000;
        SWRESET_US  : positive := 1000;
        POLL_US     : positive := 100;
        WATCHDOG_MS : positive := 250;
        DISCARD     : natural  := 8
    );
    port (
        clk          : in  std_logic;
        rst          : in  std_logic;
        sample       : out std_logic_vector(95 downto 0);
        sample_valid : out std_logic;
        sample_seen  : out std_logic;
        imu_ok       : out std_logic;
        whoami       : out std_logic_vector(7 downto 0);
        last_status  : out std_logic_vector(7 downto 0);
        retries      : out unsigned(7 downto 0);
        -- SPI pins
        spi_cs_n     : out std_logic;
        spi_sclk     : out std_logic;
        spi_mosi     : out std_logic;
        spi_miso     : in  std_logic
    );
end imu_reader;

architecture rtl of imu_reader is
    constant CYC_US   : positive := CLK_HZ / 1000000;
    constant WHOAMI_V : std_logic_vector(7 downto 0) := x"6C";

    type state_t is (
        S_BOOT, S_SWRESET, S_SWRESET_WAIT, S_WHOAMI, S_WHOAMI_WAIT,
        S_CFG3, S_CFG1, S_CFG2, S_POLL_WAIT, S_STATUS, S_BURST, S_EMIT, S_RETRY
    );
    signal state : state_t := S_BOOT;

    -- spi command
    signal spi_start : std_logic := '0';
    signal spi_rd    : std_logic := '0';
    signal spi_addr  : std_logic_vector(6 downto 0) := (others => '0');
    signal spi_wdata : std_logic_vector(7 downto 0) := (others => '0');
    signal spi_n     : unsigned(4 downto 0) := to_unsigned(1, 5);
    signal spi_done  : std_logic;
    signal spi_busy  : std_logic;
    signal rx_valid  : std_logic;
    signal rx_data   : std_logic_vector(7 downto 0);
    signal issued    : std_logic := '0';      -- command sent, waiting for done

    -- timers
    signal us_div   : natural range 0 to CYC_US - 1 := 0;
    signal us_tick  : std_logic := '0';
    signal wait_cnt  : natural range 0 to 2**24 - 1 := 0;   -- generic wait
    signal poll_cnt  : natural range 0 to 2**16 - 1 := 0;
    signal wd_cnt    : natural range 0 to 2**24 - 1 := 0;

    signal bytes    : std_logic_vector(95 downto 0) := (others => '0');
    signal byte_idx : natural range 0 to 11 := 0;
    signal discard_cnt : natural range 0 to 255 := 0;

    signal ok_r      : std_logic := '0';
    signal who_r     : std_logic_vector(7 downto 0) := (others => '0');
    signal stat_r    : std_logic_vector(7 downto 0) := (others => '0');
    signal retry_r   : unsigned(7 downto 0) := (others => '0');
    signal sample_r  : std_logic_vector(95 downto 0) := (others => '0');
    signal valid_r   : std_logic := '0';
    signal seen_r    : std_logic := '0';
begin
    u_spi : entity work.imu_spi
        generic map (CLK_HZ => CLK_HZ, SCLK_HZ => SCLK_HZ)
        port map (
            clk => clk, rst => rst,
            start => spi_start, rd => spi_rd, reg_addr => spi_addr, wdata => spi_wdata,
            nbytes => spi_n, busy => spi_busy, done => spi_done,
            rx_valid => rx_valid, rx_data => rx_data,
            spi_cs_n => spi_cs_n, spi_sclk => spi_sclk, spi_mosi => spi_mosi, spi_miso => spi_miso
        );

    sample       <= sample_r;
    sample_valid <= valid_r;
    sample_seen  <= seen_r;
    imu_ok       <= ok_r;
    whoami       <= who_r;
    last_status  <= stat_r;
    retries      <= retry_r;

    process (clk)
        -- Start one SPI transaction (only called when the engine is idle).
        procedure spi_cmd(r : std_logic; a : natural; d : natural; n : natural) is
        begin
            spi_start <= '1';
            spi_rd    <= r;
            spi_addr  <= std_logic_vector(to_unsigned(a, 7));
            spi_wdata <= std_logic_vector(to_unsigned(d, 8));
            spi_n     <= to_unsigned(n, 5);
            issued    <= '1';
        end procedure;
    begin
        if rising_edge(clk) then
            spi_start <= '0';
            valid_r   <= '0';
            seen_r    <= '0';

            -- 1 us time base
            if us_div = CYC_US - 1 then
                us_div  <= 0;
                us_tick <= '1';
            else
                us_div  <= us_div + 1;
                us_tick <= '0';
            end if;

            if rst = '1' then
                state   <= S_BOOT;
                wait_cnt <= 0;
                issued  <= '0';
                ok_r    <= '0';
                retry_r <= (others => '0');
                who_r   <= (others => '0');
                stat_r  <= (others => '0');
            else
                -- watchdog runs while configured
                if us_tick = '1' and ok_r = '1' then
                    wd_cnt <= wd_cnt + 1;
                end if;
                if us_tick = '1' and poll_cnt /= 2**16 - 1 then
                    poll_cnt <= poll_cnt + 1;
                end if;

                case state is
                when S_BOOT =>
                    if us_tick = '1' then
                        wait_cnt <= wait_cnt + 1;
                    end if;
                    if wait_cnt >= BOOT_US then
                        state <= S_SWRESET;
                    end if;

                when S_SWRESET =>
                    ok_r <= '0';
                    if issued = '0' and spi_busy = '0' then
                        spi_cmd('0', 16#12#, 16#01#, 1);        -- CTRL3_C: SW_RESET
                    elsif spi_done = '1' then
                        issued  <= '0';
                        wait_cnt <= 0;
                        state   <= S_SWRESET_WAIT;
                    end if;

                when S_SWRESET_WAIT =>
                    if us_tick = '1' then
                        wait_cnt <= wait_cnt + 1;
                    end if;
                    if wait_cnt >= SWRESET_US then
                        state <= S_WHOAMI;
                    end if;

                when S_WHOAMI =>
                    if issued = '0' and spi_busy = '0' then
                        spi_cmd('1', 16#0F#, 0, 1);             -- WHO_AM_I
                    else
                        if rx_valid = '1' then
                            who_r <= rx_data;
                        end if;
                        if spi_done = '1' then
                            issued <= '0';
                            state  <= S_WHOAMI_WAIT;
                        end if;
                    end if;

                when S_WHOAMI_WAIT =>
                    if who_r = WHOAMI_V then
                        state <= S_CFG3;
                    else
                        wait_cnt <= 0;
                        state   <= S_RETRY;
                    end if;

                when S_CFG3 =>
                    if issued = '0' and spi_busy = '0' then
                        spi_cmd('0', 16#12#, 16#44#, 1);        -- CTRL3_C: BDU, IF_INC
                    elsif spi_done = '1' then
                        issued <= '0';
                        state  <= S_CFG1;
                    end if;

                when S_CFG1 =>
                    if issued = '0' and spi_busy = '0' then
                        spi_cmd('0', 16#10#, CTRL1_XL, 1);      -- CTRL1_XL
                    elsif spi_done = '1' then
                        issued <= '0';
                        state  <= S_CFG2;
                    end if;

                when S_CFG2 =>
                    if issued = '0' and spi_busy = '0' then
                        spi_cmd('0', 16#11#, CTRL2_G, 1);       -- CTRL2_G
                    elsif spi_done = '1' then
                        issued      <= '0';
                        ok_r        <= '1';
                        wd_cnt       <= 0;
                        poll_cnt     <= 0;
                        discard_cnt <= 0;
                        state       <= S_POLL_WAIT;
                    end if;

                when S_POLL_WAIT =>
                    if wd_cnt >= WATCHDOG_MS * 1000 then
                        ok_r    <= '0';
                        wait_cnt <= 0;
                        state   <= S_RETRY;
                    elsif poll_cnt >= POLL_US then
                        poll_cnt <= 0;
                        state   <= S_STATUS;
                    end if;

                when S_STATUS =>
                    if issued = '0' and spi_busy = '0' then
                        spi_cmd('1', 16#1E#, 0, 1);             -- STATUS_REG
                    else
                        if rx_valid = '1' then
                            stat_r <= rx_data;
                        end if;
                        if spi_done = '1' then
                            issued <= '0';
                            -- bits 7..3 are always 0 on a working sensor; a
                            -- floating MISO reads 0xFF and must not count as data
                            if stat_r(7 downto 3) = "00000" and stat_r(1 downto 0) = "11" then
                                seen_r   <= '1';
                                byte_idx <= 0;
                                state    <= S_BURST;
                            else
                                state <= S_POLL_WAIT;
                            end if;
                        end if;
                    end if;

                when S_BURST =>
                    if issued = '0' and spi_busy = '0' then
                        spi_cmd('1', 16#22#, 0, 12);            -- OUTX_L_G .. OUTZ_H_A
                    else
                        if rx_valid = '1' then
                            -- byte k lands at bits 8k+7..8k, so bytes(15:0) = g_x
                            bytes(8 * byte_idx + 7 downto 8 * byte_idx) <= rx_data;
                            if byte_idx /= 11 then
                                byte_idx <= byte_idx + 1;
                            end if;
                        end if;
                        if spi_done = '1' then
                            issued <= '0';
                            state  <= S_EMIT;
                        end if;
                    end if;

                when S_EMIT =>
                    wd_cnt <= 0;
                    if discard_cnt < DISCARD then
                        discard_cnt <= discard_cnt + 1;
                    else
                        -- reorder to a_x a_y a_z g_x g_y g_z, a_x on top
                        sample_r <= bytes(63 downto 48) & bytes(79 downto 64) & bytes(95 downto 80)
                                  & bytes(15 downto 0)  & bytes(31 downto 16) & bytes(47 downto 32);
                        valid_r  <= '1';
                    end if;
                    state <= S_POLL_WAIT;

                when S_RETRY =>
                    if us_tick = '1' then
                        wait_cnt <= wait_cnt + 1;
                    end if;
                    if wait_cnt >= WATCHDOG_MS * 1000 then
                        retry_r <= retry_r + 1;
                        state   <= S_SWRESET;
                    end if;
                end case;
            end if;
        end if;
    end process;
end rtl;
