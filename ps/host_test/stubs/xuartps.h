#ifndef XUARTPS_H
#define XUARTPS_H
#include "xil_types.h"
typedef struct { UINTPTR BaseAddress; } XUartPs_Config;
typedef struct { XUartPs_Config Config; } XUartPs;
typedef struct { u32 BaudRate; u32 DataBits; u32 Parity; u8 StopBits; } XUartPsFormat;
#define XUARTPS_FORMAT_8_BITS 0u
#define XUARTPS_FORMAT_NO_PARITY 4u
#define XUARTPS_FORMAT_1_STOP_BIT 0u
#define XUARTPS_OPER_MODE_NORMAL 0u
#define XUARTPS_FIFO_OFFSET 0x30u
XUartPs_Config *XUartPs_LookupConfig(UINTPTR base);
int XUartPs_CfgInitialize(XUartPs *p, XUartPs_Config *c, UINTPTR base);
void XUartPs_SetOperMode(XUartPs *p, u8 mode);
void XUartPs_SetInterruptMask(XUartPs *p, u32 mask);
int XUartPs_SetDataFormat(XUartPs *p, XUartPsFormat *f);
u8 XUartPs_RecvByte(UINTPTR base);
void XUartPs_SendByte(UINTPTR base, u8 b);
int mock_uart_rx_ready(void);
u8 mock_uart_read(void);
#define XUartPs_IsReceiveData(base) mock_uart_rx_ready()
#define XUartPs_ReadReg(base, off) mock_uart_read()
#endif
