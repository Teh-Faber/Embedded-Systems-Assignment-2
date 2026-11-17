#include "ps.h"

#include <stddef.h>

#include "sleep.h"
#include "xdevcfg.h"
#include "xgpiops.h"
#include "xil_cache.h"
#include "xiltimer.h"
#include "xstatus.h"

/*
 * DFX over PCAP for the one reconfigurable partition (RP) in the static PL.
 *
 * The static design (pl/vivado/dfx_project.tcl) gives the PS one control line:
 * EMIO GPIO bit 0. High means "decoupled": the DFX decoupler isolates the RP's
 * AXI interfaces and the RP is held in reset. dfx_load() raises it, writes the
 * partial bitstream through the devcfg DMA, and lowers it again.
 *
 * The device configuration unit and the PS GPIO have fixed addresses on every
 * Zynq-7000, so the lookups do not depend on names in xparameters.h.
 */
#ifndef DEVCFG_BASE_ADDR
#define DEVCFG_BASE_ADDR ((UINTPTR)0xF8007000u)
#endif
#ifndef GPIOPS_BASE_ADDR
#define GPIOPS_BASE_ADDR ((UINTPTR)0xE000A000u)
#endif
/* PS GPIO pins 0-53 are MIO. EMIO bit n is pin 54 + n. */
#ifndef DFX_DECOUPLE_PIN
#define DFX_DECOUPLE_PIN 54u
#endif
#ifndef DFX_TIMEOUT_MS
#define DFX_TIMEOUT_MS 2000u
#endif
/* Time the new module spends in reset with a running clock before it is
 * coupled, and the time allowed for the reset to release afterwards. */
#ifndef DFX_SETTLE_US
#define DFX_SETTLE_US 1000u
#endif

/* Tells the devcfg DMA that this is the only transfer of the bitstream, so
 * DMA_DONE is raised when both the AXI and the PCAP side have finished. */
#define PCAP_LAST_TRANSFER 1u

/* Partial bitstreams linked into the ELF by ps_bitstreams.S. */
extern const uint8_t cnn_partial_start[], cnn_partial_end[];
extern const uint8_t snn_partial_start[], snn_partial_end[];

static XDcfg devcfg;
static XGpioPs gpio;

static void set_decouple(bool decoupled)
{
    XGpioPs_WritePin(&gpio, DFX_DECOUPLE_PIN, decoupled ? 1u : 0u);
}

int dfx_init(void)
{
    XGpioPs_Config *gpio_config = XGpioPs_LookupConfig(GPIOPS_BASE_ADDR);
    if (gpio_config == NULL) {
        return XST_DEVICE_NOT_FOUND;
    }
    int status = XGpioPs_CfgInitialize(&gpio, gpio_config, gpio_config->BaseAddr);
    if (status != XST_SUCCESS) {
        return status;
    }
    /* Start decoupled. The RP is coupled by the first successful dfx_load(). */
    XGpioPs_SetDirectionPin(&gpio, DFX_DECOUPLE_PIN, 1u);
    XGpioPs_SetOutputEnablePin(&gpio, DFX_DECOUPLE_PIN, 1u);
    set_decouple(true);
    // The timer only starts on the first usleep(); start it now so the first dfx_load() is timed correctly. Without we get a 2000 micro second "deley" on first run.
    usleep(1);
    XDcfg_Config *devcfg_config = XDcfg_LookupConfig(DEVCFG_BASE_ADDR);
    if (devcfg_config == NULL) {
        return XST_DEVICE_NOT_FOUND;
    }
    return XDcfg_CfgInitialize(&devcfg, devcfg_config, devcfg_config->BaseAddr);
}

/* Poll the devcfg interrupt status for the given bits. Any error flag or the
 * timeout ends the wait. */
static bool wait_for_status(uint32_t mask)
{
    const XTime limit = ((uint64_t)(COUNTS_PER_SECOND) * DFX_TIMEOUT_MS) / 1000u;
    XTime start, now;
    XTime_GetTime(&start);
    for (;;) {
        uint32_t status = XDcfg_IntrGetStatus(&devcfg);
        if ((status & XDCFG_IXR_ERROR_FLAGS_MASK) != 0u) {
            return false;
        }
        if ((status & mask) == mask) {
            return true;
        }
        XTime_GetTime(&now);
        if (now - start >= limit) {
            return false;
        }
    }
}

static bool pcap_write(const uint8_t *bitstream, size_t size)
{
    /* The DMA reads whole words from physical memory. */
    if (size == 0u || (size & 3u) != 0u || ((UINTPTR)bitstream & 3u) != 0u) {
        return false;
    }
    Xil_DCacheFlushRange((UINTPTR)bitstream, size);

    /* Route partial reconfiguration through PCAP instead of the PL's ICAP.
     * PROG_B is left alone: pulsing it would erase the static design. */
    XDcfg_EnablePCAP(&devcfg);
    XDcfg_SetControlRegister(&devcfg, XDCFG_CTRL_PCAP_PR_MASK);
    XDcfg_IntrClear(&devcfg, 0xFFFFFFFFu);

    uint32_t status = XDcfg_Transfer(
        &devcfg, (void *)((UINTPTR)bitstream | PCAP_LAST_TRANSFER), (uint32_t)(size / 4u),
        (void *)(UINTPTR)XDCFG_DMA_INVALID_ADDRESS, 0u, XDCFG_NON_SECURE_PCAP_WRITE);
    if (status != (uint32_t)XST_SUCCESS) {
        return false;
    }
    /* A partial bitstream finishes with D_P_DONE. PCFG_DONE stays high the
     * whole time because the static design keeps running. */
    return wait_for_status(XDCFG_IXR_DMA_DONE_MASK) &&
           wait_for_status(XDCFG_IXR_D_P_DONE_MASK);
}

bool dfx_load(uint8_t model_id, uint32_t *duration_us)
{
    const uint8_t *bitstream;
    size_t size;
    if (model_id == MODEL_CNN) {
        bitstream = cnn_partial_start;
        size = (size_t)(cnn_partial_end - cnn_partial_start);
    } else if (model_id == MODEL_SNN) {
        bitstream = snn_partial_start;
        size = (size_t)(snn_partial_end - snn_partial_start);
    } else {
        return false;
    }

    XTime start, stop;
    XTime_GetTime(&start);
    set_decouple(true);
    bool loaded = pcap_write(bitstream, size);
    if (loaded) {
        /* Flip-flops in the new module are not guaranteed to start from their
         * initial values, so it gets reset cycles before it is coupled. */
        usleep(DFX_SETTLE_US);
        set_decouple(false);
        usleep(DFX_SETTLE_US);
    }
    /* On failure the RP stays decoupled: its contents are unknown. */
    XTime_GetTime(&stop);
    *duration_us = (uint32_t)(((uint64_t)(stop - start) * 1000000ULL) /
                              (uint64_t)(COUNTS_PER_SECOND));
    return loaded;
}
