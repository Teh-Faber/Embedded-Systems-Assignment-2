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
#ifndef XPAR_DS_CNN_0_BASEADDR
#error "Select the CNN platform or update the CNN control base address."
#endif

/* Control and result registers from the lab CNN IP. */
#define CNN_BASE_ADDR ((UINTPTR)XPAR_DS_CNN_0_BASEADDR)
#define REG_AP_CTRL 0x00u
#define REG_INPUT_IMG_L 0x10u
#define REG_INPUT_IMG_H 0x14u
#define REG_PRED_OUT 0x1Cu
#define REG_LOGITS_BASE 0x30u
#define AP_START (1u << 0)
#define AP_DONE (1u << 1)
#ifndef CNN_TIMEOUT_MS
#define CNN_TIMEOUT_MS 1000u
#endif

typedef struct {
    bool attempted;
    uint32_t duration_us;
} DfxTiming;

/* Keep the raw pixels for the whole request and use a separate CNN buffer. */
static ImageRequest request;
static int16_t cnn_input[IMAGE_SIZE] __attribute__((aligned(64)));

/* The controller owns hardware readiness. The lab bitstream contains the CNN
 * at startup. A timeout invalidates the slot until a restart or recovery. */
static uint8_t ready_model = MODEL_CNN;

static bool select_model(uint8_t model_id, DfxTiming *timing)
{
    *timing = (DfxTiming){0};
    /* Integration point: a DFX controller can load the requested module,
     * confirm readiness and set timing when it performs a switch. */
    return model_id == ready_model;
}

static uint32_t elapsed_us(XTime start, XTime stop)
{
    return (uint32_t)(((uint64_t)(stop - start) * 1000000ULL) /
                      (uint64_t)COUNTS_PER_SECOND);
}

/* Match the lab's Python sender: normalize p to 4*(p/255)-2, then store
 * the rounded Q8.8 value. The PL reads the resulting buffer from DDR. */
static void prepare_cnn_input(const uint8_t *pixels)
{
    for (size_t i = 0; i < IMAGE_SIZE; ++i) {
        int32_t pixel = pixels[i];
        cnn_input[i] = (int16_t)(((1024 * pixel + 127) / 255) - 512);
    }
    Xil_DCacheFlushRange((UINTPTR)cnn_input, sizeof(cnn_input));
}

static bool run_cnn(const uint8_t *pixels, ModelResult *result)
{
    XTime start, now;
    XTime_GetTime(&start);
    prepare_cnn_input(pixels);
    XTime_GetTime(&now);
    result->prep_us = elapsed_us(start, now);

    /* The IP expects a 64-bit buffer pointer. Cortex-A9 addresses fit in
     * the lower word. Reading AP_CTRL clears any stale completion flag. */
    Xil_Out32(CNN_BASE_ADDR + REG_INPUT_IMG_L, (uint32_t)(UINTPTR)cnn_input);
    Xil_Out32(CNN_BASE_ADDR + REG_INPUT_IMG_H, 0u);
    (void)Xil_In32(CNN_BASE_ADDR + REG_AP_CTRL);

    const XTime timeout = ((uint64_t)COUNTS_PER_SECOND * CNN_TIMEOUT_MS) / 1000u;
    XTime_GetTime(&start);
    Xil_Out32(CNN_BASE_ADDR + REG_AP_CTRL, AP_START);
    while ((Xil_In32(CNN_BASE_ADDR + REG_AP_CTRL) & AP_DONE) == 0u) {
        XTime_GetTime(&now);
        if (now - start >= timeout) {
            /* Protect the DDR buffer if the accelerator is still reading it. */
            ready_model = MODEL_NONE;
            return false;
        }
    }
    XTime_GetTime(&now);
    result->infer_us = elapsed_us(start, now);

    /* Sign-extend the four packed 16-bit logits into the shared result. */
    result->class_id = (uint8_t)Xil_In32(CNN_BASE_ADDR + REG_PRED_OUT);
    uint32_t lower = Xil_In32(CNN_BASE_ADDR + REG_LOGITS_BASE);
    uint32_t upper = Xil_In32(CNN_BASE_ADDR + REG_LOGITS_BASE + 4u);
    result->scores[0] = (int16_t)(lower & 0xFFFFu);
    result->scores[1] = (int16_t)(lower >> 16u);
    result->scores[2] = (int16_t)(upper & 0xFFFFu);
    result->scores[3] = (int16_t)(upper >> 16u);
    return result->class_id < CLASS_COUNT;
}

static uint8_t run_requested_model(uint8_t model_id, ModelResult *result)
{
    DfxTiming timing;
    bool ready = select_model(model_id, &timing);
    if (timing.attempted) {
        uart_send_dfx_timing(request.image_id, model_id, timing.duration_us);
    }
    /* Integration point: dispatch the SNN here after its controller is ready. */
    if (!ready || model_id != MODEL_CNN) {
        return UART_STATUS_MODEL_UNAVAILABLE;
    }
    return run_cnn(request.pixels, result) ? UART_STATUS_OK : UART_STATUS_INFERENCE_FAILED;
}

int main(void)
{
    Xil_ICacheEnable();
    Xil_DCacheEnable();
    int status = uart_init();
    if (status != XST_SUCCESS) {
        return status;
    }

    for (;;) {
        if (!uart_receive_request(&request)) {
            continue;
        }
        if (request.model_mask == MODEL_NONE) {
            uart_send_diagnostic(request.image_id, UART_STATUS_ECHO_UNAVAILABLE);
            continue;
        }

        /* Choose the execution order here, while the UART buffer stays valid. */
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
