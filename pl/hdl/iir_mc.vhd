----------------------------------------------------------------------------------
-- iir_mc
--
-- Multichannel IIR low-pass: a cascade of direct form I biquads, time-shared
-- over the channels with one multiplier (one DSP48). Vivado has no IIR IP,
-- so this is the hand-written counterpart of the FIR Compiler in the FIR
-- design (lab 6, part 5), with the same AXI4-Stream interface:
--
--   s_axis: one int16 sample per beat, channels in order 0..N_CH-1,
--           tlast on the last channel
--   m_axis: one int16 result per input beat, same order, same tlast
--
-- Fixed point (iir_coeffs_pkg.vhd, from filters/design_filters.py):
--   coefficients Q2.16 (18 bit), stored as b0 b1 b2 -a1 -a2
--   state and section outputs Q17.8 (25 bit): the input shifted up by 8,
--   with one bit of headroom above int16 inside the cascade
--   y = sat25(round(acc / 2^16)) per section, out = sat16(round(y / 2^8))
-- filters/filter_model.py:iir_q() is the bit-exact reference.
--
-- Each channel keeps its own state. aresetn clears it (a capture reset from
-- the PS), so a new recording does not inherit the previous one's state.
-- About 7 + 8 * IIR_SECTIONS clock cycles per sample and channel.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.iir_coeffs_pkg.all;

entity iir_mc is
    generic (
        N_CH : positive := 6
    );
    port (
        aclk          : in  std_logic;
        aresetn       : in  std_logic;
        s_axis_tdata  : in  std_logic_vector(15 downto 0);
        s_axis_tvalid : in  std_logic;
        s_axis_tready : out std_logic;
        s_axis_tlast  : in  std_logic;
        m_axis_tdata  : out std_logic_vector(15 downto 0);
        m_axis_tvalid : out std_logic;
        m_axis_tready : in  std_logic;
        m_axis_tlast  : out std_logic
    );
end iir_mc;

