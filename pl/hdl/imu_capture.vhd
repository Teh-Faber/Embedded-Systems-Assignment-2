----------------------------------------------------------------------------------
-- imu_capture
--
-- Static-design IMU front end: LSM6DSO32 -> filter -> BRAM ring buffers.
--
--   imu_reader --sample--> m_axis_raw --> [FIR Compiler | iir_mc] --> s_axis_filt
--        |                                                              |
--        +------------- raw + timestamp ------> BRAM writer <-----------+
--
-- The filter is a separate block design cell, so the FIR and the IIR design
-- differ only in that cell and in the FILT_* generics. FILTER_ID = 0 bypasses
-- the filter (filtered = raw).
--
-- BRAM (port B, written only by this block; the PS reads port A over AXI):
--
--   0x00 MAGIC      0x494D5531 ("IMU1")
--   0x04 LAYOUT     [7:0] layout version 1, [15:8] FILTER_ID, [31:16] N_SAMPLES
--   0x08 FORMAT     [15:0] ODR in Hz, [23:16] channels (6), [31:24] bytes per record (12)
--   0x0C COUNT      samples written since the last capture reset
--   0x10 STATUS     [0] IMU ok, [1] SW1, [2] decoupled, [15:8] IMU retries,
--                   [23:16] filter errors, [31:24] overruns (all counters saturate)
--   0x14 LAST_TS    timestamp of the newest sample, microseconds
--   0x18 SENSOR     [7:0] WHO_AM_I read, [15:8] last STATUS_REG
--   0x1C SLOTS      ring length in records = N_SAMPLES + 1
--   0x20 FILT_OFF   byte offset of the filtered ring
--   0x24 RAW_OFF    byte offset of the raw ring
--   0x28 TS_OFF     byte offset of the timestamp ring
--   0x2C MEM_BYTES  size of the memory
--   FILT_OFF  SLOTS records of int16[6] a_x a_y a_z g_x g_y g_z (filtered)
--   RAW_OFF   the same, unfiltered
--   TS_OFF    SLOTS uint32 timestamps (us since the capture reset)
--
-- Sample k (k = 0, 1, ...) is in slot k mod SLOTS of every ring. All of a
-- sample's words are written first, then COUNT = k + 1, so COUNT works as the
-- sequence lock of Assignment 1: read COUNT, copy, read COUNT again; if it
-- changed, retry. The ring has one slot more than the window: while COUNT = c
-- the PL may be writing sample c into slot c mod SLOTS, which is never one of
-- the window's slots (samples c - N_SAMPLES .. c - 1). So a copy of the window
-- with COUNT unchanged before and after is always consistent.
--
-- capture_clear (PS, EMIO GPIO 55) clears COUNT, the timestamp, the ring
-- position and the filter state, but leaves the IMU configured and running.
-- On the board the FIR Compiler keeps its sample history through aresetn
-- (Reset_Data_Vector on or off), so after every filter reset FILT_FLUSH zero
-- samples go through the filter and their results are dropped. The filter
-- then starts from zero, as filters/filter_model.py does.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity imu_capture is
    generic (
        CLK_HZ          : positive := 100000000;
        SCLK_HZ         : positive := 1000000;
        IMU_ODR_HZ      : positive := 104;
        N_SAMPLES       : positive := 200;
        CTRL1_XL        : natural  := 16#40#;
        CTRL2_G         : natural  := 16#44#;
        FILTER_ID       : natural  := 1;       -- 0 none, 1 FIR, 2 IIR
        FILT_TDATA_W    : positive := 32;      -- width of s_axis_filt_tdata
        FILT_DATA_W     : positive := 32;      -- significant (signed) bits in it
        FILT_SHIFT      : natural  := 15;      -- filtered = sat16(round(data / 2^FILT_SHIFT))
        FILT_FLUSH      : natural  := 0;       -- zero samples after a filter reset (FIR: taps - 1)
        MEM_BYTES       : positive := 16384;
        BOOT_US         : positive := 20000;
        SWRESET_US      : positive := 1000;
        POLL_US         : positive := 100;
        WATCHDOG_MS     : positive := 250;
        DISCARD         : natural  := 8;
        FILT_TIMEOUT_US : positive := 1000;
        REFRESH_US      : positive := 1000;
        DEBOUNCE_US     : positive := 10000
    );
    port (
        aclk               : in  std_logic;
        aresetn            : in  std_logic;
        capture_clear      : in  std_logic;   -- from the PS, asynchronous
        decouple           : in  std_logic;   -- only shown in STATUS and on a LED
        sw1                : in  std_logic;   -- board switch, asynchronous
        sw1_sync           : out std_logic;
        led                : out std_logic_vector(3 downto 0);
        -- IMU SPI
        imu_cs_n           : out std_logic;
        imu_sclk           : out std_logic;
        imu_mosi           : out std_logic;
        imu_miso           : in  std_logic;
        -- to the filter
        m_axis_raw_tdata   : out std_logic_vector(15 downto 0);
        m_axis_raw_tvalid  : out std_logic;
        m_axis_raw_tready  : in  std_logic;
        m_axis_raw_tlast   : out std_logic;
        -- from the filter
        s_axis_filt_tdata  : in  std_logic_vector(FILT_TDATA_W - 1 downto 0);
        s_axis_filt_tvalid : in  std_logic;
        s_axis_filt_tready : out std_logic;
        s_axis_filt_tlast  : in  std_logic;
        filt_aresetn       : out std_logic;
        -- BRAM port B
        bram_clk           : out std_logic;
        bram_rst           : out std_logic;
        bram_en            : out std_logic;
        bram_we            : out std_logic_vector(3 downto 0);
        bram_addr          : out std_logic_vector(31 downto 0);
        bram_din           : out std_logic_vector(31 downto 0);
        bram_dout          : in  std_logic_vector(31 downto 0)
    );
