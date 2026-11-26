#include "ps.h"

#include "sleep.h"
#include "xgpiops.h"
#include "xil_io.h"
#include "xstatus.h"

/*
 * IMU capture in the static PL (pl/hdl/imu_capture.vhd).
 *
 * The PL writes every sensor sample into a shared BRAM, raw and filtered,
 * with a timestamp, into rings of n_samples + 1 slots. The PS reads it over
 * GP0 like the BRAM of assignment 1: COUNT is the sequence lock. Sample k is
 * in slot k % slots; all its words are written before COUNT becomes k + 1.
 * While COUNT = c the PL may be writing sample c, whose slot is outside the
 * newest n_samples. A copy is therefore consistent if COUNT has the same value
 * before and after it (window), or if the sample is still among the newest
 * n_samples after it (single record).
 *
 * EMIO GPIO (pl/hdl/emio_io.vhd): pin 54 decouples the partition (ps_dfx.c),
 * pin 55 clears the capture, pin 56 reads SW1.
 */

#ifndef IMU_BRAM_BASE_ADDR
#define IMU_BRAM_BASE_ADDR ((UINTPTR)0x42000000u)  /* dfx_project.tcl: imu_bram_offset */
#endif
#ifndef GPIOPS_BASE_ADDR
#define GPIOPS_BASE_ADDR ((UINTPTR)0xE000A000u)
#endif
#ifndef IMU_CLEAR_PIN
#define IMU_CLEAR_PIN 55u
#endif
#ifndef IMU_SW1_PIN
#define IMU_SW1_PIN 56u
#endif

/* BRAM header, byte offsets (imu_capture.vhd) */
#define REG_MAGIC 0x00u
#define REG_LAYOUT 0x04u
#define REG_FORMAT 0x08u
#define REG_COUNT 0x0Cu
#define REG_STATUS 0x10u
#define REG_LAST_TS 0x14u
#define REG_SENSOR 0x18u
#define REG_SLOTS 0x1Cu
#define REG_FILT_OFF 0x20u
#define REG_RAW_OFF 0x24u
#define REG_TS_OFF 0x28u
#define REG_MEM_BYTES 0x2Cu

#define IMU_MAGIC 0x494D5531u       /* "IMU1" */
#define IMU_LAYOUT_VERSION 1u
#define RECORD_BYTES 12u            /* int16[6] */
#define WINDOW_TRIES 10u

static XGpioPs gpio;
static ImuInfo geom;
static bool ready = false;

static uint32_t rd(uint32_t offset)
{
    return Xil_In32(IMU_BRAM_BASE_ADDR + offset);
}

/* Header words that the PL writes with every sample (COUNT, STATUS, LAST_TS,
 * SENSOR). Both BRAM ports are WRITE_FIRST, so a read in the same clock cycle
 * as the PL's write of that word returns invalid data (UG473, "Conflict
 * Avoidance"). Seen on the board: a bad COUNT made the stream start again
 * from an old index. The write takes one cycle, so two equal reads in a row
 * give a valid value. */
static uint32_t rd_hdr(uint32_t offset)
{
    uint32_t a = rd(offset);
    for (uint32_t i = 0; i < 8u; ++i) {
        uint32_t b = rd(offset);
        if (a == b) {
            break;
        }
        a = b;
    }
    return a;
}

/* One record: three words, two int16 each, lower half first. */
static void read_record(uint32_t base, int16_t *dest)
{
    for (uint32_t w = 0; w < 3u; ++w) {
        uint32_t word = rd(base + 4u * w);
        dest[2u * w] = (int16_t)(word & 0xFFFFu);
        dest[2u * w + 1u] = (int16_t)(word >> 16u);
    }
}

