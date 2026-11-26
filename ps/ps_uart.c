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
 * Request: A6 6A | length:u16 | version:u8 | type:u8 | id:u32 | body
 * Reply:   A5    | length:u16 | version:u8 | type:u8 | id:u32 | body | 5A
 * The length counts the body only. All multi-byte fields are little-endian.
 */
#define REQUEST_START_FIRST 0xA6u
#define REQUEST_START_SECOND 0x6Au
#define REPLY_START 0xA5u
#define REPLY_END 0x5Au
#define PROTOCOL_VERSION 1u
#define HEADER_SIZE 8u

static XUartPs uart;

/* Decode and encode wire fields without relying on C structure padding. */
uint16_t get_u16(const uint8_t *bytes)
{
    return (uint16_t)((uint16_t)bytes[0] | ((uint16_t)bytes[1] << 8u));
}

uint32_t get_u32(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8u) |
           ((uint32_t)bytes[2] << 16u) | ((uint32_t)bytes[3] << 24u);
}

void put_u16(uint8_t *bytes, uint16_t value)
{
    bytes[0] = (uint8_t)value;
    bytes[1] = (uint8_t)(value >> 8u);
}

void put_u32(uint8_t *bytes, uint32_t value)
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

/* Non-blocking marker search: consume what is in the receive FIFO. The state
 * survives between calls, so a marker split across two calls is found. */
static bool start_received(void)
{
    static bool first_seen = false;
    while (XUartPs_IsReceiveData(UART_BASE_ADDR)) {
        uint8_t byte = (uint8_t)XUartPs_ReadReg(UART_BASE_ADDR, XUARTPS_FIFO_OFFSET);
        if (first_seen && byte == REQUEST_START_SECOND) {
            first_seen = false;
            return true;
        }
        first_seen = byte == REQUEST_START_FIRST;
    }
    return false;
}

/* Once a frame has started, a half-second gap between bytes aborts it and
 * allows the next marker search. */
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
        destination[i] = (uint8_t)XUartPs_ReadReg(UART_BASE_ADDR, XUARTPS_FIFO_OFFSET);
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
static void send_header(uint8_t type, uint32_t id, uint16_t length)
{
    uint8_t header[1u + HEADER_SIZE] = {
        REPLY_START, (uint8_t)length, (uint8_t)(length >> 8u), PROTOCOL_VERSION, type
    };
    put_u32(header + 5u, id);
    send_bytes(header, sizeof(header));
}

void uart_send_frame2(uint8_t type, uint32_t id, const uint8_t *part1, uint16_t length1,
                      const uint8_t *part2, uint16_t length2)
{
    send_header(type, id, (uint16_t)(length1 + length2));
    send_bytes(part1, length1);
    send_bytes(part2, length2);
    XUartPs_SendByte(UART_BASE_ADDR, REPLY_END);
}

void uart_send_frame(uint8_t type, uint32_t id, const uint8_t *body, uint16_t length)
{
    uart_send_frame2(type, id, body, length, NULL, 0u);
}

bool uart_poll_frame(UartFrame *frame)
{
    uint8_t header[HEADER_SIZE];
    if (!start_received()) {
        return false;
    }
    if (!read_bytes(header, sizeof(header))) {
        return false; /* There is no complete ID to answer to. */
    }
    frame->length = get_u16(header);
    frame->type = header[3];
    frame->id = get_u32(header + 4u);
    if (frame->length > UART_MAX_BODY) {
        uart_send_diagnostic(frame->id, UART_STATUS_BAD_LENGTH);
        return false; /* The marker search skips the body. */
    }
    if (!read_bytes(frame->body, frame->length)) {
        uart_send_diagnostic(frame->id, UART_STATUS_TRUNCATED_REQUEST);
        return false;
    }
    if (header[2] != PROTOCOL_VERSION) {
        uart_send_diagnostic(frame->id, UART_STATUS_BAD_VERSION);
        return false;
    }
    return true;
}

void uart_send_diagnostic(uint32_t id, uint8_t status)
{
    uart_send_frame(MSG_DIAGNOSTIC, id, &status, 1u);
}

void uart_send_dfx_timing(uint32_t id, uint8_t model_id, uint32_t dfx_us)
{
    uint8_t body[5] = {model_id};
    put_u32(body + 1u, dfx_us);
    uart_send_frame(MSG_DFX_TIMING, id, body, sizeof(body));
}
