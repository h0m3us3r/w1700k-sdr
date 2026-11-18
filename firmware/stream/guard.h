/* SPDX-License-Identifier: MIT */
/* The guard: which raw ops of a host's program the control interpreter does not run on a locked
 * WM (ctl.h). What they would reach lets the radio transmit, programs an eFuse, or is an access
 * the headers mark HAZARD because it wedges a die or the MCU until a power cycle.
 *
 * It is a list of what is known to be dangerous, not of everything that could be: a register
 * nobody has understood yet is not on it. Nothing here touches hardware, so the list is tested
 * as it is on the host (tests/guard_check.c).
 */
#ifndef STREAM_GUARD_H
#define STREAM_GUARD_H

#include <stdint.h>

#include "hw/adie/adie.h"
#include "hw/adie/mt7976.h"
#include "hw/adie/rfmodes.h"
#include "hw/mt7996/cap_route.h"
#include "hw/mt7996/conn.h"
#include "hw/mt7996/phy_cal.h"
#include "hw/mt7996/probe_page.h"
#include "hw/mt7996/secure.h"

/* An MCU address a host's program does not reach: `write` with the bits `set` it would set. */
static int ctl_mmio_denied(uint32_t addr, int write, uint32_t set)
{
	addr &= ~3u;
	/* The eFuse controller (secure.h: a write of 0x400000c0 programs a fuse) and the CONN
	 * semaphores (conn.h: a read is an acquire, and semaphore 12 held by a stranger ends every
	 * A-die access): neither read nor written. */
	if (addr - MT7996_EFUSE_CTRL_BASE < 0x1000u || addr - MT7996_CONN_SEMA_BASE < 0x400u)
		return 1;
	if (!write)
		return 0;
	/* The MAC's transmit and receive disables: the radio code's alone (radio.c). */
	if (addr == MT7996_LMAC_ARB_SCR(0) || addr == MT7996_LMAC_ARB_SCR(1) || addr == MT7996_LMAC_ARB_SCR(2))
		return 1;
	/* A capture route grant (cap_route.h HAZARD: the MCU hangs). */
	if (addr == MT7996_CAP_ROUTE(0x00) || addr == MT7996_CAP_ROUTE(0x08))
		return set != 0;
	if (addr == MT7996_CAP_ROUTE(0x04))
		return (set & 0x00ffffffu) != 0;
	return 0;
}

/* The same for an A-die register (die << 16 | k << 12 | index). */
static int ctl_adie_denied(uint32_t reg, int write, uint32_t set, int whole, uint32_t value)
{
	uint32_t k = HW_GET(MT7996_PROBE_CTL_AREG_K, reg), index = HW_GET(MT7996_PROBE_CTL_AREG_INDEX, reg);

	/* A raw chip-select: the "BT" and "GPS" selects are reached no other way, and a read of
	 * the latter blanks a die (adie.h HAZARDS). */
	if (k >= MT7996_PROBE_CTL_AREG_K_RAW_CS && k < ADIE_K_TOP)
		return 1;
	if (!write)	/* adie.h HAZARDS: reads that wedge a die's SPI until a power cycle */
		return (k == ADIE_K_TOP && index - 0x200u < 0x100u) || (k == 1 && index == 0x5aau);
	if (k < ADIE_NUM_K_WF)	/* a chain's transmit blocks and transmit detectors (rfmodes.h) */
		return index == ADIE_CHAIN_EN &&
		       (set & (ADIE_CHAIN_EN_TX_MASK | ADIE_CHAIN_EN_TXDET_A | ADIE_CHAIN_EN_TXDET_B)) != 0;
	/* TOP: the external front end's and antenna switches' pins (adie.h, mt7976.h), and the
	 * two words whose zeroing makes an MT7977 keep no later write (adie.h HAZARDS). */
	if ((index >= ADIE_TOP_FEM_490 && index <= ADIE_TOP_FEM_4A6) || index == MT7976_TOP_FEM)
		return 1;
	return whole && !value && (index == 0x060u || index == 0x664u);
}

/* Whether a raw op {op, a, b, cc} of a host's program is one the guard keeps from it. */
static int ctl_denied(uint32_t op, uint32_t a, uint32_t b, uint32_t cc)
{
	switch (HW_GET(MT7996_PROBE_CTL_OP_CODE, op)) {
	case MT7996_PROBE_CTL_OP_W32:
		return ctl_mmio_denied(a, 1, b);
	case MT7996_PROBE_CTL_OP_RMW32:
		return ctl_mmio_denied(a, 1, cc);
	case MT7996_PROBE_CTL_OP_R32:
	case MT7996_PROBE_CTL_OP_POLL:
		return ctl_mmio_denied(a, 0, 0);
	case MT7996_PROBE_CTL_OP_AW:
		return ctl_adie_denied(a, 1, b, 1, b);
	case MT7996_PROBE_CTL_OP_ARMW:	/* what it leaves is not known here: taken as leaving `cc` alone */
		return ctl_adie_denied(a, 1, cc, 1, cc);
	case MT7996_PROBE_CTL_OP_AR:
	case MT7996_PROBE_CTL_OP_APOLL:
		return ctl_adie_denied(a, 0, 0, 0, 0);
	}
	return 0;	/* DELAY and END reach nothing; SAR is the synthesizer's search and reaches only it */
}

#endif
