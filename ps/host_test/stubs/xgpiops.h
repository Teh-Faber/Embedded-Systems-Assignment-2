#ifndef XGPIOPS_H
#define XGPIOPS_H
#include "xil_types.h"
typedef struct { UINTPTR BaseAddr; } XGpioPs_Config;
typedef struct { XGpioPs_Config Config; } XGpioPs;
XGpioPs_Config *XGpioPs_LookupConfig(UINTPTR base);
int XGpioPs_CfgInitialize(XGpioPs *p, XGpioPs_Config *c, UINTPTR base);
void XGpioPs_SetDirectionPin(XGpioPs *p, u32 pin, u32 dir);
void XGpioPs_SetOutputEnablePin(XGpioPs *p, u32 pin, u32 en);
void XGpioPs_WritePin(XGpioPs *p, u32 pin, u32 v);
u32 XGpioPs_ReadPin(XGpioPs *p, u32 pin);
#endif
