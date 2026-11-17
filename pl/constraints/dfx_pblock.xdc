# Floorplan of the reconfigurable partition (dfx_static_i/rp) on the xc7z020.
#
# The partition gets the two lower clock region rows over the full width of
# the device (rows Y0-Y99). The static logic stays in the upper row, next to
# the PS. Full clock region height is what allows RESET_AFTER_RECONFIG on
# 7-series.
#
# Rough capacity of this area: 35k LUTs, 146 DSP48, 93 RAMB36.
# Starting point, not yet run through place and route. If a module does not
# fit, or the static design does, move the upper edge in whole clock regions
# (50 slice rows, 20 DSP rows, 10 RAMB36 rows) in the Device view.

create_pblock pblock_rp
add_cells_to_pblock [get_pblocks pblock_rp] [get_cells dfx_static_i/rp]
resize_pblock [get_pblocks pblock_rp] -add {SLICE_X0Y0:SLICE_X113Y99}
resize_pblock [get_pblocks pblock_rp] -add {DSP48_X0Y0:DSP48_X4Y39}
resize_pblock [get_pblocks pblock_rp] -add {RAMB18_X0Y0:RAMB18_X5Y39}
resize_pblock [get_pblocks pblock_rp] -add {RAMB36_X0Y0:RAMB36_X5Y19}
set_property SNAPPING_MODE ON [get_pblocks pblock_rp]
set_property RESET_AFTER_RECONFIG true [get_pblocks pblock_rp]