architecture rtl of iir_mc is
    -- Interface attributes for the block design (module reference). Vivado reads
    -- them here, in the architecture, as in its own generated wrappers.
    attribute X_INTERFACE_INFO : string;
    attribute X_INTERFACE_PARAMETER : string;
    attribute X_INTERFACE_INFO of aclk : signal is "xilinx.com:signal:clock:1.0 aclk CLK";
    attribute X_INTERFACE_PARAMETER of aclk : signal is "ASSOCIATED_BUSIF s_axis:m_axis, ASSOCIATED_RESET aresetn";
    attribute X_INTERFACE_INFO of aresetn : signal is "xilinx.com:signal:reset:1.0 aresetn RST";
    attribute X_INTERFACE_PARAMETER of aresetn : signal is "POLARITY ACTIVE_LOW";

    constant S    : positive := IIR_SECTIONS;
    constant SW   : positive := IIR_STATE_BITS;           -- 25
    constant CW   : positive := IIR_COEFF_BITS;           -- 18
    constant CF   : natural  := IIR_COEFF_FRAC;           -- 16
    constant SF   : natural  := IIR_STATE_FRAC;           -- 8
    constant PW   : positive := SW + CW;                  -- product width
    constant AW   : positive := PW + 3;                   -- accumulator: 5 products

    subtype st_t is signed(SW - 1 downto 0);
    -- hist(k)(0..1): for k = 0 the input history x[n-1], x[n-2]; for k >= 1
    -- the output history of section k-1, which is also section k's input
    -- history. Section s reads hist(s) as x1, x2 and hist(s+1) as y1, y2.
    type pair_t is array (0 to 1) of st_t;
    type hist_t is array (0 to S) of pair_t;
    type mem_t  is array (0 to N_CH - 1) of hist_t;
    constant HIST_ZERO : hist_t := (others => (others => (others => '0')));

    type coef_rom_t is array (0 to 5 * S - 1) of signed(CW - 1 downto 0);
    function init_rom return coef_rom_t is
        variable r : coef_rom_t;
    begin
        for i in 0 to 5 * S - 1 loop
            r(i) := to_signed(IIR_COEFFS(i), CW);
        end loop;
        return r;
    end function;
    constant ROM : coef_rom_t := init_rom;

    -- Saturate a signed value to w bits.
    function sat(v : signed; w : positive) return signed is
        variable hi, lo : signed(w - 1 downto 0);
    begin
        hi := (others => '1');
        hi(w - 1) := '0';
        lo := (others => '0');
        lo(w - 1) := '1';
        if v > resize(hi, v'length) then
            return hi;
        elsif v < resize(lo, v'length) then
            return lo;
        else
            return resize(v, w);
        end if;
    end function;

    type state_t is (S_IDLE, S_MAC, S_DRAIN, S_SECTION, S_ROUND, S_OUT);
    signal state : state_t := S_IDLE;

    signal mem    : mem_t := (others => HIST_ZERO);
    signal wk   : hist_t := HIST_ZERO;
    signal ch     : natural range 0 to N_CH - 1 := 0;
    signal ch_cur : natural range 0 to N_CH - 1 := 0;
    signal last_r : std_logic := '0';
    signal v      : st_t := (others => '0');          -- input of the current section
    signal sec    : natural range 0 to S - 1 := 0;
    signal tap    : natural range 0 to 5 := 0;

    -- MAC pipeline: operands -> product -> accumulator
    signal op_a   : st_t := (others => '0');
    signal op_b   : signed(CW - 1 downto 0) := (others => '0');
    signal op_v   : std_logic := '0';
    signal prod   : signed(PW - 1 downto 0) := (others => '0');
    signal prod_v : std_logic := '0';
    signal acc    : signed(AW - 1 downto 0) := (others => '0');

    signal out_r  : std_logic_vector(15 downto 0) := (others => '0');
    signal ovalid : std_logic := '0';
    signal olast  : std_logic := '0';
begin
    s_axis_tready <= '1' when state = S_IDLE and aresetn = '1' else '0';
    m_axis_tdata  <= out_r;
    m_axis_tvalid <= ovalid;
    m_axis_tlast  <= olast;

    process (aclk)
        variable a_sel : st_t;
        variable y     : st_t;
        variable rnd   : signed(AW - 1 downto 0);
        variable o17   : signed(SW downto 0);
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                state  <= S_IDLE;
                mem    <= (others => HIST_ZERO);
                ch     <= 0;
                ovalid <= '0';
                op_v   <= '0';
                prod_v <= '0';
            else
                -- pipeline stages 2 and 3 (the product register only loads
                -- when there is an operand pair: the DSP's clock enable)
                if op_v = '1' then
                    prod <= op_a * op_b;
                end if;
                prod_v <= op_v;
                if prod_v = '1' then
                    acc <= acc + resize(prod, AW);
                end if;
                op_v <= '0';

                case state is
                when S_IDLE =>
                    if s_axis_tvalid = '1' then
                        v      <= shift_left(resize(signed(s_axis_tdata), SW), SF);
                        wk   <= mem(ch);
                        ch_cur <= ch;
                        last_r <= s_axis_tlast;
                        -- tlast realigns the channel counter
                        if s_axis_tlast = '1' or ch = N_CH - 1 then
                            ch <= 0;
                        else
                            ch <= ch + 1;
                        end if;
                        sec   <= 0;
                        tap   <= 0;
                        acc   <= (others => '0');
                        state <= S_MAC;
                    end if;

                when S_MAC =>
                    -- issue one product per cycle: b0*x0 b1*x1 b2*x2 na1*y1 na2*y2
                    case tap is
                        when 0      => a_sel := v;
                        when 1      => a_sel := wk(sec)(0);
                        when 2      => a_sel := wk(sec)(1);
                        when 3      => a_sel := wk(sec + 1)(0);
                        when others => a_sel := wk(sec + 1)(1);
                    end case;
                    op_a <= a_sel;
                    op_b <= ROM(5 * sec + tap);
                    op_v <= '1';
                    if tap = 4 then
                        tap   <= 0;
                        state <= S_DRAIN;
                    else
                        tap <= tap + 1;
                    end if;

                when S_DRAIN =>
                    -- wait until the last product is in the accumulator
                    if op_v = '0' and prod_v = '0' then
                        state <= S_SECTION;
                    end if;

                when S_SECTION =>
                    rnd := shift_right(acc + shift_left(to_signed(1, AW), CF - 1), CF);
                    y   := sat(rnd, SW);
                    -- this section's input history shifts in its input
                    wk(sec)(1) <= wk(sec)(0);
                    wk(sec)(0) <= v;
                    v   <= y;
                    acc <= (others => '0');
                    if sec = S - 1 then
                        -- the last section's output history
                        wk(S)(1) <= wk(S)(0);
                        wk(S)(0) <= y;
                        state    <= S_ROUND;
                    else
                        sec   <= sec + 1;
                        state <= S_MAC;
                    end if;

                when S_ROUND =>
                    -- int16 output from the registered last section output
                    -- (a separate cycle: both roundings in one cycle miss 100 MHz)
                    o17 := shift_right(resize(wk(S)(0), SW + 1) + to_signed(2**(SF - 1), SW + 1), SF);
                    out_r  <= std_logic_vector(sat(o17, 16));
                    ovalid <= '1';
                    olast  <= last_r;
                    state  <= S_OUT;

                when S_OUT =>
                    if m_axis_tready = '1' then
                        ovalid      <= '0';
                        mem(ch_cur) <= wk;
                        state       <= S_IDLE;
                    end if;
                end case;
            end if;
        end if;
    end process;
end rtl;
