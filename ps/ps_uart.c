#include "ps.h"

#include <stddef.h>

#include "xiltimer.h"
#include "xparameters.h"
#include "xstatus.h"
#include "xuartps.h"

#ifndef SDT
#error "Build with the Vitis SDT standalone Cortex-A9 domain."
#endif

#ifndef UART_BASE_ADDR
#define UART_BASE_ADDR ((UINTPTR)XPAR_XUARTPS_0_BASEADDR)
#endif
#ifndef UART_BAUD
#define UART_BAUD 4000000u
#endif

/*
 * Request: A6 6A | length:u16 | version:u8 | type:u8 | image_id:u32 | body
 * Reply:   A5    | length:u16 | version:u8 | type:u8 | image_id:u32 | body | 5A
 * The length counts the body only. All multi-byte fields are little-endian.
 */
#define REQUEST_START_FIRST 0xA6u
#define REQUEST_START_SECOND 0x6Au
#define REPLY_START 0xA5u
#define REPLY_END 0x5Au
#define PROTOCOL_VERSION 1u
#define MESSAGE_IMAGE 1u
#define MESSAGE_RESULT 2u
#define MESSAGE_DIAGNOSTIC 3u
#define MESSAGE_DFX_TIMING 4u
#define HEADER_SIZE 8u
#define IMAGE_BODY_SIZE (1u + IMAGE_SIZE)
#define RESULT_BODY_SIZE (11u + 4u * CLASS_COUNT)

static XUartPs uart;

/* Decode and encode wire fields without relying on C structure padding. */
static uint16_t read_u16(const uint8_t *bytes)
{
    return (uint16_t)((uint16_t)bytes[0] | ((uint16_t)bytes[1] << 8u));
}

static uint32_t read_u32(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8u) |
           ((uint32_t)bytes[2] << 16u) | ((uint32_t)bytes[3] << 24u);
}

static void write_u32(uint8_t *bytes, uint32_t value)
{
    for (size_t i = 0; i < 4u; ++i) {
        bytes[i] = (uint8_t)value;
        value >>= 8u;
    }
}

int uart_init(void)
{
    XUartPs_Config *config = XUartPs_LookupConfig(UART_BASE_ADDR);
    if (config == NULL) {
        return XST_DEVICE_NOT_FOUND;
    }

    int status = XUartPs_CfgInitialize(&uart, config, config->BaseAddress);
    if (status != XST_SUCCESS) {
        return status;
    }

    /* The PC must use the same baud rate and 8N1 settings. */
    XUartPsFormat format = {
        .BaudRate = UART_BAUD,
        .DataBits = XUARTPS_FORMAT_8_BITS,
        .Parity = XUARTPS_FORMAT_NO_PARITY,
        .StopBits = XUARTPS_FORMAT_1_STOP_BIT,
    };
    XUartPs_SetOperMode(&uart, XUARTPS_OPER_MODE_NORMAL);
    XUartPs_SetInterruptMask(&uart, 0u);
    return XUartPs_SetDataFormat(&uart, &format);
}

/* Wait indefinitely for the first marker. Once a frame has started, a
 * half-second gap between bytes aborts it and allows the next marker search. */
static void wait_for_start(void)
{
    bool first_seen = false;
    for (;;) {
        uint8_t byte = XUartPs_RecvByte(UART_BASE_ADDR);
        if (first_seen && byte == REQUEST_START_SECOND) {
            return;
        }
        first_seen = byte == REQUEST_START_FIRST;
    }
}

static bool read_bytes(uint8_t *destination, size_t count)
{
    const XTime idle_limit = (XTime)(COUNTS_PER_SECOND) / 2u;
    for (size_t i = 0; i < count; ++i) {
        if (!XUartPs_IsReceiveData(UART_BASE_ADDR)) {
            XTime start, now;
            XTime_GetTime(&start);
            do {
                XTime_GetTime(&now);
                if (now - start >= idle_limit) {
                    return false;
                }
            } while (!XUartPs_IsReceiveData(UART_BASE_ADDR));
        }
        uint8_t byte = (uint8_t)XUartPs_ReadReg(UART_BASE_ADDR, XUARTPS_FIFO_OFFSET);
        if (destination != NULL) {
            destination[i] = byte;
        }
    }
    return true;
}

