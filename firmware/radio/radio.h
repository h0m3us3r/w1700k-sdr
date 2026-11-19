/* SPDX-License-Identifier: MIT */
/* The radio as our WM brings it up from cold: shared state of the radio modules.
 *
 * The host delivers the unit's EEPROM image (the calibration data MediaTek's driver sends with
 * EFUSE_CONTROL in buffer mode, 7,680 bytes) into radio_ee before the bring-up; every EEPROM
 * input of every module is read from it with ee8()/ee16(). radio_identify() reads the A-dies'
 * chip IDs and the D-die's revision once; the modules branch on them as MediaTek's firmware does.
 *
 * Modules (each with its own header): patch_init.h (the ROM patch's radio init), rfinit.h (A-die
 * RF init, R-cal, RC calibration), bandinit.h (the common PHY init and the per-band PHY init),
 * chanset.h (the receive-only channel set and the synthesizer), dcoc.h (RX DC-offset
 * calibration and its write-back). radio.c runs them in order.
 */
#ifndef RADIO_RADIO_H
#define RADIO_RADIO_H

#include <stdint.h>

#include "hw/adie/adie.h"
#include "hw/mt7996/conn.h"
#include "hal.h"

#define RADIO_EE_BYTES		7680u	/* host/w1700k EEPROM_BYTES */
#define RADIO_DIES		ADIE_NUM_DIES
#define RADIO_BANDS		3u

extern uint8_t radio_ee[RADIO_EE_BYTES];

static inline uint32_t ee8(uint32_t off)
{
	return off < RADIO_EE_BYTES ? radio_ee[off] : 0;
}

static inline uint32_t ee16(uint32_t off)
{
	return ee8(off) | ee8(off + 1) << 8;
}

static inline uint32_t ee32(uint32_t off)
{
	return ee16(off) | ee16(off + 2) << 16;
}

struct radio_id {
	uint32_t chip_id[RADIO_DIES];	/* ADIE_TOP_CHIP_ID of each die */
	uint32_t hw_rev;		/* MT7996_CBTOP_HW_REV */
	uint32_t identified;
};
extern struct radio_id radio_id;

/* What the bring-up measured, for later commands: each die's RC-calibration code (adie.h
 * ADIE_RCTRIM; the MT7977's first code), valid where rc_valid has the die's bit (rfinit.c). */
struct radio_cal {
	uint32_t rc_code[RADIO_DIES];
	uint32_t rc_valid;
};
extern struct radio_cal radio_cal;

/* Each band's tuning as the commands left it: the LO (SETUP, TUNE) and the bandwidth class (SETUP,
 * RATE); set once SETUP has run. */
struct radio_band {
	uint64_t lo_hz;
	uint64_t lock_hz;	/* the LO its synthesizer last locked at (a TUNE's); 0: never. A HOLD moves
				 * lo_hz away from it and leaves it */
	uint32_t bw, set;
};
extern struct radio_band radio_band[RADIO_BANDS];

/* Reads radio_id (once per bring-up, after the XO is up). */
static inline void radio_identify(void)
{
	for (uint32_t die = 0; die < RADIO_DIES; die++)
		radio_id.chip_id[die] = hal_ard(die, ADIE_K_TOP, ADIE_TOP_CHIP_ID);
	radio_id.hw_rev = hal_rd(MT7996_CBTOP_HW_REV);
	radio_id.identified = 1;
}

static inline int radio_is_mt7977(uint32_t die)
{
	return HW_GET(ADIE_TOP_CHIP_ID_PART, radio_id.chip_id[die]) == ADIE_TOP_CHIP_ID_PART_MT7977;
}

/* MediaTek's ECO byte: die 2's chip ID [7:0]; it selects the RF_INIT variant of both MT7977 dies. */
static inline uint32_t radio_eco(void)
{
	return HW_GET(ADIE_TOP_CHIP_ID_VER, radio_id.chip_id[2]);
}

#endif
