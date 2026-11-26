#include "ps.h"

#include <string.h>

#include "xil_cache.h"
#include "xiltimer.h"
#include "xstatus.h"

/*
 * IMU application: training-data capture and debug inference.
 *
 *   PC --MSG_IMU_COMMAND-->  info, start/stop streaming, clear, window snapshot
 *   PS --MSG_IMU_SAMPLES-->  every new sample while streaming: timestamp, raw
 *                            and filtered a_x a_y a_z g_x g_y g_z
 *   PC --MSG_IMU_INFER---->  a window from the PC; the PS copies it to DDR,
 *   PS --MSG_IMU_RESULT--->  runs the requested model(s) and returns the result
 *
 * SW1 (EMIO 2): low = debug mode, the PC supplies the windows. High = live
 * mode, inference belongs to the filtered IMU windows, so PC inference
 * requests are refused (status UART_STATUS_SW1_LIVE). The live inference loop
 * itself is not part of this application yet; imu_read_window() is what it
 * will use (the WINDOW command already returns the same data to the PC).
 *
 * Wire formats: docs/imu_system.md. PC side: pc/imu_tool.py.
 */

#ifndef SDT
#error "Build with the Vitis SDT standalone Cortex-A9 domain."
#endif

#define APP_VERSION 1u

enum {
    IMU_CMD_INFO = 0,
    IMU_CMD_STREAM_START = 1,   /* arg: 1 = clear the capture first */
    IMU_CMD_STREAM_STOP = 2,
    IMU_CMD_CLEAR = 3,
    IMU_CMD_WINDOW = 4
};

#define STREAM_MAX_RECORDS 16u
#define STREAM_RECORD_BYTES 28u     /* ts u32, raw i16[6], filt i16[6] */
#define STREAM_HEADER_BYTES 9u      /* first_index u32, lost u32, count u8 */
#define INFER_HEADER_BYTES 4u       /* model_mask u8, n_classes u8, n_samples u16 */
#define RESULT_HEADER_BYTES 16u
#define NO_WINDOW_INDEX 0xFFFFFFFFu /* result for a window from the PC */

static UartFrame frame;
static ImuInfo info;
static bool imu_ready = false;
/* DDR buffer the accelerator reads: int16[n_samples][6], oldest sample first */
static int16_t model_input[IMU_MAX_SAMPLES * IMU_CHANNELS] __attribute__((aligned(64)));

static bool streaming = false;
static uint32_t next_index = 0;     /* next sample to stream */
static uint32_t stream_seq = 0;     /* frame ID of sample frames */

static uint32_t elapsed_us(XTime start, XTime stop)
{
    return (uint32_t)(((uint64_t)(stop - start) * 1000000ULL) /
                      (uint64_t)(COUNTS_PER_SECOND));
}

static void put_i16s(uint8_t *bytes, const int16_t *values, uint32_t n)
{
    for (uint32_t i = 0; i < n; ++i) {
        put_u16(bytes + 2u * i, (uint16_t)values[i]);
    }
}

static void send_info(uint32_t id)
{
    uint8_t body[28] = {APP_VERSION};
    body[1] = info.filter_id;
    put_u16(body + 2, info.n_samples);
    put_u16(body + 4, info.slots);
    put_u16(body + 6, info.odr_hz);
    put_u32(body + 8, imu_count());
    put_u32(body + 12, imu_status());
    put_u32(body + 16, imu_sensor());
    put_u32(body + 20, imu_last_ts());
    body[24] = imu_sw1() ? 1u : 0u;
    body[25] = accel_loaded_model();
    body[26] = imu_ready ? 1u : 0u;
    body[27] = streaming ? 1u : 0u;
    uart_send_frame(MSG_IMU_INFO, id, body, sizeof(body));
}

/* Send what has arrived since the last call, up to STREAM_MAX_RECORDS per
 * frame. Samples that were overwritten before they could be sent are counted
 * in `lost`, so the PC always knows whether its recording is complete. */
