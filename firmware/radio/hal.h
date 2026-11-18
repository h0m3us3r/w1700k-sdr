/* SPDX-License-Identifier: MIT */
/* The radio code's only way to the hardware.
 *
 * Every module in radio/ reaches registers through these calls and nothing else, with register
 * addresses and A-die indices from the hw/ headers. On the WM they are hal_wm.c; in the host test
 * (radio/tests/) a mock checks every call, in order, against an expected access list made from a
 * decoded MediaTek oracle, and serves the oracle's read values.
 *
 * D-die: 32-bit accesses on the WM's bus. A-die: die 0..2, bank k (0..4 the chain / synthesizer
 * banks, ADIE_K_TOP the TOP bank), 12-bit index; one SPI transaction each.
 *
 * Failures (an A-die transaction that does not complete, a poll that times out) are recorded,
 * the first one kept, and the call returns as if it had worked (a read returns 0): MediaTek's
 * firmware goes on through its own timeouts, and so does a sequence here. radio_failed() tells
 * the caller afterwards.
 */
#ifndef RADIO_HAL_H
#define RADIO_HAL_H

#include <stdint.h>

#include "hw/hw_bits.h"

uint32_t hal_rd(uint32_t addr);
void hal_wr(uint32_t addr, uint32_t val);
/* Read until (value & mask) == val or timeout_us; the last value read. A timeout is recorded
 * (HAL_FAIL_POLL) unless `soft`. */
uint32_t hal_poll(uint32_t addr, uint32_t mask, uint32_t val, uint32_t timeout_us, int soft);

uint32_t hal_ard(uint32_t die, uint32_t k, uint32_t idx);
void hal_awr(uint32_t die, uint32_t k, uint32_t idx, uint32_t val);
uint32_t hal_apoll(uint32_t die, uint32_t k, uint32_t idx, uint32_t mask, uint32_t val, uint32_t timeout_us,
		   int soft);

/* One eFuse byte through the ROM's read-only reader (hw/mt7996/secure.h
 * MT7996_ROM_EFUSE_READ8); 0 on success. hal_efuse8 records a failure, the _soft one does not. */
int hal_efuse8_soft(uint32_t addr, uint8_t *val);
void hal_fail(uint32_t code, uint32_t arg);
#define HAL_FAIL_EFUSE		6u	/* an eFuse read failed; arg = eFuse address */

static inline int hal_efuse8(uint32_t addr, uint8_t *val)
{
	if (hal_efuse8_soft(addr, val)) {
		hal_fail(HAL_FAIL_EFUSE, addr);
		return -1;
	}
	return 0;
}

/* A ROM routine with up to four word arguments (WM only: the ROM's gp, interrupts back off after);
 * its return value. */
uint32_t hal_rom_call4(uint32_t fn, uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3);

void hal_udelay(uint32_t us);
/* Microseconds since an arbitrary point; wraps at 2^32. */
uint32_t hal_now_us(void);

/* Failure record: the first failure's code and its argument (address or A-die register). */
#define HAL_FAIL_NONE		0u
#define HAL_FAIL_POLL		1u	/* D-die poll timed out; arg = address */
#define HAL_FAIL_APOLL		2u	/* A-die poll timed out; arg = HAL_AREG */
#define HAL_FAIL_SPI		3u	/* A-die transaction failed; arg = HAL_AREG */
#define HAL_FAIL_ARG		4u	/* a module refused its arguments; arg = module-defined */
#define HAL_FAIL_STATE		5u	/* a module found the hardware in an unexpected state */
#define HAL_FAIL_TRAP		7u	/* a load or store faulted during the command (the WM skips it and
					 * goes on with whatever was in the register); arg = its address */
#define HAL_AREG(die, k, idx)	((uint32_t)(die) << 16 | (uint32_t)(k) << 12 | (uint32_t)(idx))

struct hal_fail {
	uint32_t code, arg, count;
};
extern struct hal_fail hal_fail_rec;

void hal_fail(uint32_t code, uint32_t arg);

static inline int radio_failed(void)
{
	return hal_fail_rec.count != 0;
}

/* Read-modify-write as MediaTek's firmware does them: (old & ~mask) | val. */
static inline uint32_t hal_rmw(uint32_t addr, uint32_t mask, uint32_t val)
{
	uint32_t v = (hal_rd(addr) & ~mask) | val;

	hal_wr(addr, v);
	return v;
}

static inline uint32_t hal_set(uint32_t addr, uint32_t bits)
{
	return hal_rmw(addr, 0, bits);
}

static inline uint32_t hal_clr(uint32_t addr, uint32_t bits)
{
	return hal_rmw(addr, bits, 0);
}

/* A field: mask from HW_GENMASK/HW_BIT, value in field units. */
static inline uint32_t hal_wf(uint32_t addr, uint32_t mask, uint32_t field)
{
	return hal_rmw(addr, mask, (field << __builtin_ctz(mask)) & mask);
}

static inline uint32_t hal_armw(uint32_t die, uint32_t k, uint32_t idx, uint32_t mask, uint32_t val)
{
	uint32_t v = (hal_ard(die, k, idx) & ~mask) | val;

	hal_awr(die, k, idx, v);
	return v;
}

static inline uint32_t hal_awf(uint32_t die, uint32_t k, uint32_t idx, uint32_t mask, uint32_t field)
{
	return hal_armw(die, k, idx, mask, (field << __builtin_ctz(mask)) & mask);
}

#endif
