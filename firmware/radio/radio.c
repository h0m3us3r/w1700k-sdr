// SPDX-License-Identifier: MIT
/* The radio commands (radio_cmd.h) and the order of the cold bring-up.
 *
 * The bring-up follows MediaTek's cold load, reduced to what reception needs (docs/roadmap.md):
 * the ROM patch's radio init; the UMAC pool layout, which our patch's hook turns into the
 * capture partition (MediaTek's WM makes this call while it boots); then, as the WM's PHY-init
 * routine does, the common init, the per-band PHY init of bands 0, 1, 2, item 0 with its R-cal
 * and RC calibration on each die (die = band), and the common tail. Channels are set afterwards,
 * one CHANSET per band; each runs the RX DC-offset calibration at the channel it set.
 */
#include "hw/mt7996/host.h"
#include "hw/mt7996/mcu_map.h"
#include "hw/mt7996/phy_cal.h"
#include "hw/mt7996/rom_patch.h"

#include "bandinit.h"
#include "capture.h"
#include "chanset.h"
#include "dcoc.h"
#include "patch_init.h"
#include "radio.h"
#include "radio_cmd.h"
#include "rfinit.h"

struct radio_id radio_id;
struct radio_cal radio_cal;
struct radio_band radio_band[RADIO_BANDS];
uint8_t radio_ee[RADIO_EE_BYTES] __attribute__((aligned(4)));

static uint32_t done(uint32_t *state, uint32_t bits)
{
	if (radio_failed())
		return RADIO_ERR_HW;
	*state |= bits;
	return RADIO_OK;
}

/* halUmacLibPlePseSysramInit(PLE banks, 0, PSE banks, 1) with the stock boot profile's counts:
 * our patch's layout hook inside it repartitions the pool for the capture (rom_patch.h). */
static uint32_t pool_layout(uint32_t *state)
{
	hal_rom_call4(MT7996_ROM_UMAC_PLE_PSE_INIT, MT7996_MCU_POOL_P4_PLE_BANKS, 0, MT7996_MCU_POOL_P4_PSE_BANKS, 1);
	return done(state, RADIO_STATE_POOL);
}

static uint32_t radio_step(uint32_t step, uint32_t arg, uint32_t *state)
{
	switch (step) {
	case RADIO_STEP_PATCH_INIT:
		if (patch_init() != RADIO_OK)
			return RADIO_ERR_HW;
		return done(state, RADIO_STATE_IDENTIFIED | RADIO_STATE_PATCH_INIT);
	case RADIO_STEP_POOL:
		return pool_layout(state);
	case RADIO_STEP_COMMON:
		if (!(*state & RADIO_STATE_PATCH_INIT))
			return RADIO_ERR_ORDER;
		phy_common_init();
		return done(state, RADIO_STATE_COMMON);
	case RADIO_STEP_BAND:
		if (arg >= RADIO_BANDS)
			return RADIO_ERR_ARG;
		if (!(*state & RADIO_STATE_COMMON))
			return RADIO_ERR_ORDER;
		phy_band_init(arg);
		return done(state, RADIO_STATE_BAND(arg));
	case RADIO_STEP_RF_INIT:
		if (arg >= RADIO_DIES)
			return RADIO_ERR_ARG;
		if (!(*state & RADIO_STATE_PATCH_INIT))
			return RADIO_ERR_ORDER;
		rf_init(arg);
		return done(state, RADIO_STATE_RF(arg));
	case RADIO_STEP_POST:
		if (!(*state & RADIO_STATE_COMMON))
			return RADIO_ERR_ORDER;
		phy_post_init();
		return done(state, RADIO_STATE_POST);
	case RADIO_STEP_DCOC:
		if (arg >= RADIO_BANDS)
			return RADIO_ERR_ARG;
		if (!(*state & RADIO_STATE_CHAN(arg)))
			return RADIO_ERR_ORDER;
		return dcoc_cal(arg);
	}
	return RADIO_ERR_ARG;
}

static uint32_t bringup(uint32_t *state)
{
	static const uint32_t steps[] = { RADIO_STEP_PATCH_INIT, RADIO_STEP_POOL, RADIO_STEP_COMMON };
	uint32_t i, res;

	*state = 0;
	/* Every band's configuration comes from the EEPROM image: refuse one that is not this chip's. */
	if (ee16(MT7996_PHY_EE_CHIP_ID) != MT7996_PCI_DEVICE_ID) {
		hal_fail(HAL_FAIL_ARG, ee16(MT7996_PHY_EE_CHIP_ID));
		return RADIO_ERR_ARG;
	}
	for (i = 0; i < sizeof(steps) / sizeof(steps[0]); i++)
		if ((res = radio_step(steps[i], 0, state)) != RADIO_OK)
			return res;
	for (i = 0; i < RADIO_BANDS; i++)
		if ((res = radio_step(RADIO_STEP_BAND, i, state)) != RADIO_OK)
			return res;
	for (i = 0; i < RADIO_DIES; i++)
		if ((res = radio_step(RADIO_STEP_RF_INIT, i, state)) != RADIO_OK)
			return res;
	if ((res = radio_step(RADIO_STEP_POST, 0, state)) != RADIO_OK)
		return res;
	*state |= RADIO_STATE_UP;
	return RADIO_OK;
}

