----------------------------------------------------------------------------------
-- emio_io
--
-- Splits the PS's EMIO GPIO bank into named signals:
--   EMIO 0 (PS GPIO pin 54)  out  decouple       DFX decoupler + partition reset
--   EMIO 1 (PS GPIO pin 55)  out  capture_clear  clears the IMU capture pipeline
--   EMIO 2 (PS GPIO pin 56)  in   sw1            board switch SW1 (debounced in imu_capture)
-- Plain wires: the decouple path does not depend on any other PL logic.
----------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

entity emio_io is
    port (
        gpio_o        : in  std_logic_vector(2 downto 0);
        gpio_i        : out std_logic_vector(2 downto 0);
        sw1           : in  std_logic;
        decouple      : out std_logic;
        capture_clear : out std_logic
    );
end emio_io;

architecture rtl of emio_io is
begin
    decouple      <= gpio_o(0);
    capture_clear <= gpio_o(1);
    gpio_i        <= sw1 & gpio_o(1 downto 0);
end rtl;