static void stream_poll(void)
{
    static uint8_t body[STREAM_HEADER_BYTES + STREAM_MAX_RECORDS * STREAM_RECORD_BYTES];
    if (!streaming) {
        return;
    }
    uint32_t count = imu_count();
    if (count < next_index) {
        next_index = 0;                     /* the capture was cleared */
    }
    if (count == next_index) {
        return;
    }
    uint32_t lost = 0;
    if (count - next_index > info.n_samples) {
        lost = count - info.n_samples - next_index;
        next_index = count - info.n_samples;
    }
    uint32_t first = next_index;
    uint8_t n = 0;
    while (n < STREAM_MAX_RECORDS && next_index < count) {
        ImuRecord r;
        if (!imu_read_record(next_index, &r)) {
            if (n != 0u) {
                break;                      /* keep the frame contiguous; retry next poll */
            }
            ++lost;                         /* overwritten while we were reading */
            ++next_index;
            first = next_index;
            continue;
        }
        uint8_t *p = body + STREAM_HEADER_BYTES + STREAM_RECORD_BYTES * n;
        put_u32(p, r.ts_us);
        put_i16s(p + 4, r.raw, IMU_CHANNELS);
        put_i16s(p + 16, r.filt, IMU_CHANNELS);
        ++n;
        ++next_index;
    }
    put_u32(body, first);
    put_u32(body + 4, lost);
    body[8] = n;
    uart_send_frame(MSG_IMU_SAMPLES, stream_seq++, body,
                    (uint16_t)(STREAM_HEADER_BYTES + STREAM_RECORD_BYTES * n));
}

static void send_window(uint32_t id)
{
    uint8_t header[6];
    uint32_t end_index;
    if (!imu_ready || !imu_read_window(model_input, &end_index)) {
        uart_send_diagnostic(id, UART_STATUS_IMU_NOT_READY);
        return;
    }
    put_u32(header, end_index);
    put_u16(header + 4, info.n_samples);
    /* int16 little-endian on the wire is the Cortex-A9's memory layout */
    uart_send_frame2(MSG_IMU_WINDOW, id, header, sizeof(header), (const uint8_t *)model_input,
                     (uint16_t)(info.n_samples * IMU_CHANNELS * sizeof(int16_t)));
}

static void handle_command(const UartFrame *f)
{
    if (f->length < 1u) {
        uart_send_diagnostic(f->id, UART_STATUS_BAD_LENGTH);
        return;
    }
    uint8_t cmd = f->body[0];
    if (cmd == IMU_CMD_INFO) {
        send_info(f->id);
        return;
    }
    if (!imu_ready) {
        uart_send_diagnostic(f->id, UART_STATUS_IMU_NOT_READY);
        return;
    }
    switch (cmd) {
    case IMU_CMD_STREAM_START:
        if (f->length >= 2u && f->body[1] != 0u) {
            imu_clear();
            next_index = 0;
        } else {
            next_index = imu_count();
        }
        streaming = true;
        uart_send_diagnostic(f->id, UART_STATUS_OK);
        break;
    case IMU_CMD_STREAM_STOP:
        streaming = false;
        uart_send_diagnostic(f->id, UART_STATUS_OK);
        break;
    case IMU_CMD_CLEAR:
        imu_clear();
        next_index = 0;
        uart_send_diagnostic(f->id, UART_STATUS_OK);
        break;
    case IMU_CMD_WINDOW:
        send_window(f->id);
        break;
    default:
        uart_send_diagnostic(f->id, UART_STATUS_BAD_COMMAND);
        break;
    }
}

