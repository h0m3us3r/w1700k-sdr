/* SPDX-License-Identifier: MIT */
/* One A-die SPI transaction from an MCU core, as the ROM's sys_spi driver does it: CONN semaphore
 * 12 taken and released around it, the master's busy bit polled before and after. Shared by the
 * control interpreter (ctl.h) and the radio code (radio/hal_wm.c).
 *
 * An A-die reg is die << 16 | k << 12 | index (MT7996_PROBE_CTL_AREG): k 0..4 a chain or the
 * synthesizer bank, 8..14 a raw chip-select 0..6, 15 the die's TOP bank.
 */
#ifndef STREAM_ADIE_SPI_H
#define STREAM_ADIE_SPI_H

#include <stdint.h>

#include "hw/adie/adie.h"
#include "hw/mt7996/mcu_periph.h"
#include "hw/mt7996/probe_page.h"

#ifndef R32
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#endif

static inline uint32_t adie_rdcycle(void)
{
	uint32_t v;

	asm volatile("csrr %0, mcycle" : "=r"(v));
	return v;
}

/* Where a register's transaction goes: the die's SPI block, the chip select, and the channel's
 * command, data and busy words. */
struct adie_route {
	uint32_t m, cs, cmd, wd, rd, busy;
};

static inline uint32_t adie_route(uint32_t reg, struct adie_route *r)	/* 0, or BAD_OP */
{
	uint32_t die = HW_GET(MT7996_PROBE_CTL_AREG_DIE, reg), k = HW_GET(MT7996_PROBE_CTL_AREG_K, reg);

	if (die >= ADIE_NUM_DIES || (k >= ADIE_NUM_K_WF && k < MT7996_PROBE_CTL_AREG_K_RAW_CS))
		return MT7996_PROBE_CTL_BAD_OP;
	/* k 8..14 names a raw chip-select 0..6 (the ROM's sys_spi ids: 2 "BT", 4 "GPS" are otherwise unreachable) */
	r->cs = k >= MT7996_PROBE_CTL_AREG_K_RAW_CS && k < ADIE_K_TOP ? k - MT7996_PROBE_CTL_AREG_K_RAW_CS :
		ADIE_K_TO_CS(k);
	r->m = ADIE_SPI_MCU_BASE(die);
	if (r->cs == ADIE_CS_TOP) {
		r->cmd = ADIE_SPI_TOP_CMD; r->wd = ADIE_SPI_TOP_WDATA; r->rd = ADIE_SPI_TOP_RDATA;
		r->busy = ADIE_SPI_STATUS_BUSY_TOP;
	} else if (r->cs == ADIE_CS_BT) {
		r->cmd = ADIE_SPI_CH1_CMD; r->wd = ADIE_SPI_CH1_WDATA; r->rd = ADIE_SPI_CH1_RDATA;
		r->busy = ADIE_SPI_STATUS_BUSY_CH1;
	} else {
		r->cmd = ADIE_SPI_CH0_CMD; r->wd = ADIE_SPI_CH0_WDATA; r->rd = ADIE_SPI_CH0_RDATA;
		r->busy = ADIE_SPI_STATUS_BUSY_CH0;
	}
	return 0;
}

/* The transfer's start, with the semaphore held and the channel idle: the command word, then
 * the data word, whose write starts it. */
static inline void adie_start(const struct adie_route *r, uint32_t reg, int read, uint32_t val)
{
	R32(r->m + r->cmd) = (R32(r->m + r->cmd) & ADIE_SPI_CMD_KEEP) | r->cs << HW_SHIFT(ADIE_SPI_CMD_CS) |
			     (uint32_t)read << HW_SHIFT(ADIE_SPI_CMD_READ) |
			     HW_PREP(ADIE_SPI_CMD_INDEX, HW_GET(MT7996_PROBE_CTL_AREG_INDEX, reg));
	(void)R32(r->m + r->wd);
	R32(r->m + r->wd) = read ? 0 : val;
}

/* One SPI transaction, waited for; 0 or a CTL code. (The control interpreter does not wait:
 * ctl_adie in ctl.h.) */
static inline uint32_t adie_xfer(uint32_t reg, int read, uint32_t *val)
{
	struct adie_route r;
	uint32_t t0;

	if (adie_route(reg, &r))
		return MT7996_PROBE_CTL_BAD_OP;
	t0 = adie_rdcycle();
	while (!ADIE_SEMA_GRANTED(R32(ADIE_SEMA_ACQ_MCU)))	/* a READ is the acquire */
		if (adie_rdcycle() - t0 > 1000u * MT7996_MCU_CYCLES_PER_US)
			return MT7996_PROBE_CTL_SEMA;
	if (R32(r.m + ADIE_SPI_STATUS) & r.busy) {
		R32(ADIE_SEMA_REL_MCU) = ADIE_SEMA_REL_VAL;
		return MT7996_PROBE_CTL_BUSY;
	}
	adie_start(&r, reg, read, read ? 0 : *val);
	t0 = adie_rdcycle();
	while (R32(r.m + ADIE_SPI_STATUS) & r.busy)
		if (adie_rdcycle() - t0 > 1000u * MT7996_MCU_CYCLES_PER_US) {
			R32(ADIE_SEMA_REL_MCU) = ADIE_SEMA_REL_VAL;
			return MT7996_PROBE_CTL_SPI;
		}
	if (read)
		*val = R32(r.m + r.rd);
	R32(ADIE_SEMA_REL_MCU) = ADIE_SEMA_REL_VAL;
	return MT7996_PROBE_CTL_OK;
}

static inline uint32_t adie_w(uint32_t reg, uint32_t v)
{
	return adie_xfer(reg, 0, &v);
}

#endif
