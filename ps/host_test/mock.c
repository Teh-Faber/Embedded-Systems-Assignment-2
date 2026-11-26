/*
 * Host-test stand-ins for the hardware the PS code talks to (ps/host_test).
 *
 *   UART          a pseudo-terminal; its name is written to $MOCK_PTY_FILE so
 *                 the PC tool can open it like a COM port
 *   capture BRAM  0x42000000: the first 16 KiB of the file $MOCK_SHM, written
 *                 by pl_sim.py with the same layout as imu_capture.vhd
 *   EMIO GPIO     words in the same file after the BRAM: clear requests and
 *                 acknowledgements, SW1, decouple
 *   accelerator   0x40000000: AP_START "runs" a fake model at once: s = sum of
 *                 the int16 inputs, class = s mod 5, score i = s + 1000 i
 *                 (16-bit packed for the CNN, 32-bit for the SNN, like the IPs)
 *   DFX           dfx_load() always succeeds and reports 1234 us
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "ps.h"
#include "xgpiops.h"
#include "xil_io.h"
#include "xiltimer.h"
#include "xstatus.h"
#include "xuartps.h"

#define BRAM_BASE 0x42000000u
#define BRAM_BYTES 16384u
#define ACCEL_BASE 0x40000000u
#define CTRL_WORDS 16u
enum { CTRL_CLEAR_REQ = 0, CTRL_CLEAR_ACK = 1, CTRL_SW1 = 2, CTRL_DECOUPLE = 3, CTRL_CLEAR_PIN = 4 };

static int uart_fd = -1;
static int rx_peek = -1;
static volatile uint32_t *bram;
static volatile uint32_t *ctrl;
static uint32_t accel_regs[0x100];
static uint8_t mock_model = MODEL_NONE;

__attribute__((constructor)) static void mock_setup(void)
{
    const char *shm = getenv("MOCK_SHM");
    const char *pty_file = getenv("MOCK_PTY_FILE");
    if (shm == NULL || pty_file == NULL) {
        fprintf(stderr, "set MOCK_SHM and MOCK_PTY_FILE\n");
        exit(2);
    }
    int fd = open(shm, O_RDWR);
    if (fd < 0) {
        perror(shm);
        exit(2);
    }
    void *p = mmap(NULL, BRAM_BYTES + 4u * CTRL_WORDS, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) {
        perror("mmap");
        exit(2);
    }
    bram = (volatile uint32_t *)p;
    ctrl = bram + BRAM_BYTES / 4u;

    uart_fd = posix_openpt(O_RDWR | O_NOCTTY);
    if (uart_fd < 0 || grantpt(uart_fd) || unlockpt(uart_fd)) {
        perror("pty");
        exit(2);
    }
    struct termios t;
    tcgetattr(uart_fd, &t);
    cfmakeraw(&t);
    tcsetattr(uart_fd, TCSANOW, &t);
    fcntl(uart_fd, F_SETFL, O_NONBLOCK);
    FILE *f = fopen(pty_file, "w");
    fprintf(f, "%s\n", ptsname(uart_fd));
    fclose(f);
}

/* ---- UART ---- */
XUartPs_Config *XUartPs_LookupConfig(UINTPTR base)
{
    static XUartPs_Config c;
    c.BaseAddress = base;
    return &c;
}
int XUartPs_CfgInitialize(XUartPs *p, XUartPs_Config *c, UINTPTR base) { (void)p; (void)c; (void)base; return XST_SUCCESS; }
void XUartPs_SetOperMode(XUartPs *p, u8 mode) { (void)p; (void)mode; }
void XUartPs_SetInterruptMask(XUartPs *p, u32 mask) { (void)p; (void)mask; }
int XUartPs_SetDataFormat(XUartPs *p, XUartPsFormat *f) { (void)p; (void)f; return XST_SUCCESS; }

int mock_uart_rx_ready(void)
{
    if (rx_peek >= 0) {
        return 1;
    }
    unsigned char c;
    if (read(uart_fd, &c, 1) == 1) {
        rx_peek = c;
        return 1;
    }
    usleep(20);   /* do not spin a host core at 100 % */
    return 0;
}

u8 mock_uart_read(void)
{
    while (!mock_uart_rx_ready()) {
    }
    u8 c = (u8)rx_peek;
    rx_peek = -1;
    return c;
}

u8 XUartPs_RecvByte(UINTPTR base) { (void)base; return mock_uart_read(); }