static void send_result(uint32_t id, uint8_t model_id, uint8_t status, uint8_t n_classes,
                        const AccelResult *r, uint32_t prep_us)
{
    uint8_t body[RESULT_HEADER_BYTES + 4u * ACCEL_MAX_CLASSES] = {model_id, status, 0xFFu, n_classes};
    if (status == UART_STATUS_OK) {
        body[2] = r->class_id;
        put_u32(body + 4, r->infer_us);
        put_u32(body + 8, prep_us);
        for (uint8_t i = 0; i < n_classes; ++i) {
            put_u32(body + RESULT_HEADER_BYTES + 4u * i, (uint32_t)r->scores[i]);
        }
    }
    put_u32(body + 12, NO_WINDOW_INDEX);
    uart_send_frame(MSG_IMU_RESULT, id, body, (uint16_t)(RESULT_HEADER_BYTES + 4u * n_classes));
}

/* Debug inference: the PC sends exactly what the accelerator will read. */
static void handle_infer(const UartFrame *f)
{
    if (imu_sw1()) {
        uart_send_diagnostic(f->id, UART_STATUS_SW1_LIVE);
        return;
    }
    if (f->length < INFER_HEADER_BYTES) {
        uart_send_diagnostic(f->id, UART_STATUS_BAD_LENGTH);
        return;
    }
    uint8_t mask = f->body[0];
    uint8_t n_classes = f->body[1];
    uint16_t n_samples = get_u16(f->body + 2);
    uint32_t data_bytes = (uint32_t)n_samples * IMU_CHANNELS * sizeof(int16_t);
    if (n_samples == 0u || n_samples > IMU_MAX_SAMPLES || n_classes == 0u ||
        n_classes > ACCEL_MAX_CLASSES || f->length != INFER_HEADER_BYTES + data_bytes) {
        uart_send_diagnostic(f->id, UART_STATUS_BAD_LENGTH);
        return;
    }
    if (mask == MODEL_NONE || mask > MODEL_BOTH) {
        uart_send_diagnostic(f->id, UART_STATUS_BAD_MASK);
        return;
    }

    XTime start, now;
    XTime_GetTime(&start);
    memcpy(model_input, f->body + INFER_HEADER_BYTES, data_bytes);  /* little-endian int16 */
    XTime_GetTime(&now);
    uint32_t prep_us = elapsed_us(start, now);

    /* Run the loaded module first, so a request for both costs one switch. */
    uint8_t first = accel_loaded_model() == MODEL_SNN ? MODEL_SNN : MODEL_CNN;
    const uint8_t order[] = {first, first ^ MODEL_BOTH};
    for (uint32_t i = 0; i < sizeof(order); ++i) {
        uint8_t model_id = order[i];
        if ((mask & model_id) == 0u) {
            continue;
        }
        bool switched;
        uint32_t dfx_us;
        bool ready = accel_select(model_id, &switched, &dfx_us);
        if (switched) {
            uart_send_dfx_timing(f->id, model_id, dfx_us);
        }
        AccelResult r = {0};
        uint8_t status = ready ? accel_run(model_id, model_input, data_bytes, n_classes, &r)
                               : UART_STATUS_MODEL_UNAVAILABLE;
        send_result(f->id, model_id, status, n_classes, &r, prep_us);
    }
}

int main(void)
{
    Xil_ICacheEnable();
    Xil_DCacheEnable();
    int status = uart_init();
    if (status != XST_SUCCESS) {
        return status;
    }
    status = dfx_init();
    if (status != XST_SUCCESS) {
        return status;
    }
    /* Without the capture BRAM (wrong bitstream) the debug inference still
     * works; IMU commands answer UART_STATUS_IMU_NOT_READY. */
    imu_ready = imu_init(&info) == XST_SUCCESS;

    for (;;) {
        if (uart_poll_frame(&frame)) {
            switch (frame.type) {
            case MSG_IMU_COMMAND:
                handle_command(&frame);
                break;
            case MSG_IMU_INFER:
                handle_infer(&frame);
                break;
            default:
                uart_send_diagnostic(frame.id, UART_STATUS_BAD_TYPE);
                break;
            }
        }
        stream_poll();
    }
}
