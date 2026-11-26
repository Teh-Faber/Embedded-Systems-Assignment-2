----------------------------------------------------------------------------------
-- fir_compiler_model (simulation only)
--
-- Stand-in for the Xilinx FIR Compiler as configured by dfx_project.tcl, so
-- imu_capture can be simulated in GHDL: N_CH interleaved channels, integer
-- coefficients, full-precision output, tlast on the last channel (vector
-- framing). Like the real core it applies back-pressure: s_axis_data_tready
-- is low for BUSY cycles after each accepted sample, and each result comes
-- out LATENCY cycles later. There is no m_axis_data_tready (imu_capture
-- holds the real core's tready high).
-- Like the real core on the board, aresetn does not clear the sample history
-- (KEEP_HISTORY = true). imu_capture must flush it (FILT_FLUSH).
-- Coefficients come from fir_tb_pkg.vhd, written by gen_tb.py from
-- filters/filters.json.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.fir_tb_pkg.all;

entity fir_compiler_model is
    generic (
        N_CH    : positive := 6;
        BUSY    : positive := 11;
        LATENCY : positive := 23;
        KEEP_HISTORY : boolean := true
    );
    port (
        aclk               : in  std_logic;
        aresetn            : in  std_logic;
        s_axis_data_tdata  : in  std_logic_vector(15 downto 0);
        s_axis_data_tvalid : in  std_logic;
        s_axis_data_tready : out std_logic;
        s_axis_data_tlast  : in  std_logic;
        m_axis_data_tdata  : out std_logic_vector(31 downto 0);
        m_axis_data_tvalid : out std_logic;
        m_axis_data_tlast  : out std_logic
    );
end fir_compiler_model;

architecture sim of fir_compiler_model is
    constant TAPS : positive := FIR_TB_COEFFS'length;
    type line_t is array (0 to TAPS - 1) of integer;
    type lines_t is array (0 to N_CH - 1) of line_t;
    type pipe_v_t is array (0 to LATENCY - 1) of std_logic;
    type pipe_d_t is array (0 to LATENCY - 1) of integer;
    signal ready : std_logic := '0';
begin
    s_axis_data_tready <= ready;

    process (aclk)
        variable dl     : lines_t := (others => (others => 0));
        variable ch     : natural := 0;
        variable busy_c : natural := 0;
        variable pv, pl : pipe_v_t := (others => '0');
        variable pd     : pipe_d_t := (others => 0);
        variable acc    : integer;
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                if not KEEP_HISTORY then
                    dl := (others => (others => 0));
                end if;
                ch := 0;
                busy_c := 2;
                pv := (others => '0');
                ready <= '0';
                m_axis_data_tvalid <= '0';
            else
                -- output pipeline
                m_axis_data_tvalid <= pv(LATENCY - 1);
                m_axis_data_tlast  <= pl(LATENCY - 1);
                m_axis_data_tdata  <= std_logic_vector(to_signed(pd(LATENCY - 1), 32));
                pv := '0' & pv(0 to LATENCY - 2);
                pl := '0' & pl(0 to LATENCY - 2);
                pd := 0 & pd(0 to LATENCY - 2);

                if ready = '1' and s_axis_data_tvalid = '1' then
                    dl(ch) := to_integer(signed(s_axis_data_tdata)) & dl(ch)(0 to TAPS - 2);
                    acc := 0;
                    for k in 0 to TAPS - 1 loop
                        acc := acc + FIR_TB_COEFFS(k) * dl(ch)(k);
                    end loop;
                    pv(0) := '1';
                    pd(0) := acc;
                    if ch = N_CH - 1 then
                        pl(0) := '1';
                        ch := 0;
                    else
                        pl(0) := '0';
                        ch := ch + 1;
                    end if;
                    if (s_axis_data_tlast = '1') /= (pl(0) = '1') then
                        report "fir model: tlast does not match the channel count" severity error;
                    end if;
                    busy_c := BUSY;
                end if;

                if busy_c /= 0 then
                    busy_c := busy_c - 1;
                    ready <= '0';
                else
                    ready <= '1';
                end if;
                if ready = '1' and s_axis_data_tvalid = '1' then
                    ready <= '0';
                end if;
            end if;
        end if;
    end process;
end sim;
