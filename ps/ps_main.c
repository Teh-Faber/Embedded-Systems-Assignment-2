#include "ps.h"

#include <stddef.h>

#include "xil_cache.h"
#include "xil_io.h"
#include "xiltimer.h"
#include "xparameters.h"
#include "xstatus.h"

#ifndef SDT
#error "Build with the Vitis SDT standalone Cortex-A9 domain."
#endif

/* PS_DFX=1 (default): the PL is the DFX design. The CNN and the SNN share one
 * reconfigurable partition and are swapped over PCAP (ps_dfx.c).
 * PS_DFX=0: a fixed bitstream with only the CNN, as in the lab. */
#ifndef PS_DFX
#define PS_DFX 1
#endif

/* AXI-Lite base address of the accelerator. Both modules sit behind the same
 * address in the DFX design. pl/vivado/dfx_project.tcl assigns 0x40000000. */
#ifndef ACCEL_BASE_ADDR
#if !PS_DFX && defined(XPAR_DS_CNN_0_BASEADDR)
#define ACCEL_BASE_ADDR ((UINTPTR)XPAR_DS_CNN_0_BASEADDR)
#else
#define ACCEL_BASE_ADDR ((UINTPTR)0x40000000u)
#endif
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
 * an IP's top-level arguments change (see ps/README.md). */
static const ModelRegisters model_registers[] = {
    [MODEL_CNN] = {.input_ptr = 0x10u, .prediction = 0x1Cu, .logits = 0x30u, .logit_bits = 16u},
    [MODEL_SNN] = {.input_ptr = 0x10u, .prediction = 0x1Cu, .logits = 0x30u, .logit_bits = 32u},
};

typedef struct {
    bool attempted;
    uint32_t duration_us;
} DfxTiming;

/* Keep the raw pixels for the whole request and use a separate buffer for the
 * accelerator. Both modules read the same Q8.8 image over their AXI master. */
static ImageRequest request;
static int16_t model_input[IMAGE_SIZE] __attribute__((aligned(64)));

/* The controller owns hardware readiness. A timeout or a failed switch
 * invalidates the partition until the next successful load. With DFX the
 * partition starts decoupled, so the first request always loads its module. */
#if PS_DFX
static uint8_t ready_model = MODEL_NONE;
#else
static uint8_t ready_model = MODEL_CNN;
#endif

static bool select_model(uint8_t model_id, DfxTiming *timing)
{
    *timing = (DfxTiming){0};
    if (model_id == ready_model) {
        return true;
    }
#if PS_DFX
    /* Nothing is running in the partition here: the previous inference has
     * either finished or timed out, and a reload also recovers a stuck IP. */
    timing->attempted = true;
    ready_model = dfx_load(model_id, &timing->duration_us) ? model_id : MODEL_NONE;
#endif
    return model_id == ready_model;
}

static uint32_t elapsed_us(XTime start, XTime stop)
{
    /* COUNTS_PER_SECOND expands to "a/2" without parentheses in xiltimer.h. */
    return (uint32_t)(((uint64_t)(stop - start) * 1000000ULL) /
                      (uint64_t)(COUNTS_PER_SECOND));
}

/* Match the lab's Python sender: normalize p to 4*(p/255)-2, then store
 * the rounded Q8.8 value. The PL reads the resulting buffer from DDR. */
static void prepare_input(const uint8_t *pixels)
{
    for (size_t i = 0; i < IMAGE_SIZE; ++i) {
        int32_t pixel = pixels[i];
        model_input[i] = (int16_t)(((1024 * pixel + 127) / 255) - 512);
    }
    Xil_DCacheFlushRange((UINTPTR)model_input, sizeof(model_input));
}