end imu_capture;

architecture rtl of imu_capture is
    -- Interface attributes for the block design (module reference). Vivado reads
    -- them here, in the architecture, as in its own generated wrappers.
    attribute X_INTERFACE_INFO : string;
    attribute X_INTERFACE_PARAMETER : string;
    attribute X_INTERFACE_MODE : string;
    attribute X_INTERFACE_INFO of aclk : signal is "xilinx.com:signal:clock:1.0 aclk CLK";
    attribute X_INTERFACE_PARAMETER of aclk : signal is "ASSOCIATED_BUSIF m_axis_raw:s_axis_filt, ASSOCIATED_RESET aresetn";
    attribute X_INTERFACE_INFO of aresetn : signal is "xilinx.com:signal:reset:1.0 aresetn RST";
    attribute X_INTERFACE_PARAMETER of aresetn : signal is "POLARITY ACTIVE_LOW";
    attribute X_INTERFACE_INFO of filt_aresetn : signal is "xilinx.com:signal:reset:1.0 filt_aresetn RST";
    attribute X_INTERFACE_PARAMETER of filt_aresetn : signal is "POLARITY ACTIVE_LOW";
    attribute X_INTERFACE_INFO of m_axis_raw_tdata : signal is "xilinx.com:interface:axis:1.0 m_axis_raw TDATA";
    attribute X_INTERFACE_INFO of m_axis_raw_tvalid : signal is "xilinx.com:interface:axis:1.0 m_axis_raw TVALID";
    attribute X_INTERFACE_INFO of m_axis_raw_tready : signal is "xilinx.com:interface:axis:1.0 m_axis_raw TREADY";
    attribute X_INTERFACE_INFO of m_axis_raw_tlast : signal is "xilinx.com:interface:axis:1.0 m_axis_raw TLAST";
    attribute X_INTERFACE_INFO of s_axis_filt_tdata : signal is "xilinx.com:interface:axis:1.0 s_axis_filt TDATA";
    attribute X_INTERFACE_INFO of s_axis_filt_tvalid : signal is "xilinx.com:interface:axis:1.0 s_axis_filt TVALID";
    attribute X_INTERFACE_INFO of s_axis_filt_tready : signal is "xilinx.com:interface:axis:1.0 s_axis_filt TREADY";
    attribute X_INTERFACE_INFO of s_axis_filt_tlast : signal is "xilinx.com:interface:axis:1.0 s_axis_filt TLAST";
    attribute X_INTERFACE_INFO of bram_clk : signal is "xilinx.com:interface:bram:1.0 BRAM_PORTB CLK";
    attribute X_INTERFACE_INFO of bram_rst : signal is "xilinx.com:interface:bram:1.0 BRAM_PORTB RST";
    attribute X_INTERFACE_INFO of bram_en : signal is "xilinx.com:interface:bram:1.0 BRAM_PORTB EN";
    attribute X_INTERFACE_INFO of bram_we : signal is "xilinx.com:interface:bram:1.0 BRAM_PORTB WE";
    attribute X_INTERFACE_INFO of bram_addr : signal is "xilinx.com:interface:bram:1.0 BRAM_PORTB ADDR";
    attribute X_INTERFACE_INFO of bram_din : signal is "xilinx.com:interface:bram:1.0 BRAM_PORTB DIN";
    attribute X_INTERFACE_INFO of bram_dout : signal is "xilinx.com:interface:bram:1.0 BRAM_PORTB DOUT";
    attribute X_INTERFACE_MODE of bram_addr : signal is "Master";
    attribute X_INTERFACE_PARAMETER of bram_addr : signal is
        "XIL_INTERFACENAME BRAM_PORTB, MASTER_TYPE BRAM_CTRL, MEM_SIZE 16384, MEM_ECC NONE, MEM_WIDTH 32, READ_WRITE_MODE READ_WRITE, READ_LATENCY 1";

    constant CYC_US     : positive := CLK_HZ / 1000000;
    constant MAGIC      : std_logic_vector(31 downto 0) := x"494D5531";
    constant FILT_OFF   : natural := 256;
    constant SLOTS      : positive := N_SAMPLES + 1;
    constant RING_BYTES : natural := ((12 * SLOTS + 255) / 256) * 256;
    constant RAW_OFF    : natural := FILT_OFF + RING_BYTES;
    constant TS_OFF     : natural := RAW_OFF + RING_BYTES;
    constant HB_SAMPLES : positive := (IMU_ODR_HZ + 1) / 2;     -- heartbeat LED toggles every half second
    constant FILT_RST_CYC : positive := 16;

    type i16_arr is array (0 to 5) of std_logic_vector(15 downto 0);

    function u32(n : natural) return std_logic_vector is
    begin
        return std_logic_vector(to_unsigned(n, 32));
    end function;

    -- round(d / 2^s), half up. s = 0 returns d unchanged.
    function round_shift(d : signed; s : natural) return signed is
        variable r : signed(d'length - 1 downto 0);
    begin
        if s = 0 then
            return d;
        end if;
        r := d + shift_left(to_signed(1, d'length), s - 1);
        return shift_right(r, s);
    end function;

    function sat_inc(c : unsigned(7 downto 0)) return unsigned is
    begin
        if c = x"FF" then
            return c;
        end if;
        return c + 1;
    end function;

    -- resets
    signal sys_rst      : std_logic := '1';
    signal clr_s        : std_logic_vector(2 downto 0) := (others => '0');
    signal pipe_rst     : std_logic := '1';
    signal frst_cnt     : natural range 0 to FILT_RST_CYC := FILT_RST_CYC;
    signal frst_req     : std_logic := '0';
    -- zero samples still to send. The range starts at -1 so that synthesis accepts
    -- the (unused) decrement when FILT_FLUSH = 0.
    signal flush_cnt    : integer range -1 to FILT_FLUSH := FILT_FLUSH;
    signal flushing     : std_logic := '0';                             -- the sample in the filter is a zero sample

    -- reader
    signal r_sample     : std_logic_vector(95 downto 0);
    signal r_valid      : std_logic;
    signal r_seen       : std_logic;
    signal r_ok         : std_logic;
    signal r_whoami     : std_logic_vector(7 downto 0);
    signal r_status     : std_logic_vector(7 downto 0);
    signal r_retries    : unsigned(7 downto 0);

    -- time base
    signal us_div       : natural range 0 to CYC_US - 1 := 0;
    signal us_tick      : std_logic := '0';
    signal ts_us        : unsigned(31 downto 0) := (others => '0');
    signal ts_seen      : unsigned(31 downto 0) := (others => '0');

    -- pipeline
    type pstate_t is (P_INIT, P_IDLE, P_SEND, P_COLLECT, P_WRITE, P_REFRESH);
    signal pst          : pstate_t := P_INIT;
    signal pend         : std_logic := '0';                         -- sample waiting for the pipeline
    signal pend_smp     : std_logic_vector(95 downto 0) := (others => '0');
    signal pend_ts      : unsigned(31 downto 0) := (others => '0');
    signal raw          : std_logic_vector(95 downto 0) := (others => '0');
    signal ts_smp       : unsigned(31 downto 0) := (others => '0');
    signal filt         : i16_arr := (others => (others => '0'));
    signal beat         : natural range 0 to 5 := 0;
    signal got          : natural range 0 to 6 := 0;
    signal bad          : std_logic := '0';
    signal tmo          : natural range 0 to FILT_TIMEOUT_US := 0;
    signal widx         : natural range 0 to 15 := 0;
    signal slot12       : natural range 0 to 12 * SLOTS := 0;       -- 12 * slot
    signal slot4        : natural range 0 to 4 * SLOTS := 0;        -- 4 * slot
    signal count        : unsigned(31 downto 0) := (others => '0');
    signal refresh_cnt  : natural range 0 to REFRESH_US := 0;

    signal filt_err     : unsigned(7 downto 0) := (others => '0');
    signal overruns     : unsigned(7 downto 0) := (others => '0');

    -- BRAM write port
    signal we_r         : std_logic := '0';
    signal addr_r       : unsigned(31 downto 0) := (others => '0');
    signal din_r        : std_logic_vector(31 downto 0) := (others => '0');

    -- switch and LEDs
    signal sw_s         : std_logic_vector(1 downto 0) := (others => '0');
    signal sw_db        : std_logic := '0';
    signal sw_cnt       : natural range 0 to DEBOUNCE_US := 0;
    signal hb           : std_logic := '0';
    signal hb_cnt       : natural range 0 to HB_SAMPLES - 1 := 0;

    signal raw_tvalid   : std_logic := '0';

    function status_word(ok, sw, dec : std_logic; retr, ferr, ovr : unsigned(7 downto 0)) return std_logic_vector is
    begin
        return std_logic_vector(ovr) & std_logic_vector(ferr) & std_logic_vector(retr)
             & "00000" & dec & sw & ok;
    end function;
begin
    assert TS_OFF + 4 * SLOTS <= MEM_BYTES
        report "imu_capture: the rings do not fit in MEM_BYTES" severity failure;
    assert FILT_DATA_W <= FILT_TDATA_W
        report "imu_capture: FILT_DATA_W > FILT_TDATA_W" severity failure;

    u_reader : entity work.imu_reader
        generic map (
            CLK_HZ => CLK_HZ, SCLK_HZ => SCLK_HZ, CTRL1_XL => CTRL1_XL, CTRL2_G => CTRL2_G,
            BOOT_US => BOOT_US, SWRESET_US => SWRESET_US, POLL_US => POLL_US,
            WATCHDOG_MS => WATCHDOG_MS, DISCARD => DISCARD
        )
        port map (
            clk => aclk, rst => sys_rst,
            sample => r_sample, sample_valid => r_valid, sample_seen => r_seen,
            imu_ok => r_ok, whoami => r_whoami, last_status => r_status, retries => r_retries,
            spi_cs_n => imu_cs_n, spi_sclk => imu_sclk, spi_mosi => imu_mosi, spi_miso => imu_miso
        );

    -- outputs
    sw1_sync           <= sw_db;
    led                <= decouple & sw_db & r_ok & (hb and r_ok);
    filt_aresetn       <= '0' when frst_cnt /= 0 else '1';
    s_axis_filt_tready <= '1';
    m_axis_raw_tvalid  <= raw_tvalid;
    m_axis_raw_tdata   <= raw(95 - 16 * beat downto 80 - 16 * beat);
    m_axis_raw_tlast   <= '1' when beat = 5 else '0';
    bram_clk           <= aclk;
    bram_rst           <= '0';
    bram_en            <= we_r;
    bram_we            <= (others => we_r);
    bram_addr          <= std_logic_vector(addr_r);
    bram_din           <= din_r;

    process (aclk)
        variable d    : signed(FILT_DATA_W downto 0);
        variable rnd  : signed(FILT_DATA_W downto 0);
        variable r16  : std_logic_vector(15 downto 0);
        variable rec  : i16_arr;
        variable base : natural;
        variable consume : boolean;
    begin
        if rising_edge(aclk) then
            -- reset synchronizers: aresetn comes from proc_sys_reset (synchronous),
            -- capture_clear from the PS GPIO block (asynchronous)
            sys_rst  <= not aresetn;
            clr_s    <= clr_s(1 downto 0) & capture_clear;
            pipe_rst <= (not aresetn) or clr_s(2);

            -- filter reset: at least FILT_RST_CYC cycles (FIR Compiler needs 2)
            if pipe_rst = '1' or frst_req = '1' then
                frst_cnt  <= FILT_RST_CYC;
                flush_cnt <= FILT_FLUSH;
            elsif frst_cnt /= 0 then
                frst_cnt <= frst_cnt - 1;
            end if;
            frst_req <= '0';

            -- microsecond time base
            if us_div = CYC_US - 1 then
                us_div  <= 0;
                us_tick <= '1';
            else
                us_div  <= us_div + 1;
                us_tick <= '0';
            end if;
            if pipe_rst = '1' then
                ts_us <= (others => '0');
            elsif us_tick = '1' then
                ts_us <= ts_us + 1;
            end if;
            if r_seen = '1' then
                ts_seen <= ts_us;
            end if;

            -- SW1: two-flop synchronizer and debounce
            sw_s <= sw_s(0) & sw1;
            if sw_s(1) = sw_db then
                sw_cnt <= 0;
            elsif us_tick = '1' then
                if sw_cnt = DEBOUNCE_US then
                    sw_db  <= sw_s(1);
                    sw_cnt <= 0;
                else
                    sw_cnt <= sw_cnt + 1;
                end if;
            end if;

            we_r <= '0';

            if pipe_rst = '1' then
                pst        <= P_INIT;
                pend       <= '0';
                flushing   <= '0';
                widx       <= 0;
                raw_tvalid <= '0';
                count      <= (others => '0');
                slot12     <= 0;
                slot4      <= 0;
                filt_err   <= (others => '0');
                overruns   <= (others => '0');
                got        <= 0;
                refresh_cnt <= 0;
            else
                -- filtered results can arrive while the last raw beats are still going out
                if s_axis_filt_tvalid = '1' then
                    if pst = P_SEND or pst = P_COLLECT then
                        d   := resize(signed(s_axis_filt_tdata(FILT_DATA_W - 1 downto 0)), FILT_DATA_W + 1);
                        rnd := round_shift(d, FILT_SHIFT);
                        if rnd > to_signed(32767, FILT_DATA_W + 1) then
                            r16 := x"7FFF";
                        elsif rnd < to_signed(-32768, FILT_DATA_W + 1) then
                            r16 := x"8000";
                        else
                            r16 := std_logic_vector(resize(rnd, 16));
                        end if;
                        if got < 6 then
                            filt(got) <= r16;
                            got <= got + 1;
                        end if;
                        -- tlast must be on the sixth result, otherwise the
                        -- filter's channel order is off: drop the sample
                        if (s_axis_filt_tlast = '1') /= (got = 5) then
                            bad <= '1';
                        end if;
                    end if;
                    -- results outside a sample are ignored; if the filter got out
                    -- of step, the tlast check of the next sample catches it
                end if;

                if us_tick = '1' and refresh_cnt /= REFRESH_US then
                    refresh_cnt <= refresh_cnt + 1;
                end if;

                -- one sample can wait while the pipeline is busy; a second one
                -- overwrites it and is counted as an overrun (cannot happen at
                -- these rates unless the filter hangs)
                -- a waiting sample is not taken while the filter is flushed
                consume := pst = P_IDLE and pend = '1' and not (FILTER_ID /= 0 and flush_cnt > 0);
                if r_valid = '1' then
                    if pend = '1' and not consume then
                        overruns <= sat_inc(overruns);
                    end if;
                    pend_smp <= r_sample;
                    pend_ts  <= ts_seen;
                end if;

                case pst is
                when P_INIT =>
                    -- header constants, COUNT = 0, then wait for samples
                    we_r <= '1';
                    addr_r <= to_unsigned(4 * widx, 32);
                    case widx is
                        when 0      => din_r <= MAGIC;
                        when 1      => din_r <= u32(N_SAMPLES * 65536 + FILTER_ID * 256 + 1);
                        when 2      => din_r <= u32(12 * 16777216 + 6 * 65536 + IMU_ODR_HZ);
                        when 3      => din_r <= (others => '0');                   -- COUNT
                        when 4      => din_r <= status_word(r_ok, sw_db, decouple, r_retries, filt_err, overruns);
                        when 5      => din_r <= (others => '0');                   -- LAST_TS
                        when 6      => din_r <= x"0000" & r_status & r_whoami;
                        when 7      => din_r <= u32(SLOTS);
                        when 8      => din_r <= u32(FILT_OFF);
                        when 9      => din_r <= u32(RAW_OFF);
                        when 10     => din_r <= u32(TS_OFF);
                        when others => din_r <= u32(MEM_BYTES);
                    end case;
                    if widx = 11 then
                        widx <= 0;
                        pst  <= P_IDLE;
                    else
                        widx <= widx + 1;
                    end if;

                when P_IDLE =>
                    got <= 0;
                    bad <= '0';
                    if FILTER_ID /= 0 and flush_cnt > 0 then
                        -- after a filter reset: send a zero sample, drop its results
                        raw        <= (others => '0');
                        flushing   <= '1';
                        beat       <= 0;
                        raw_tvalid <= '1';
                        pst        <= P_SEND;
                    elsif pend = '1' then
                        flushing <= '0';
                        raw    <= pend_smp;
                        ts_smp <= pend_ts;
                        if FILTER_ID = 0 then
                            for i in 0 to 5 loop
                                filt(i) <= pend_smp(95 - 16 * i downto 80 - 16 * i);
                            end loop;
                            widx <= 0;
                            pst  <= P_WRITE;
                        else
                            beat       <= 0;
                            raw_tvalid <= '1';
                            pst        <= P_SEND;
                        end if;
                    elsif refresh_cnt = REFRESH_US then
                        refresh_cnt <= 0;
                        widx <= 0;
                        pst  <= P_REFRESH;
                    end if;

                when P_SEND =>
                    if m_axis_raw_tready = '1' then
                        if beat = 5 then
                            raw_tvalid <= '0';
                            tmo        <= 0;
                            pst        <= P_COLLECT;
                        else
                            beat <= beat + 1;
                        end if;
                    end if;

                when P_COLLECT =>
                    if us_tick = '1' and tmo /= FILT_TIMEOUT_US then
                        tmo <= tmo + 1;
                    end if;
                    if got = 6 then
                        if bad = '1' then
                            filt_err <= sat_inc(filt_err);
                            frst_req <= '1';
                            pst      <= P_IDLE;
                        elsif flushing = '1' then
                            if flush_cnt > 0 then
                                flush_cnt <= flush_cnt - 1;
                            end if;
                            pst <= P_IDLE;
                        else
                            widx <= 0;
                            pst  <= P_WRITE;
                        end if;
                    elsif tmo = FILT_TIMEOUT_US then
                        -- the filter lost results: reset it, drop the sample
                        filt_err <= sat_inc(filt_err);
                        frst_req <= '1';
                        pst      <= P_IDLE;
                    end if;

                when P_WRITE =>
                    -- raw (3 words), filtered (3 words), timestamp, then COUNT,
                    -- LAST_TS and STATUS: COUNT only after the data
                    for i in 0 to 5 loop
                        rec(i) := raw(95 - 16 * i downto 80 - 16 * i);
                    end loop;
                    we_r <= '1';
                    case widx is
                        when 0 | 1 | 2 =>
                            base := RAW_OFF + slot12 + 4 * widx;
                            addr_r <= to_unsigned(base, 32);
                            din_r  <= rec(2 * widx + 1) & rec(2 * widx);
                        when 3 | 4 | 5 =>
                            base := FILT_OFF + slot12 + 4 * (widx - 3);
                            addr_r <= to_unsigned(base, 32);
                            din_r  <= filt(2 * (widx - 3) + 1) & filt(2 * (widx - 3));
                        when 6 =>
                            addr_r <= to_unsigned(TS_OFF + slot4, 32);
                            din_r  <= std_logic_vector(ts_smp);
                        when 7 =>
                            addr_r <= to_unsigned(16#0C#, 32);
                            din_r  <= std_logic_vector(count + 1);
                        when 8 =>
                            addr_r <= to_unsigned(16#14#, 32);
                            din_r  <= std_logic_vector(ts_smp);
                        when others =>
                            addr_r <= to_unsigned(16#10#, 32);
                            din_r  <= status_word(r_ok, sw_db, decouple, r_retries, filt_err, overruns);
                    end case;
                    if widx = 9 then
                        widx  <= 0;
                        count <= count + 1;
                        if slot4 = 4 * (SLOTS - 1) then
                            slot12 <= 0;
                            slot4  <= 0;
                        else
                            slot12 <= slot12 + 12;
                            slot4  <= slot4 + 4;
                        end if;
                        if hb_cnt = HB_SAMPLES - 1 then
                            hb_cnt <= 0;
                            hb     <= not hb;
                        else
                            hb_cnt <= hb_cnt + 1;
                        end if;
                        pst <= P_IDLE;
                    else
                        widx <= widx + 1;
                    end if;

                when P_REFRESH =>
                    -- keep STATUS and SENSOR current when no samples arrive
                    -- (sensor unplugged, still booting)
                    we_r <= '1';
                    if widx = 0 then
                        addr_r <= to_unsigned(16#10#, 32);
                        din_r  <= status_word(r_ok, sw_db, decouple, r_retries, filt_err, overruns);
                        widx   <= 1;
                    else
                        addr_r <= to_unsigned(16#18#, 32);
                        din_r  <= x"0000" & r_status & r_whoami;
                        widx   <= 0;
                        pst    <= P_IDLE;
                    end if;
                end case;

                if r_valid = '1' then
                    pend <= '1';
                elsif consume then
                    pend <= '0';
                end if;
            end if;
        end if;
    end process;
end rtl;
