/*
 * Links the two partial bitstreams into the ELF as read-only data.
 *
 * The files are written by pl/vivado/export_dfx.tcl into ps/bitstreams/.
 * The assembler looks for them in the include path, so the application needs
 * that folder as an include directory (see ps/README.md).
 *
 * They must be the .bin files (write_bitstream -bin_file), not the .bit files.
 */
    .section .rodata.dfx_bitstreams, "a", %progbits

    .balign 64
    .global cnn_partial_start
    .global cnn_partial_end
cnn_partial_start:
    .incbin "cnn_partial.bin"
cnn_partial_end:

    .balign 64
    .global snn_partial_start
    .global snn_partial_end
snn_partial_start:
    .incbin "snn_partial.bin"
snn_partial_end:

    .balign 64