static void read_scores(const ModelRegisters *regs, int32_t scores[CLASS_COUNT])
{
    const UINTPTR base = ACCEL_BASE_ADDR + regs->logits;
    if (regs->logit_bits == 16u) {
        /* Sign-extend the packed 16-bit logits. */
        for (size_t i = 0; i < CLASS_COUNT; ++i) {
            uint32_t word = Xil_In32(base + 4u * (i / 2u));
            scores[i] = (int16_t)(word >> (16u * (i % 2u)));
        }
    } else {
        for (size_t i = 0; i < CLASS_COUNT; ++i) {
            scores[i] = (int32_t)Xil_In32(base + 4u * i);
        }
    }
}

static bool run_model(uint8_t model_id, const uint8_t *pixels, ModelResult *result)
{
    const ModelRegisters *regs = &model_registers[model_id];
    XTime start, now;
    XTime_GetTime(&start);
    prepare_input(pixels);
    XTime_GetTime(&now);
    result->prep_us = elapsed_us(start, now);

    /* The IP expects a 64-bit buffer pointer. Cortex-A9 addresses fit in
     * the lower word. Reading AP_CTRL clears any stale completion flag. */
    Xil_Out32(ACCEL_BASE_ADDR + regs->input_ptr, (uint32_t)(UINTPTR)model_input);
    Xil_Out32(ACCEL_BASE_ADDR + regs->input_ptr + 4u, 0u);
    (void)Xil_In32(ACCEL_BASE_ADDR + REG_AP_CTRL);

    const XTime timeout = ((uint64_t)(COUNTS_PER_SECOND) * MODEL_TIMEOUT_MS) / 1000u;
    XTime_GetTime(&start);
    Xil_Out32(ACCEL_BASE_ADDR + REG_AP_CTRL, AP_START);
    while ((Xil_In32(ACCEL_BASE_ADDR + REG_AP_CTRL) & AP_DONE) == 0u) {
        XTime_GetTime(&now);
        if (now - start >= timeout) {
            /* The accelerator may still be reading the DDR buffer. With DFX
             * the next request reloads and resets the partition. */
            ready_model = MODEL_NONE;
            return false;
        }
    }
    XTime_GetTime(&now);
    result->infer_us = elapsed_us(start, now);

    result->class_id = (uint8_t)Xil_In32(ACCEL_BASE_ADDR + regs->prediction);
    read_scores(regs, result->scores);
    return result->class_id < CLASS_COUNT;
}

static uint8_t run_requested_model(uint8_t model_id, ModelResult *result)
{
    DfxTiming timing;
    bool ready = select_model(model_id, &timing);
    if (timing.attempted) {
        uart_send_dfx_timing(request.image_id, model_id, timing.duration_us);
    }
    if (!ready) {
        return UART_STATUS_MODEL_UNAVAILABLE;
    }
    return run_model(model_id, request.pixels, result) ? UART_STATUS_OK
                                                       : UART_STATUS_INFERENCE_FAILED;
}

int main(void)
{
    Xil_ICacheEnable();
    Xil_DCacheEnable();
    int status = uart_init();
    if (status != XST_SUCCESS) {
        return status;
    }
#if PS_DFX
    status = dfx_init();
    if (status != XST_SUCCESS) {
        return status;
    }
#endif

    for (;;) {
        if (!uart_receive_request(&request)) {
            continue;
        }
        if (request.model_mask == MODEL_NONE) {
            uart_send_diagnostic(request.image_id, UART_STATUS_ECHO_UNAVAILABLE);
            continue;
        }

        /* Run the loaded module first, so a request for both costs one switch. */
        uint8_t first = ready_model == MODEL_SNN ? MODEL_SNN : MODEL_CNN;
        const uint8_t order[] = {first, first ^ MODEL_BOTH};
        for (size_t i = 0; i < sizeof(order); ++i) {
            uint8_t model_id = order[i];
            if ((request.model_mask & model_id) != 0u) {
                ModelResult result;
                uint8_t model_status = run_requested_model(model_id, &result);
                uart_send_result(request.image_id, model_id, model_status,
                                 model_status == UART_STATUS_OK ? &result : NULL);
            }
        }
    }
}
