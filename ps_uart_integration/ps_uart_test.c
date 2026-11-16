#include "ps.h"

#include <stddef.h>

#include "xstatus.h"

static ImageRequest request;

/* Mark fixture replies and exercise both signed scores and multi-byte times.
 * The PC checks the exact values; these never enter model performance reports. */
static void send_fixture(uint8_t model_id)
{
    ModelResult result = {
        .class_id = (uint8_t)((request.pixels[0] + model_id) % CLASS_COUNT),
        .infer_us = 0x01020300u + model_id,
        .prep_us = 0x04050600u + model_id,
        .scores = {-100000 - (int32_t)model_id, 200000 + (int32_t)model_id,
                   request.pixels[IMAGE_SIZE / 2u], request.pixels[IMAGE_SIZE - 1u]},
    };
    uart_send_result(request.image_id, model_id, UART_STATUS_TEST_RESULT, &result);
}

int main(void)
{
    int status = uart_init();
    if (status != XST_SUCCESS) {
        return status;
    }

    uint8_t first = MODEL_CNN;
    for (;;) {
        if (!uart_receive_request(&request)) {
            continue;
        }
        if (request.model_mask == MODEL_NONE) {
            uart_send_image_echo(&request);
            continue;
        }
        /* Alternate order in paired tests so the PC must use model IDs. */
        const uint8_t order[] = {first, first ^ MODEL_BOTH};
        for (size_t i = 0; i < sizeof(order); ++i) {
            if ((request.model_mask & order[i]) != 0u) {
                send_fixture(order[i]);
            }
        }
        if (request.model_mask == MODEL_BOTH) {
            first ^= MODEL_BOTH;
        }
    }
}
