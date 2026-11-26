#include "ps.h"

#include "xil_cache.h"
#include "xil_io.h"
#include "xiltimer.h"
#include "xparameters.h"
#include "xstatus.h"

/*
 * The accelerator in the reconfigurable partition: register access, start,
 * completion polling and model selection. The CNN and the SNN share the
 * partition and are swapped over PCAP (ps_dfx.c).
 */

/* AXI-Lite base address of the accelerator. Both modules sit behind the same
 * address. pl/vivado/dfx_project.tcl assigns 0x40000000. */
#ifndef ACCEL_BASE_ADDR
#define ACCEL_BASE_ADDR ((UINTPTR)0x40000000u)
#endif

#define REG_AP_CTRL 0x00u
#define AP_START (1u << 0)
#define AP_DONE (1u << 1)
#ifndef MODEL_TIMEOUT_MS
#define MODEL_TIMEOUT_MS 1000u
#endif

/* AXI-Lite register offsets of one HLS IP, from its generated x<top>_hw.h.
 * logit_bits is the width of one logits_out element: 16-bit elements are
 * packed two per register, 32-bit elements take one register each. */
typedef struct {
    uint8_t input_ptr;
    uint8_t prediction;
    uint8_t logits;
    uint8_t logit_bits;
} ModelRegisters;

/* Indexed by model ID. Check both rows against the generated headers each time
 * an IP's top-level arguments change (see ps/README.md). The values are those
 * of the placeholder modules in pl/ip. The IMU models keep the same argument
 * list (pl/ip/README.md), so the same offsets apply as long as that contract
 * is kept. */
static const ModelRegisters model_registers[] = {
    [MODEL_CNN] = {.input_ptr = 0x10u, .prediction = 0x1Cu, .logits = 0x30u, .logit_bits = 16u},
    [MODEL_SNN] = {.input_ptr = 0x10u, .prediction = 0x1Cu, .logits = 0x30u, .logit_bits = 32u},
};

/* The controller owns hardware readiness. A timeout or a failed switch
 * invalidates the partition until the next successful load. The partition
 * starts decoupled, so the first request always loads its module. */
static uint8_t ready_model = MODEL_NONE;

uint8_t accel_loaded_model(void)
{
    return ready_model;
}

bool accel_select(uint8_t model_id, bool *switched, uint32_t *dfx_us)
{
    *switched = false;
    *dfx_us = 0u;
    if (model_id == ready_model) {
        return true;
    }
    /* Nothing is running in the partition here: the previous inference has
     * either finished or timed out, and a reload also recovers a stuck IP. */
    *switched = true;
    ready_model = dfx_load(model_id, dfx_us) ? model_id : MODEL_NONE;
    return model_id == ready_model;
}

static uint32_t elapsed_us(XTime start, XTime stop)
{
    /* COUNTS_PER_SECOND expands to "a/2" without parentheses in xiltimer.h. */
    return (uint32_t)(((uint64_t)(stop - start) * 1000000ULL) /
                      (uint64_t)(COUNTS_PER_SECOND));
}

static void read_scores(const ModelRegisters *regs, uint8_t n, int32_t *scores)
{
    const UINTPTR base = ACCEL_BASE_ADDR + regs->logits;
    if (regs->logit_bits == 16u) {
        /* Sign-extend the packed 16-bit logits. */
        for (uint8_t i = 0; i < n; ++i) {
            uint32_t word = Xil_In32(base + 4u * (i / 2u));
            scores[i] = (int16_t)(word >> (16u * (i % 2u)));
        }
    } else {
        for (uint8_t i = 0; i < n; ++i) {
            scores[i] = (int32_t)Xil_In32(base + 4u * i);
        }
    }
}

uint8_t accel_run(uint8_t model_id, const void *input, size_t bytes, uint8_t n_classes,
                  AccelResult *result)
{
    if (model_id != ready_model || model_id == MODEL_NONE || n_classes > ACCEL_MAX_CLASSES) {
        return UART_STATUS_MODEL_UNAVAILABLE;
    }
    const ModelRegisters *regs = &model_registers[model_id];
    Xil_DCacheFlushRange((UINTPTR)input, bytes);

    /* The IP expects a 64-bit buffer pointer. Cortex-A9 addresses fit in
     * the lower word. Reading AP_CTRL clears any stale completion flag. */
    Xil_Out32(ACCEL_BASE_ADDR + regs->input_ptr, (uint32_t)(UINTPTR)input);
    Xil_Out32(ACCEL_BASE_ADDR + regs->input_ptr + 4u, 0u);
    (void)Xil_In32(ACCEL_BASE_ADDR + REG_AP_CTRL);

    const XTime timeout = ((uint64_t)(COUNTS_PER_SECOND) * MODEL_TIMEOUT_MS) / 1000u;
    XTime start, now;
    XTime_GetTime(&start);
    Xil_Out32(ACCEL_BASE_ADDR + REG_AP_CTRL, AP_START);
    while ((Xil_In32(ACCEL_BASE_ADDR + REG_AP_CTRL) & AP_DONE) == 0u) {
        XTime_GetTime(&now);
        if (now - start >= timeout) {
            /* The accelerator may still be reading the DDR buffer. With DFX
             * the next request reloads and resets the partition. */
            ready_model = MODEL_NONE;
            return UART_STATUS_INFERENCE_FAILED;
        }
    }
    XTime_GetTime(&now);
    result->infer_us = elapsed_us(start, now);

    result->class_id = (uint8_t)Xil_In32(ACCEL_BASE_ADDR + regs->prediction);
    read_scores(regs, n_classes, result->scores);
    return result->class_id < n_classes ? UART_STATUS_OK : UART_STATUS_INFERENCE_FAILED;
}