void XUartPs_SendByte(UINTPTR base, u8 b)
{
    (void)base;
    while (write(uart_fd, &b, 1) != 1) {
        if (errno != EAGAIN && errno != EIO) {
            perror("uart write");
            exit(3);
        }
        usleep(50);
    }
}

/* ---- timer ---- */
void XTime_GetTime(XTime *t)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint64_t ns = (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
    *t = ns * (uint64_t)(COUNTS_PER_SECOND) / 1000000000ull;
}

/* ---- GPIO ---- */
XGpioPs_Config *XGpioPs_LookupConfig(UINTPTR base)
{
    static XGpioPs_Config c;
    c.BaseAddr = base;
    return &c;
}
int XGpioPs_CfgInitialize(XGpioPs *p, XGpioPs_Config *c, UINTPTR base) { (void)p; (void)c; (void)base; return XST_SUCCESS; }
void XGpioPs_SetDirectionPin(XGpioPs *p, u32 pin, u32 dir) { (void)p; (void)pin; (void)dir; }
void XGpioPs_SetOutputEnablePin(XGpioPs *p, u32 pin, u32 en) { (void)p; (void)pin; (void)en; }

void XGpioPs_WritePin(XGpioPs *p, u32 pin, u32 v)
{
    (void)p;
    if (pin == 54u) {
        ctrl[CTRL_DECOUPLE] = v;
    } else if (pin == 55u) {
        if (ctrl[CTRL_CLEAR_PIN] && !v) {
            /* Falling edge: the PL has re-initialised by now in hardware.
             * Wait for pl_sim.py so the PS sees the same order of events. */
            uint32_t req = ctrl[CTRL_CLEAR_REQ] + 1u;
            ctrl[CTRL_CLEAR_REQ] = req;
            for (int i = 0; i < 20000 && ctrl[CTRL_CLEAR_ACK] != req; ++i) {
                usleep(100);
            }
        }
        ctrl[CTRL_CLEAR_PIN] = v;
    }
}

u32 XGpioPs_ReadPin(XGpioPs *p, u32 pin)
{
    (void)p;
    return pin == 56u ? (ctrl[CTRL_SW1] & 1u) : 0u;
}

/* ---- AXI ---- */
static void fake_inference(void)
{
    const int16_t *in = (const int16_t *)(uintptr_t)accel_regs[0x10 / 4];
    int n = getenv("MOCK_SAMPLES") ? atoi(getenv("MOCK_SAMPLES")) : 200;
    int64_t s = 0;
    for (int i = 0; i < n * 6; ++i) {
        s += in[i];
    }
    accel_regs[0x1C / 4] = (uint32_t)(((s % 5) + 5) % 5);
    for (int i = 0; i < 8; ++i) {
        int64_t v = s + 1000 * i;
        if (mock_model == MODEL_CNN) {
            uint32_t w = accel_regs[0x30 / 4 + i / 2];
            uint32_t h = (uint16_t)(int16_t)v;
            w = (i % 2) ? ((w & 0xFFFFu) | (h << 16)) : ((w & 0xFFFF0000u) | h);
            accel_regs[0x30 / 4 + i / 2] = w;
        } else {
            accel_regs[0x30 / 4 + i] = (uint32_t)(int32_t)v;
        }
    }
    accel_regs[0] = 0x2u;   /* ap_done */
}

u32 Xil_In32(UINTPTR addr)
{
    if (addr >= BRAM_BASE && addr < BRAM_BASE + BRAM_BYTES) {
        return bram[(addr - BRAM_BASE) / 4u];
    }
    if (addr >= ACCEL_BASE && addr < ACCEL_BASE + sizeof(accel_regs)) {
        return accel_regs[(addr - ACCEL_BASE) / 4u];
    }
    fprintf(stderr, "Xil_In32 from unmapped 0x%lx\n", (unsigned long)addr);
    return 0;
}

void Xil_Out32(UINTPTR addr, u32 value)
{
    if (addr >= ACCEL_BASE && addr < ACCEL_BASE + sizeof(accel_regs)) {
        accel_regs[(addr - ACCEL_BASE) / 4u] = value;
        if (addr == ACCEL_BASE && (value & 1u)) {
            fake_inference();
        }
        return;
    }
    fprintf(stderr, "Xil_Out32 to unmapped 0x%lx\n", (unsigned long)addr);
}

/* ---- DFX ---- */
int dfx_init(void) { return XST_SUCCESS; }

bool dfx_load(uint8_t model_id, uint32_t *duration_us)
{
    mock_model = model_id;
    *duration_us = 1234u;
    return true;
}