static void send_bytes(const uint8_t *bytes, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        XUartPs_SendByte(UART_BASE_ADDR, bytes[i]);
    }
}

/* Keep the header and trailer identical for every type of reply. */
static void send_header(uint8_t type, uint32_t image_id, uint16_t length)
{
    uint8_t header[1u + HEADER_SIZE] = {
        REPLY_START, (uint8_t)length, (uint8_t)(length >> 8u), PROTOCOL_VERSION, type
    };
    write_u32(header + 5u, image_id);
    send_bytes(header, sizeof(header));
}

static void send_frame(uint8_t type, uint32_t image_id, const uint8_t *body, uint16_t length)
{
    send_header(type, image_id, length);
    send_bytes(body, length);
    XUartPs_SendByte(UART_BASE_ADDR, REPLY_END);
}

bool uart_receive_request(ImageRequest *request)
{
    uint8_t header[HEADER_SIZE];
    wait_for_start();
    if (!read_bytes(header, sizeof(header))) {
        return false; /* There is no complete image ID to return yet. */
    }

    uint16_t length = read_u16(header);
    request->image_id = read_u32(header + 4u);
    uint8_t status = UART_STATUS_OK;
    if (length != IMAGE_BODY_SIZE) {
        status = UART_STATUS_BAD_LENGTH;
    } else if (header[2] != PROTOCOL_VERSION) {
        status = UART_STATUS_BAD_VERSION;
    } else if (header[3] != MESSAGE_IMAGE) {
        status = UART_STATUS_BAD_TYPE;
    }
    if (status != UART_STATUS_OK) {
        /* Discard a bounded malformed body before searching for another start. */
        if (length <= IMAGE_BODY_SIZE) {
            (void)read_bytes(NULL, length);
        }
        uart_send_diagnostic(request->image_id, status);
        return false;
    }

    /* Store the original pixels directly in the caller's image buffer. */
    if (!read_bytes(&request->model_mask, 1u) ||
        !read_bytes(request->pixels, sizeof(request->pixels))) {
        uart_send_diagnostic(request->image_id, UART_STATUS_TRUNCATED_REQUEST);
        return false;
    }
    if (request->model_mask > MODEL_BOTH) {
        uart_send_diagnostic(request->image_id, UART_STATUS_BAD_MASK);
        return false;
    }
    return true;
}

void uart_send_result(uint32_t image_id, uint8_t model_id, uint8_t status,
                      const ModelResult *result)
{
    /* An error has the same frame size, with class 255 and zeroed data. */
    uint8_t body[RESULT_BODY_SIZE] = {model_id, status, 0xFFu};
    if (result != NULL) {
        body[2] = result->class_id;
        write_u32(body + 3u, result->infer_us);
        write_u32(body + 7u, result->prep_us);
        for (size_t i = 0; i < CLASS_COUNT; ++i) {
            write_u32(body + 11u + 4u * i, (uint32_t)result->scores[i]);
        }
    }
    send_frame(MESSAGE_RESULT, image_id, body, sizeof(body));
}

void uart_send_diagnostic(uint32_t image_id, uint8_t status)
{
    send_frame(MESSAGE_DIAGNOSTIC, image_id, &status, 1u);
}

void uart_send_image_echo(const ImageRequest *request)
{
    /* Stream the echo from the received buffer without making a second copy. */
    send_header(MESSAGE_DIAGNOSTIC, request->image_id, IMAGE_BODY_SIZE);
    XUartPs_SendByte(UART_BASE_ADDR, UART_STATUS_OK);
    send_bytes(request->pixels, sizeof(request->pixels));
    XUartPs_SendByte(UART_BASE_ADDR, REPLY_END);
}

void uart_send_dfx_timing(uint32_t image_id, uint8_t model_id, uint32_t dfx_us)
{
    uint8_t body[5] = {model_id};
    write_u32(body + 1u, dfx_us);
    send_frame(MESSAGE_DFX_TIMING, image_id, body, sizeof(body));
}
