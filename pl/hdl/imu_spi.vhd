----------------------------------------------------------------------------------
-- imu_spi
--
-- SPI master for the LSM6DSO32, mode 3 (SCLK idle high, data changed on the
-- falling edge and sampled on the rising edge). Replaces the lab's CTRL.vhd:
--   * runs on the 100 MHz fabric clock with a clock enable, so the whole IMU
--     path is in one clock domain with the filter and the BRAM
--   * SCLK, MOSI and CS are registered, so SCLK has no glitches
--   * one transaction can read up to 16 bytes (the sensor's IF_INC
--     auto-increment), so a 6-axis sample is one burst instead of 12 frames
-- These were the limitations listed in the Assignment 1 report.
--
-- Transaction: CS low, command byte (bit 7 = read, bits 6..0 = register),
-- then N data bytes, CS high. A write sends one data byte (wdata).
-- Each received byte is presented on rx_data with a one-cycle rx_valid.
-- done pulses after CS has gone high again.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity imu_spi is
    generic (
        CLK_HZ  : positive := 100000000;
        SCLK_HZ : positive := 1000000
    );
    port (
        clk      : in  std_logic;
        rst      : in  std_logic;                     -- synchronous, active high
        -- command
        start    : in  std_logic;                     -- one-cycle pulse when idle
        rd       : in  std_logic;                     -- '1' read, '0' write
        reg_addr : in  std_logic_vector(6 downto 0);
        wdata    : in  std_logic_vector(7 downto 0);
        nbytes   : in  unsigned(4 downto 0);          -- data bytes to read, 1..16
        busy     : out std_logic;
        done     : out std_logic;
        rx_valid : out std_logic;
        rx_data  : out std_logic_vector(7 downto 0);
        -- pins
        spi_cs_n : out std_logic;
        spi_sclk : out std_logic;
        spi_mosi : out std_logic;
        spi_miso : in  std_logic
    );
end imu_spi;

architecture rtl of imu_spi is
    constant HALF : positive := CLK_HZ / (2 * SCLK_HZ);   -- cycles per half SCLK period

    type state_t is (S_IDLE, S_CS_SETUP, S_LOW, S_HIGH, S_CS_HOLD, S_GAP);
    signal state    : state_t := S_IDLE;
    signal tick_cnt : natural range 0 to HALF - 1 := 0;
    signal bit_cnt  : natural range 0 to 7 := 0;
    signal byte_cnt : unsigned(4 downto 0) := (others => '0');  -- data bytes left
    signal cmd_byte : std_logic := '1';                         -- shifting the command
    signal is_read  : std_logic := '1';
    signal tx_sr    : std_logic_vector(7 downto 0) := (others => '0');
    signal rx_sr    : std_logic_vector(7 downto 0) := (others => '0');
    signal wdata_r  : std_logic_vector(7 downto 0) := (others => '0');
    signal miso_s   : std_logic_vector(1 downto 0) := (others => '0');

    signal cs_n_r, sclk_r, mosi_r : std_logic := '1';
    signal done_r, rxv_r          : std_logic := '0';
    signal rxd_r                  : std_logic_vector(7 downto 0) := (others => '0');
begin
    spi_cs_n <= cs_n_r;
    spi_sclk <= sclk_r;
    spi_mosi <= mosi_r;
    busy     <= '0' when state = S_IDLE else '1';
    done     <= done_r;
    rx_valid <= rxv_r;
    rx_data  <= rxd_r;

    process (clk)
        variable shifted : std_logic_vector(7 downto 0);
    begin
        if rising_edge(clk) then
            miso_s <= miso_s(0) & spi_miso;      -- two-flop synchronizer
            done_r <= '0';
            rxv_r  <= '0';

            if rst = '1' then
                state    <= S_IDLE;
                cs_n_r   <= '1';
                sclk_r   <= '1';
                mosi_r   <= '1';
                tick_cnt <= 0;
            else
                case state is
                when S_IDLE =>
                    cs_n_r <= '1';
                    sclk_r <= '1';
                    if start = '1' then
                        tx_sr    <= rd & reg_addr;
                        is_read  <= rd;
                        wdata_r  <= wdata;
                        if rd = '1' then
                            byte_cnt <= nbytes;
                        else
                            byte_cnt <= to_unsigned(1, 5);
                        end if;
                        cmd_byte <= '1';
                        bit_cnt  <= 7;
                        cs_n_r   <= '0';
                        tick_cnt <= 0;
                        state    <= S_CS_SETUP;
                    end if;

                when S_CS_SETUP =>               -- half a period between CS and the first edge
                    if tick_cnt = HALF - 1 then
                        tick_cnt <= 0;
                        sclk_r   <= '0';
                        mosi_r   <= tx_sr(7);
                        state    <= S_LOW;
                    else
                        tick_cnt <= tick_cnt + 1;
                    end if;

                when S_LOW =>                    -- SCLK low; rising edge samples MISO
                    if tick_cnt = HALF - 1 then
                        tick_cnt <= 0;
                        sclk_r   <= '1';
                        rx_sr    <= rx_sr(6 downto 0) & miso_s(1);
                        state    <= S_HIGH;
                    else
                        tick_cnt <= tick_cnt + 1;
                    end if;

                when S_HIGH =>                   -- SCLK high; next falling edge shifts MOSI
                    if tick_cnt = HALF - 1 then
                        tick_cnt <= 0;
                        if bit_cnt = 0 then
                            -- one byte finished
                            if cmd_byte = '0' then
                                rxv_r    <= is_read;
                                rxd_r    <= rx_sr;
                                byte_cnt <= byte_cnt - 1;
                            end if;
                            if cmd_byte = '0' and byte_cnt = 1 then
                                state <= S_CS_HOLD;           -- last byte done
                            else
                                if cmd_byte = '1' and is_read = '0' then
                                    shifted := wdata_r;
                                else
                                    shifted := (others => '0');
                                end if;
                                cmd_byte <= '0';
                                tx_sr    <= shifted;
                                bit_cnt  <= 7;
                                sclk_r   <= '0';
                                mosi_r   <= shifted(7);
                                state    <= S_LOW;
                            end if;
                        else
                            bit_cnt <= bit_cnt - 1;
                            tx_sr   <= tx_sr(6 downto 0) & '0';
                            sclk_r  <= '0';
                            mosi_r  <= tx_sr(6);
                            state   <= S_LOW;
                        end if;
                    else
                        tick_cnt <= tick_cnt + 1;
                    end if;

                when S_CS_HOLD =>                -- CS stays low half a period after the last edge
                    if tick_cnt = HALF - 1 then
                        tick_cnt <= 0;
                        cs_n_r   <= '1';
                        mosi_r   <= '1';
                        state    <= S_GAP;
                    else
                        tick_cnt <= tick_cnt + 1;
                    end if;

                when S_GAP =>                    -- CS high for a full period between frames
                    if tick_cnt = HALF - 1 then
                        tick_cnt <= 0;
                        done_r   <= '1';
                        state    <= S_IDLE;
                    else
                        tick_cnt <= tick_cnt + 1;
                    end if;
                end case;
            end if;
        end if;
    end process;
end rtl;
