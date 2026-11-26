#ifndef PS_H
#define PS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ------------------------------------------------------------------------
 * UART protocol, models and DFX
 * ------------------------------------------------------------------------ */

/* Model IDs double as flags in a request's model mask. */
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
    UART_STATUS_SW1_LIVE = 9,          /* SW1 is high: PC inference is refused */
    UART_STATUS_IMU_NOT_READY = 10,    /* no capture BRAM, or not enough samples yet */
    UART_STATUS_BAD_COMMAND = 11
};

/* Message types. Requests start with A6 6A, replies with A5 and end with 5A;
 * the header is length:u16 version:u8 type:u8 id:u32 (docs/imu_system.md). */
enum {
    MSG_DIAGNOSTIC = 3,     /* reply: status byte (also the IMU command acknowledgement) */
    MSG_DFX_TIMING = 4,     /* reply: duration of a partial reconfiguration */
    MSG_IMU_COMMAND = 16,   /* request: IMU capture command, see IMU_CMD_* */
    MSG_IMU_INFER = 17,     /* request: debug inference on a window sent by the PC */
    MSG_IMU_INFO = 18,      /* reply: capture configuration and state */
    MSG_IMU_SAMPLES = 19,   /* reply/stream: raw + filtered samples with timestamps */
    MSG_IMU_WINDOW = 20,    /* reply: the newest filtered window */
    MSG_IMU_RESULT = 21     /* reply: IMU inference result */
};

#define UART_MAX_BODY 6200u     /* the largest request: an IMU window of 512 samples */

/* One received frame. The body has been read completely and the version checked. */
typedef struct {
    uint8_t type;
    uint32_t id;
    uint16_t length;
    uint8_t body[UART_MAX_BODY];
} UartFrame;

/* Configure the PS UART. Return the Xilinx driver status. */
int uart_init(void);

/* Non-blocking: return true when a complete frame has been received. Bytes
 * before a start marker are skipped. A frame with a bad version, an oversized
 * body or a body that stops arriving is answered with a diagnostic and dropped. */
bool uart_poll_frame(UartFrame *frame);

/* Send one reply frame. The second form sends the body in two parts. */
void uart_send_frame(uint8_t type, uint32_t id, const uint8_t *body, uint16_t length);
void uart_send_frame2(uint8_t type, uint32_t id, const uint8_t *part1, uint16_t length1,
                      const uint8_t *part2, uint16_t length2);

/* Send a type 3 status reply. */
void uart_send_diagnostic(uint32_t id, uint8_t status);

/* Optional type 4 measurement. Send it before the corresponding model result. */
void uart_send_dfx_timing(uint32_t id, uint8_t model_id, uint32_t dfx_us);

void put_u16(uint8_t *bytes, uint16_t value);
void put_u32(uint8_t *bytes, uint32_t value);
uint16_t get_u16(const uint8_t *bytes);
uint32_t get_u32(const uint8_t *bytes);

/* ------------------------------------------------------------------------
 * Accelerator in the reconfigurable partition (ps_accel.c)
 * ------------------------------------------------------------------------ */

#define ACCEL_MAX_CLASSES 8u

typedef struct {
    uint8_t class_id;
    uint32_t infer_us;
    int32_t scores[ACCEL_MAX_CLASSES];
} AccelResult;

/* The module the partition holds now (MODEL_NONE after start-up with DFX). */
uint8_t accel_loaded_model(void);

/* Make model_id the loaded module, loading its partial bitstream if needed.
 * *dfx_us is set and *switched is true when a reconfiguration was attempted. */
bool accel_select(uint8_t model_id, bool *switched, uint32_t *dfx_us);

/* Run the loaded module on an input buffer in DDR and read n_classes scores.
 * The buffer is flushed from the data cache first. Returns a UART status:
 * OK, INFERENCE_FAILED (timeout or class out of range) or MODEL_UNAVAILABLE. */
uint8_t accel_run(uint8_t model_id, const void *input, size_t bytes, uint8_t n_classes,
                  AccelResult *result);

/* DFX over PCAP (ps_dfx.c). dfx_init() returns the Xilinx driver status and
 * leaves the reconfigurable partition decoupled. dfx_load() writes the partial
 * bitstream of MODEL_CNN or MODEL_SNN and couples the partition again. It
 * reports the time for the whole switch, also when it fails. */
int dfx_init(void);
bool dfx_load(uint8_t model_id, uint32_t *duration_us);

/* ------------------------------------------------------------------------
 * IMU capture in the static PL (ps_imu.c)
 * ------------------------------------------------------------------------ */

#define IMU_CHANNELS 6u                 /* a_x a_y a_z g_x g_y g_z */
#define IMU_MAX_SAMPLES 512u            /* largest window the PS accepts */

typedef struct {
    uint8_t filter_id;                  /* 0 none, 1 FIR, 2 IIR */
    uint16_t n_samples;                 /* window length */
    uint16_t slots;                     /* ring length = n_samples + 1 */
    uint16_t odr_hz;
    uint32_t filt_off, raw_off, ts_off; /* byte offsets of the rings in the BRAM */
} ImuInfo;

typedef struct {
    uint32_t ts_us;                     /* PL timestamp, microseconds since the capture reset */
    int16_t raw[IMU_CHANNELS];
    int16_t filt[IMU_CHANNELS];
} ImuRecord;

/* Check the capture BRAM header and set up the EMIO pins. Returns
 * XST_SUCCESS, or XST_FAILURE when the BRAM does not hold the IMU layout
 * (wrong bitstream). */
int imu_init(ImuInfo *info);

/* Clear the capture (EMIO 1): COUNT, timestamps and filter state restart. */
void imu_clear(void);

/* SW1 (EMIO 2), debounced in the PL. */
bool imu_sw1(void);

uint32_t imu_count(void);       /* samples since the last clear */
uint32_t imu_status(void);      /* STATUS word, see imu_capture.vhd */
uint32_t imu_sensor(void);      /* [7:0] WHO_AM_I, [15:8] last STATUS_REG */
uint32_t imu_last_ts(void);

/* Read sample `index` (0 = first after the clear). Returns false when it has
 * not been written yet or has already been overwritten. */
bool imu_read_record(uint32_t index, ImuRecord *record);

/* Copy the newest n_samples filtered samples, oldest first, as int16[n][6].
 * *end_index is the index after the newest one (COUNT at the time of the
 * copy). Returns false when fewer than n_samples have been captured. */
bool imu_read_window(int16_t *dest, uint32_t *end_index);

#endif
