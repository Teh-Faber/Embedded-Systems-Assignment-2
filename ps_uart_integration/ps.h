#ifndef PS_H
#define PS_H

#include <stdbool.h>
#include <stdint.h>

#define IMAGE_WIDTH 80u
#define IMAGE_HEIGHT 60u
#define IMAGE_SIZE (IMAGE_WIDTH * IMAGE_HEIGHT)
#define CLASS_COUNT 4u

/* Model IDs double as flags in an image request's model mask. */
enum {
    MODEL_NONE = 0,
    MODEL_CNN = 1,
    MODEL_SNN = 2,
    MODEL_BOTH = MODEL_CNN | MODEL_SNN
};

enum {
    UART_STATUS_OK = 0,
    UART_STATUS_BAD_LENGTH = 1,
    UART_STATUS_BAD_MASK = 2,
    UART_STATUS_MODEL_UNAVAILABLE = 3,
    UART_STATUS_INFERENCE_FAILED = 4,
    UART_STATUS_BAD_VERSION = 5,
    UART_STATUS_BAD_TYPE = 6,
    UART_STATUS_TRUNCATED_REQUEST = 7,
    UART_STATUS_ECHO_UNAVAILABLE = 8,
    UART_STATUS_TEST_RESULT = 128
};

/* These are application data types. The UART module serializes each field. */
typedef struct {
    uint32_t image_id;
    uint8_t model_mask;
    uint8_t pixels[IMAGE_SIZE];
} ImageRequest;

typedef struct {
    uint8_t class_id;
    uint32_t infer_us;
    uint32_t prep_us;
    int32_t scores[CLASS_COUNT];
} ModelResult;

/* Configure the PS UART. Return the Xilinx driver status. */
int uart_init(void);

/* Wait for one request. Return false if a malformed request was dropped or rejected. */
bool uart_receive_request(ImageRequest *request);

/* Send one model's result. Pass NULL when the reply carries an error status. */
void uart_send_result(uint32_t image_id, uint8_t model_id, uint8_t status,
                      const ModelResult *result);

/* Send a type 3 status reply or a byte-for-byte test echo. */
void uart_send_diagnostic(uint32_t image_id, uint8_t status);
void uart_send_image_echo(const ImageRequest *request);

/* Optional type 4 measurement. Send it before the corresponding model result. */
void uart_send_dfx_timing(uint32_t image_id, uint8_t model_id, uint32_t dfx_us);

#endif