int imu_init(ImuInfo *info)
{
    XGpioPs_Config *config = XGpioPs_LookupConfig(GPIOPS_BASE_ADDR);
    if (config == NULL) {
        return XST_DEVICE_NOT_FOUND;
    }
    int status = XGpioPs_CfgInitialize(&gpio, config, config->BaseAddr);
    if (status != XST_SUCCESS) {
        return status;
    }
    XGpioPs_SetDirectionPin(&gpio, IMU_CLEAR_PIN, 1u);
    XGpioPs_SetOutputEnablePin(&gpio, IMU_CLEAR_PIN, 1u);
    XGpioPs_WritePin(&gpio, IMU_CLEAR_PIN, 0u);
    XGpioPs_SetDirectionPin(&gpio, IMU_SW1_PIN, 0u);

    /* The PL writes the header right after its reset; give it a moment. */
    for (uint32_t i = 0; i < 100u && rd(REG_MAGIC) != IMU_MAGIC; ++i) {
        usleep(1000u);
    }
    uint32_t layout = rd(REG_LAYOUT);
    if (rd(REG_MAGIC) != IMU_MAGIC || (layout & 0xFFu) != IMU_LAYOUT_VERSION) {
        return XST_FAILURE;
    }
    geom.filter_id = (uint8_t)(layout >> 8u);
    geom.n_samples = (uint16_t)(layout >> 16u);
    geom.odr_hz = (uint16_t)rd(REG_FORMAT);
    geom.slots = (uint16_t)rd(REG_SLOTS);
    geom.filt_off = rd(REG_FILT_OFF);
    geom.raw_off = rd(REG_RAW_OFF);
    geom.ts_off = rd(REG_TS_OFF);
    uint32_t mem = rd(REG_MEM_BYTES);
    if (geom.n_samples == 0u || geom.n_samples > IMU_MAX_SAMPLES ||
        geom.slots != geom.n_samples + 1u ||
        geom.filt_off + RECORD_BYTES * geom.slots > geom.raw_off ||
        geom.raw_off + RECORD_BYTES * geom.slots > geom.ts_off ||
        geom.ts_off + 4u * geom.slots > mem) {
        return XST_FAILURE;
    }
    *info = geom;
    ready = true;
    return XST_SUCCESS;
}

void imu_clear(void)
{
    /* The PL holds the pipeline in reset while the pin is high, then writes
     * the header again with COUNT = 0. Both take well under a microsecond. */
    XGpioPs_WritePin(&gpio, IMU_CLEAR_PIN, 1u);
    usleep(10u);
    XGpioPs_WritePin(&gpio, IMU_CLEAR_PIN, 0u);
    usleep(10u);
}

bool imu_sw1(void)
{
    return ready && XGpioPs_ReadPin(&gpio, IMU_SW1_PIN) != 0u;
}

uint32_t imu_count(void)
{
    return ready ? rd_hdr(REG_COUNT) : 0u;
}

uint32_t imu_status(void)
{
    return ready ? rd_hdr(REG_STATUS) : 0u;
}

uint32_t imu_sensor(void)
{
    return ready ? rd_hdr(REG_SENSOR) : 0u;
}

uint32_t imu_last_ts(void)
{
    return ready ? rd_hdr(REG_LAST_TS) : 0u;
}

bool imu_read_record(uint32_t index, ImuRecord *record)
{
    if (!ready) {
        return false;
    }
    const uint32_t n = geom.n_samples;
    uint32_t before = rd_hdr(REG_COUNT);
    if (index >= before || before - index > n) {
        return false;                       /* not written yet / already overwritten */
    }
    uint32_t slot = index % geom.slots;
    record->ts_us = rd(geom.ts_off + 4u * slot);
    read_record(geom.raw_off + RECORD_BYTES * slot, record->raw);
    read_record(geom.filt_off + RECORD_BYTES * slot, record->filt);
    uint32_t after = rd_hdr(REG_COUNT);
    return after >= before && after - index <= n;   /* still not reused */
}

bool imu_read_window(int16_t *dest, uint32_t *end_index)
{
    if (!ready) {
        return false;
    }
    const uint32_t n = geom.n_samples;
    for (uint32_t attempt = 0; attempt < WINDOW_TRIES; ++attempt) {
        uint32_t before = rd_hdr(REG_COUNT);
        if (before < n) {
            return false;
        }
        uint32_t slot = (before - n) % geom.slots;
        for (uint32_t i = 0; i < n; ++i) {
            read_record(geom.filt_off + RECORD_BYTES * slot, dest + IMU_CHANNELS * i);
            slot = slot + 1u == geom.slots ? 0u : slot + 1u;
        }
        if (rd_hdr(REG_COUNT) == before) {
            *end_index = before;
            return true;
        }
    }
    return false;
}
