#ifndef XILTIMER_H
#define XILTIMER_H
#include "xil_types.h"
typedef u64 XTime;
void XTime_GetTime(XTime *t);
/* Like the real header: no parentheses around the division. */
#define COUNTS_PER_SECOND 666666687/2
#endif