static uint32_t setup(const uint32_t *arg, uint32_t *state)
{
	uint32_t band = arg[0], bw = HW_GET(RADIO_CHAN_BW, arg[3]), res;

	if (band >= RADIO_BANDS)
		return RADIO_ERR_ARG;
	if (!(*state & RADIO_STATE_UP))
		return RADIO_ERR_ORDER;
	*state &= ~(RADIO_STATE_CHAN(band) | RADIO_STATE_CAPTURE(band));
	res = chanset(band, (uint64_t)arg[2] << 32 | arg[1], bw, HW_GET(RADIO_CHAN_FLAGS, arg[3]));
	if (res != RADIO_OK)
		return res;
	*state |= RADIO_STATE_CHAN(band);
	if (arg[3] & RADIO_CHAN_NO_DCOC)
		return RADIO_OK;
	return dcoc_cal(band);
}

/* The band's MAC receive path on: ARB_SCR RX_DIS cleared. Without it the capture gets no samples
 * at all. MediaTek's per-band TX / RX state machine (V:e01245ec) clears TX_DIS and RX_DIS once the
 * host has started the band (its radio-enable commands); we clear RX_DIS and set TX_DIS, so the
 * MAC never transmits. TX_DIS is set at reset and nothing of ours clears it, but that the MAC
 * does not transmit is not left to what the word held before: it is written here, every time. */
static void mac_rx_enable(uint32_t band)
{
	hal_rmw(MT7996_LMAC_ARB_SCR(band), MT7996_LMAC_ARB_SCR_TX_DIS | MT7996_LMAC_ARB_SCR_RX_DIS,
		MT7996_LMAC_ARB_SCR_TX_DIS);
}

/* RATE band bw: the sample rate alone; the band's capture path must be set up again. */
static uint32_t rate(const uint32_t *arg, uint32_t *state)
{
	uint32_t res;

	if (arg[0] >= RADIO_BANDS)
		return RADIO_ERR_ARG;
	if (!(*state & RADIO_STATE_CHAN(arg[0])))
		return RADIO_ERR_ORDER;
	if ((res = chanset_rate(arg[0], arg[1])) != RADIO_OK)
		return res;
	*state &= ~RADIO_STATE_CAPTURE(arg[0]);
	return RADIO_OK;
}

static uint32_t capture(const uint32_t *arg, uint32_t *state)
{
	uint32_t res;

	if (arg[0] >= RADIO_BANDS)
		return RADIO_ERR_ARG;
	if (!(*state & RADIO_STATE_CHAN(arg[0])))
		return RADIO_ERR_ORDER;
	if ((res = capture_setup(arg[0], arg[1])) != RADIO_OK)
		return res;
	mac_rx_enable(arg[0]);
	if (radio_failed())
		return RADIO_ERR_HW;
	*state |= RADIO_STATE_CAPTURE(arg[0]);
	return RADIO_OK;
}

/* One command: RADIO_SELFTEST_MAGIC / RADIO_OK / RADIO_ERR_*; *state is the state word, updated. */
uint32_t radio_command(uint32_t op, const uint32_t *arg, uint32_t *state)
{
	switch (op) {
	case RADIO_OP_SELFTEST:
		return RADIO_SELFTEST_MAGIC;
	case RADIO_OP_IDENTIFY:
		radio_identify();
		return done(state, RADIO_STATE_IDENTIFIED);
	case RADIO_OP_BRINGUP:
		return bringup(state);
	case RADIO_OP_SETUP:
		return setup(arg, state);
	case RADIO_OP_STEP:
		return radio_step(arg[0], arg[1], state);
	case RADIO_OP_CAPTURE:
		return capture(arg, state);
	case RADIO_OP_RATE:
		return rate(arg, state);
	case RADIO_OP_DCOC:
		if (arg[0] >= RADIO_BANDS)
			return RADIO_ERR_ARG;
		if (!(*state & RADIO_STATE_CHAN(arg[0])))
			return RADIO_ERR_ORDER;
		return dcoc_cal(arg[0]);
	case RADIO_OP_SELECT:
		if (arg[0] >= RADIO_BANDS)
			return RADIO_ERR_ARG;
		capture_select(arg[0]);
		if (radio_failed())
			return RADIO_ERR_HW;
		*state = (*state & ~RADIO_STATE_SELECTED_ANY) | RADIO_STATE_SELECTED(arg[0]);
		return RADIO_OK;
	}
	return RADIO_ERR_OP;
}
