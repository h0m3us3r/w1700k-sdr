// SPDX-License-Identifier: MIT
/* The receive-only channel set (chanset.h): MediaTek's channel-switch handler V:e00b0a74 and its
 * channel flow V:e00d708a, in MediaTek's order, reduced to what reception needs. What each step
 * does, and what is left out, is in chanset.h and hw/mt7996/phy_cal.h "Channel set"; the tables
 * are hw/adie/rfchan.h (item 1) and hw/mt7996/phy_chan.h (PHY tables A and B, AGC rows, gain
 * steps).
 */
#include "hw/adie/rfchan.h"
#include "hw/adie/mt7976.h"
#include "hw/adie/sx.h"
#include "hw/mt7996/conn.h"
#include "hw/mt7996/mac.h"
#include "hw/mt7996/mcu_map.h"
#include "hw/mt7996/phy_chan.h"
#include "hw/mt7996/secure.h"

#include "chanset.h"
#include "radio.h"
#include "rfctl.h"

#define TOP ADIE_K_TOP

/* ------------------------------------------------------------------------------------------------
 * Tables (compact arrays built from the generated lists)
 * ------------------------------------------------------------------------------------------------ */

#define GRP(n, c)		{ (n), (c) },

/* item 1: the A-die walker's tables (V:e0051af2) */
struct rf_walk {
	const uint8_t (*grp)[2];	/* per group: rows, columns */
	const uint16_t *reg;		/* per row: 1 register, or 2 (P[2] != 1 / P[2] = 1) */
	const uint32_t *val;		/* per row: one value per column of its group */
	const uint8_t *colmap;		/* [key][group] */
	const void *keys;		/* u8 or u16 key values */
	uint16_t nkeys;
	uint8_t ngroups, pairs, keyw;
};

#define RF_REG1(r, ...)		(r),
#define RF_REG2(r0, r1, ...)	(r0), (r1),
#define RF_VALS(r, ...)		__VA_ARGS__,
#define RF_VALS2(r0, r1, ...)	__VA_ARGS__,

#define RF_TABLE(name, P, regm, valm, pairs_, keyt, keyw_)						\
	static const uint8_t name##_grp[][2] = { P##_GROUPS(GRP) };					\
	static const uint16_t name##_reg[] = { P##_ROWS(regm) };					\
	static const uint32_t name##_val[] = { P##_ROWS(valm) };					\
	static const uint8_t name##_colmap[] = P##_COLMAP;						\
	static const keyt name##_keys[] = P##_KEYS;							\
	static const struct rf_walk name = { name##_grp, name##_reg, name##_val, name##_colmap,	\
					     name##_keys, P##_NKEYS, P##_NGROUPS, pairs_, keyw_ }

RF_TABLE(rf_epa_common, RFCHAN_EPA_COMMON, RF_REG2, RF_VALS2, 1, uint8_t, 1);
RF_TABLE(rf_noepa_common, RFCHAN_NOEPA_COMMON, RF_REG2, RF_VALS2, 1, uint8_t, 1);
RF_TABLE(rf_epa_bw, RFCHAN_EPA_BW, RF_REG2, RF_VALS2, 1, uint8_t, 1);
RF_TABLE(rf_noepa_bw, RFCHAN_NOEPA_BW, RF_REG2, RF_VALS2, 1, uint8_t, 1);
RF_TABLE(rf_xlna, RFCHAN_XLNA, RF_REG1, RF_VALS, 0, uint8_t, 1);
RF_TABLE(rf_xpa, RFCHAN_XPA, RF_REG1, RF_VALS, 0, uint8_t, 1);
RF_TABLE(rf_epa_group, RFCHAN_EPA_GROUP, RF_REG2, RF_VALS2, 1, uint16_t, 2);
RF_TABLE(rf_noepa_group, RFCHAN_NOEPA_GROUP, RF_REG2, RF_VALS2, 1, uint16_t, 2);

#define RF_PRE(r0, r1, v)	{ { (r0), (r1) }, (v) },
static const struct {
	uint16_t reg[2];
	uint32_t val;
} rf_epa_bw_pre[] = { RFCHAN_EPA_BW_PRE_ROWS(RF_PRE) };

#define RF_EXTRA(g0, g1, k, r, v)	{ (g0), (g1), (k), (r), (v) },
static const struct {
	uint8_t g0, g1, k;
	uint16_t reg;
	uint32_t val;
} rf_extra[] = { RFCHAN_EXTRA(RF_EXTRA) };

#define BWGRP(g, lo, span)	{ (g), (lo), (span) },
struct bwgrp {
	uint8_t g;
	uint16_t lo, span;
};
static const struct bwgrp bwgrp_160[] = { RFCHAN_BWGRP_160(BWGRP) };
static const struct bwgrp bwgrp_320_epa[] = { RFCHAN_BWGRP_320_EPA(BWGRP) };
static const struct bwgrp bwgrp_320_noepa[] = { RFCHAN_BWGRP_320_NOEPA(BWGRP) };

#define FGRP(g, lo, span)	{ (g), (lo), (span) },
static const struct bwgrp freq_groups[] = { MT7996_CHSET_GROUPS(FGRP) };

/* PHY tables A and B (V:e00d917e) */
struct phy_table {
	const uint8_t (*grp)[2];
	const uint32_t *addr;
	const uint32_t *val;
	const uint8_t *colmap;
	const uint8_t *keys;
	uint8_t ngroups, nkeys;
};

#define PT_ADDR(a, ...)		(a),
#define PT_VALS(a, ...)		__VA_ARGS__,
#define PHY_TABLE(name, P)										\
	static const uint8_t name##_grp[][2] = { P##_GROUPS(GRP) };					\
	static const uint32_t name##_addr[] = { P##_ROWS(PT_ADDR) };					\
	static const uint32_t name##_val[] = { P##_ROWS(PT_VALS) };					\
	static const uint8_t name##_colmap[] = P##_COLMAP;						\
	static const uint8_t name##_keys[] = P##_KEYS;							\
	static const struct phy_table name = { name##_grp, name##_addr, name##_val, name##_colmap,	\
					       name##_keys, P##_NGROUPS, P##_NKEYS }

PHY_TABLE(tbl_a, MT7996_CHSET_TBL_A);
PHY_TABLE(tbl_b, MT7996_CHSET_TBL_B);

/* AGC table entries (V:e00d1a5e) */
#define AGC2(a, b)		{ (a), (b) },
#define AGC5(a, b, c, d, e)	{ (a), (b), (c), (d), (e) },
static const uint32_t agc_2g4[][MT7996_CHSET_AGC_COLS_2G4] = { MT7996_CHSET_AGC_2G4(AGC2) };
static const uint32_t agc_5g6g[][MT7996_CHSET_AGC_COLS_5G6G] = { MT7996_CHSET_AGC_5G6G(AGC5) };
static const uint8_t agc_col[] = MT7996_CHSET_AGC_COL;

/* gain-step tables (V:e00d1dc4) */
#define GROW(...)		{ __VA_ARGS__ },
static const int8_t gstep[][MT7996_CHSET_GPAIRS][2] = { MT7996_CHSET_GSTEP(GROW) };	/* { first, second } */
static const int8_t gthr[][MT7996_CHSET_GSLOTS] = { MT7996_CHSET_GTHR(GROW) };
static const uint16_t glin[] = MT7996_CHSET_GLIN;

#define ARRAY_SIZE(a)		(sizeof(a) / sizeof((a)[0]))

/* TUNE's program for the LO (prepare builds it to check the LO; the flow builds it again with the
 * band's new bandwidth for its frequency words) and its results. */
static struct rfctl_op tune_ops[RFCTL_MAX_OPS];
static uint32_t tune_res[RFCTL_MAX_OPS];

/* ------------------------------------------------------------------------------------------------
 * P block, keys and validation (no hardware access)
 * ------------------------------------------------------------------------------------------------ */

/* V:e00d41f6: the group of a centre frequency (kHz). */
static uint32_t freq_group(uint32_t khz)
{
	uint32_t mhz = khz / MT7996_CHSET_KHZ_PER_MHZ;

	for (uint32_t i = 0; i < ARRAY_SIZE(freq_groups); i++)
		if (mhz - freq_groups[i].lo <= freq_groups[i].span)
			return freq_groups[i].g;
	return 0;
}

/* What the channel record held, from the LO alone (ours: no channel numbers): the centre in kHz,
 * the band type's channel-0 frequency, the frequency group (V:e00d41f6; on an MT7977 an LO outside
 * every group takes the nearest one, the first below 4850 MHz and the last above 7125 MHz) and the
 * LO's number on the band type's 5 MHz raster (0 off the raster), which only MediaTek's few
 * channel-number rules read (rx_adie_ch95, BN_MODE [8], RMAC CHFREQ). */
static void lo_plan(struct chanset_ctx *c, uint64_t hz)
{
	uint32_t lo_mhz = freq_groups[0].lo, rem;

	c->hz = hz;

	c->khz = (uint32_t)rfctl_div64(hz + CHANSET_HZ_PER_KHZ / 2u, CHANSET_HZ_PER_KHZ, &rem);
	c->group = c->mt7977 ? freq_group(c->khz) : 0;
	if (c->mt7977 && !c->group)
		c->group = c->khz / MT7996_CHSET_KHZ_PER_MHZ < lo_mhz ? MT7996_CHSET_GROUP_5G_FIRST :
									  MT7996_CHSET_GROUP_6G_LAST;
	c->base_khz = !c->mt7977 ? MT7996_CHSET_BASE_KHZ_2G4 :
		      c->group <= MT7996_CHSET_GROUP_5G_LAST ? MT7996_CHSET_BASE_KHZ_5G : MT7996_CHSET_BASE_KHZ_6G;
	c->ch = c->khz > c->base_khz && (c->khz - c->base_khz) % MT7996_CHSET_CH_KHZ == 0 ?
		(c->khz - c->base_khz) / MT7996_CHSET_CH_KHZ : 0;
}

static int key_index(const void *keys, uint32_t keyw, uint32_t nkeys, uint32_t value)
{
	for (uint32_t i = 0; i < nkeys; i++) {
		uint32_t k = keyw == 1 ? ((const uint8_t *)keys)[i] : ((const uint16_t *)keys)[i];

		if (k == value)
			return (int)i;
	}
	return -1;
}

/* Item 1's BW group of the centre frequency (V:e0043328 EPA, V:e00435f0 no EPA): *sel = 1 at
 * BW320 when a group matched. */
static uint32_t bw_group(const struct chanset_ctx *c, uint32_t *sel)
{
	uint32_t mhz = c->khz / MT7996_CHSET_KHZ_PER_MHZ, n, i;
	const struct bwgrp *t;

	*sel = 0;
	if (c->bw == MT7996_CHSET_BW160) {
		t = bwgrp_160;
		n = ARRAY_SIZE(bwgrp_160);
	} else {
		if (c->epa && mhz == RFCHAN_BWGRP_320_EPA_5650_MHZ) {
			*sel = 1;
			return RFCHAN_BWGRP_320_EPA_5650;
		}
		t = c->epa ? bwgrp_320_epa : bwgrp_320_noepa;
		n = c->epa ? ARRAY_SIZE(bwgrp_320_epa) : ARRAY_SIZE(bwgrp_320_noepa);
	}
	for (i = 0; i < n; i++)
		if (mhz - t[i].lo <= t[i].span) {
			*sel = c->bw == MT7996_CHSET_BW320;
			return t[i].g;
		}
	return 0;
}

/* Item 1's temperature selector of the group tables (V:e0042780). */
static uint32_t temp_sel(const struct chanset_ctx *c)
{
	if (c->temp == RFCHAN_TEMP_HOT)
		return RFCHAN_TSEL_HOT;
	return c->temp == RFCHAN_TEMP_COLD ? RFCHAN_TSEL_COLD : 0;
}

/* The item-1 keys of one WF; -1 in a key whose value is not in its list. */
struct item1_keys {
	int common, bw, xlna, xpa, group;
};

static void item1_keys(const struct chanset_ctx *c, uint32_t wf, struct item1_keys *k)
{
	uint32_t g = c->group, sel, bg;

	if (c->epa)
		k->common = key_index(rf_epa_common.keys, 1, rf_epa_common.nkeys,
				      g * RFCHAN_KEY_GRP_STRIDE + wf +
				      (radio_eco() < RFCHAN_ECO_NEW ? RFCHAN_KEY_ECO_LT10 : 0));
	else
		k->common = key_index(rf_noepa_common.keys, 1, rf_noepa_common.nkeys,
				      g * RFCHAN_KEY_GRP_STRIDE + wf);
	k->bw = 0;
	if (c->bw == MT7996_CHSET_BW160 || c->bw == MT7996_CHSET_BW320) {
		bg = bw_group(c, &sel);
		if (c->epa)
			k->bw = key_index(rf_epa_bw.keys, 1, rf_epa_bw.nkeys,
					  bg * RFCHAN_KEY_GRP_STRIDE + wf + sel * RFCHAN_KEY_EPA_BW320);
		else
			k->bw = key_index(rf_noepa_bw.keys, 1, rf_noepa_bw.nkeys,
					  bg * RFCHAN_KEY_GRP_STRIDE + wf + sel * RFCHAN_KEY_NOEPA_BW320);
	}
	k->xlna = key_index(rf_xlna.keys, 1, rf_xlna.nkeys, c->elna * RFCHAN_KEY_GRP_STRIDE + wf);
	k->xpa = key_index(rf_xpa.keys, 1, rf_xpa.nkeys, c->epa * RFCHAN_KEY_GRP_STRIDE + wf);
	k->group = 0;
	if (g)
		k->group = key_index(c->epa ? rf_epa_group.keys : rf_noepa_group.keys, 2,
				     c->epa ? rf_epa_group.nkeys : rf_noepa_group.nkeys,
				     g * RFCHAN_KEY_GRP_STRIDE + wf + temp_sel(c) * RFCHAN_KEY_TEMP);
}

/* Key record of table A (V:e00d17da) with k3 = chain, and its key value (V:e00d9046). */
static uint32_t tbl_a_key(const struct chanset_ctx *c, uint32_t chain)
{
	uint32_t k0, k2;

	k0 = c->group ? MT7996_CHSET_TBL_A_K0_5G6G : MT7996_CHSET_TBL_A_K0_2G4;
	k2 = c->group ? MT7996_CHSET_TBL_A_K2_6G5G :
	     c->khz == MT7996_CHSET_2G4_CH14_KHZ ? MT7996_CHSET_TBL_A_K2_CH14 : 0;
	if (c->khz > MT7996_CHSET_TBL_A_K0_HI_KHZ && !c->epa)
		k0 = MT7996_CHSET_TBL_A_K0_HI;
	return MT7996_PHY_CHTBL_A_KEY(k0, c->epa, k2, chain);
}

/* Band 0 wider than 40 MHz (ours, chanset.h): MediaTek's 2.4 GHz rows of table B and of the AGC
 * table end at BW40. */
static int wide_2g4(const struct chanset_ctx *c)
{
	return c->band == MT7996_PHY_BAND_2G4 && c->bw > MT7996_CHSET_BW40;
}

/* Table B's key value for RF type k0 at BW class k1 (V:e00d90b6; a 5 GHz key at BW320 takes the
 * 6 GHz column). */
static uint32_t tbl_b_keyval(const struct chanset_ctx *c, uint32_t k0, uint32_t k1)
{
	if (k1 == MT7996_CHSET_BW320 && k0 == MT7996_CHSET_TBL_B_K0_5G)
		k0 = MT7996_CHSET_TBL_B_K0_6G;
	return MT7996_PHY_CHTBL_B_KEY(k0, k1, c->nss_rx - 1, !c->elna);
}

/* Key record of table B (V:e00d1836): k0, k1 into *k0 / *k1 (the fields read them), and the key
 * indexes of the walk: key[0] the column walked; key[1] / key[2] -1, or for band 0 above 40 MHz
 * the 5 GHz BW40 and 5 GHz bandwidth columns that key[0], the 2.4 GHz BW40 column, takes bits from
 * (tbl_b_wide). -1 when a column is missing. */
static int tbl_b_cols(const struct chanset_ctx *c, int key[3], uint32_t *k0p, uint32_t *k1p)
{
	uint32_t g = c->group, k0;

	k0 = !g ? MT7996_CHSET_TBL_B_K0_2G4 : g <= MT7996_CHSET_GROUP_5G_LAST ? MT7996_CHSET_TBL_B_K0_5G :
							MT7996_CHSET_TBL_B_K0_6G;
	*k0p = k0;
	*k1p = c->bw;
	key[1] = key[2] = -1;
	if (!wide_2g4(c)) {
		key[0] = key_index(tbl_b.keys, 1, tbl_b.nkeys, tbl_b_keyval(c, k0, c->bw));
		return key[0] < 0 ? -1 : 0;
	}
	key[0] = key_index(tbl_b.keys, 1, tbl_b.nkeys, tbl_b_keyval(c, k0, MT7996_CHSET_BW40));
	key[1] = key_index(tbl_b.keys, 1, tbl_b.nkeys,
			   tbl_b_keyval(c, MT7996_CHSET_TBL_B_K0_5G, MT7996_CHSET_BW40));
	key[2] = key_index(tbl_b.keys, 1, tbl_b.nkeys, tbl_b_keyval(c, MT7996_CHSET_TBL_B_K0_5G, c->bw));
	return key[0] < 0 || key[1] < 0 || key[2] < 0 ? -1 : 0;
}

/* R1: the P block (V:e00d4a1e mode 2), the channel record from the LO (lo_plan) and the WF range
 * (V:e00d6232), and every key the channel set will look up; RADIO_ERR_ARG for an LO the band's
 * synthesizer does not reach or anything the tables do not cover. No primary 20 MHz channel: its
 * index (V:e00e55b0) is 0, the lowest. */
static uint32_t chanset_prepare(struct chanset_ctx *c, uint32_t band, uint64_t hz, uint32_t bw, uint32_t flags)
{
	uint32_t ee, wf;
	struct item1_keys k;

	if (band >= RADIO_BANDS || bw >= MT7996_CHSET_NBW)
		return RADIO_ERR_ARG;
	/* band 0 up to 160 MHz: at the 320 MHz class its PHY delivers no samples (chanset.h) */
	if (band == MT7996_PHY_BAND_2G4 && bw > MT7996_CHSET_BW160)
		return RADIO_ERR_ARG;
	c->band = band;
	c->mt7977 = radio_is_mt7977(band);
	c->bw = bw;
	c->flags = flags;
	c->sec = 0;
	lo_plan(c, hz);
	ee = ee8(MT7996_PHY_EE_196(band));
	c->epa = HW_GET(MT7996_PHY_EE_196_EPA, ee);
	c->elna = HW_GET(MT7996_PHY_EE_196_ELNA, ee);
	c->fem = c->epa ? HW_GET(MT7996_PHY_EE_196_P1C, ee) : 0;
	/* P[8]: MediaTek's temperature class from the thermal sensor (V:e00af328: 0 for 1..110 C);
	 * the thermal path is left out and the class taken as 0, the normal range */
	c->temp = 0;
	c->wf_first = 0;
	c->wf_last = MT7996_CHSET_WF_LAST(band);
	c->nss_tx = c->nss_rx = MT7996_CHSET_NSS_STOCK;

	/* keys */
	{
		uint32_t k0, k1;
		int key[3];

		if (tbl_b_cols(c, key, &k0, &k1) < 0)
			return RADIO_ERR_ARG;
	}
	if (flags & CHANSET_F_TABLE_A)
		for (wf = c->wf_first; wf <= MT7996_CHSET_TBL_A_LAST_CHAIN; wf++)
			if (key_index(tbl_a.keys, 1, tbl_a.nkeys, tbl_a_key(c, wf)) < 0)
				return RADIO_ERR_ARG;
	if (c->mt7977)
		for (wf = c->wf_first; wf <= c->wf_last; wf++) {
			item1_keys(c, wf, &k);
			if (k.common < 0 || k.bw < 0 || k.xlna < 0 || k.xpa < 0 || k.group < 0)
				return RADIO_ERR_ARG;
		}
	if (!wide_2g4(c) && agc_col[bw] >= (band == MT7996_PHY_BAND_2G4 ? ARRAY_SIZE(agc_2g4[0]) :
										 ARRAY_SIZE(agc_5g6g[0])))
		return RADIO_ERR_ARG;
	/* the LO within the synthesizer's reach: TUNE's program builds (rfctl_tune) */
	{
		uint64_t actual;
		uint32_t health;

		if (!rfctl_tune(band, hz, tune_ops, RFCTL_MAX_OPS, &actual, &health))
			return RADIO_ERR_ARG;
	}
	return RADIO_OK;
}

/* ------------------------------------------------------------------------------------------------
 * The D414 debug key (MT7996_PHY_RFIF_D414): every table helper reads the word twice, then tests
 * the first read's [27:16] and the second's bit.
 * ------------------------------------------------------------------------------------------------ */

static int d414_skip(uint32_t band, uint32_t bit)
{
	uint32_t a = hal_rd(MT7996_PHY_RFIF_D414(band)), b = hal_rd(MT7996_PHY_RFIF_D414(band));

	return HW_GET(MT7996_PHY_RFIF_D414_LOGKEY, a) == MT7996_PHY_RFIF_D414_LOGKEY_ON && (b & bit);
}

/* ------------------------------------------------------------------------------------------------
 * R2: item 1, the MT7977 per-channel A-die tables (V:e0040fe2)
 * ------------------------------------------------------------------------------------------------ */

static void rf_awr(uint32_t die, uint32_t wf, uint32_t idx, uint32_t v)
{
	if (wf == RFCHAN_WF_ALL)
		for (uint32_t k = 0; k < RFCHAN_NBANKS; k++)
			hal_awr(die, k, idx, v);
	else
		hal_awr(die, wf, idx, v);
}

/* V:e0051af2(desc, idx, vals, colmap, ncols, key, wf, ngroups; variant, pairs) */
static void rf_walk(const struct rf_walk *t, uint32_t key, uint32_t die, uint32_t wf, uint32_t p2)
{
	uint32_t row = 0, vi = 0, var = t->pairs && p2 == 1;

	for (uint32_t g = 0; g < t->ngroups; g++) {
		uint32_t col = t->colmap[key * t->ngroups + g], n = t->grp[g][0], ncols = t->grp[g][1];

		for (uint32_t r = 0; r < n; r++, row++, vi += ncols) {
			uint32_t v = t->val[vi + col];

			if (v != RFCHAN_SKIP)
				rf_awr(die, wf, t->reg[t->pairs ? 2 * row + var : row], v);
		}
	}
}

/* The key record of the RF COMMON walk (V:e0052042..V:e00520be): the key and its nibble-swapped
 * successor into the WF's half word. */
static void rf_key_record(uint32_t die, uint32_t wf, uint32_t p2, uint32_t key)
{
	uint32_t n = key + 1, sh = p2 ? RFCHAN_KEYREC_SHIFT_SX1 : 0;
	uint32_t v = key + ((HW_PREP(RFCHAN_KEYREC_NIB_HI, HW_GET(RFCHAN_KEYREC_NIB_LO, n)) |
			     HW_GET(RFCHAN_KEYREC_NIB_HI, n)) << RFCHAN_KEYREC_HI_SHIFT);

	hal_rmw(MT7996_PHY_RF_TBL_KEY(die, wf), MT7996_PHY_RF_TBL_KEY_SX0 << sh,
		(v & MT7996_PHY_RF_TBL_KEY_SX0) << sh);
}

static void item1(const struct chanset_ctx *c, uint32_t wf)
{
	uint32_t die = c->band, band = c->band, p2 = 0, i;
	struct item1_keys k;

	item1_keys(c, wf, &k);
	/* RF COMMON (V:e0051ff8 EPA, V:e0051ce0 no EPA) */
	if (!d414_skip(band, MT7996_PHY_RFIF_D414_SKIP_TBL)) {
		rf_key_record(die, wf, p2, k.common);
		rf_walk(c->epa ? &rf_epa_common : &rf_noepa_common, k.common, die, wf, p2);
	}
	if (c->epa) {
		for (i = 0; i < RFCHAN_NBANKS; i++) {
			hal_awr(die, i, ADIE_CHAIN_637, RFCHAN_637_EPA);
			hal_awr(die, i, ADIE_CHAIN_638, RFCHAN_638_EPA);
		}
		if (radio_eco() >= RFCHAN_ECO_NEW && (ee8(MT7996_PHY_EE_1A4) & MT7996_PHY_EE_1A4_B7))
			for (i = 0; i < ARRAY_SIZE(rf_extra); i++)
				if (c->group >= rf_extra[i].g0 && c->group <= rf_extra[i].g1)
					hal_awr(die, rf_extra[i].k, rf_extra[i].reg, rf_extra[i].val);
	}
	/* COMMON_BW at BW160 / BW320 (V:e0051de2 EPA, V:e0051e6a no EPA) */
	if (c->bw == MT7996_CHSET_BW160 || c->bw == MT7996_CHSET_BW320) {
		if (!d414_skip(band, MT7996_PHY_RFIF_D414_SKIP_TBL)) {
			if (c->epa) {
				for (i = 0; i < ARRAY_SIZE(rf_epa_bw_pre); i++)
					if (rf_epa_bw_pre[i].val != RFCHAN_SKIP)
						hal_awr(die, wf, rf_epa_bw_pre[i].reg[p2], rf_epa_bw_pre[i].val);
				rf_walk(&rf_epa_bw, k.bw, die, wf, p2);
			} else {
				rf_walk(&rf_noepa_bw, k.bw, die, wf, p2);
			}
		}
	}
	rf_walk(&rf_xlna, k.xlna, die, wf, p2);		/* V:e0051fb8 */
	rf_walk(&rf_xpa, k.xpa, die, wf, p2);		/* V:e0051f78 */
	if (c->group)					/* V:e005232a EPA, V:e00522e8 no EPA */
		rf_walk(c->epa ? &rf_epa_group : &rf_noepa_group, k.group, die, wf, p2);
}

/* ------------------------------------------------------------------------------------------------
 * R7: V:e00d2540, the gain-step inputs and A-die gain-step flags (pass 1) and, in pass 0, the
 * per-chain RX gain offsets (V:e00d1dc4)
 * ------------------------------------------------------------------------------------------------ */

/* E(x, y) (V:e00e0774): the WF's signed RX-gain EEPROM byte for the group's subband. */
static int8_t ee_rxgain(const struct chanset_ctx *c, uint32_t wf, uint32_t x, uint32_t y)
{
	uint32_t base, n, sub = MT7996_CHSET_RXG_SUB(c->group);

	if (!c->group) {
		base = MT7996_CHSET_EE_RXG_2G4;
		n = MT7996_CHSET_EE_RXG_N_2G4;
		sub = 0;
	} else if (c->group <= MT7996_CHSET_GROUP_5G_LAST) {
		base = MT7996_CHSET_EE_RXG_5G;
		n = MT7996_CHSET_EE_RXG_N_5G;
	} else {
		base = MT7996_CHSET_EE_RXG_6G;
		n = MT7996_CHSET_EE_RXG_N_6G;
	}
	return (int8_t)ee8(MT7996_CHSET_EE_RXG(base, n, sub, wf, x, y));
}

/* V:e00d1d14 */
static uint32_t glin_of(int x)
{
	if (x < MT7996_CHSET_GLIN_LO)
		return MT7996_CHSET_GLIN_V0;
	if (x < MT7996_CHSET_GLIN_M4)
		return MT7996_CHSET_GLIN_V1;
	if (x < MT7996_CHSET_GLIN_M1)
		return MT7996_CHSET_GLIN_V2;
	if (x <= 0)
		return MT7996_CHSET_GLIN_V4;
	if (x > MT7996_CHSET_GLIN_HI)
		return MT7996_CHSET_GLIN_VHI;
	return glin[x - 1];
}

/* V:e00d1c22: a linear gain (16 bits) back to a signed step, piecewise linear. */
#define GLOG(max, pre, mul, add, sh)	{ (max), (pre), (mul), (add), (sh) },
static const struct {
	uint16_t max;
	int16_t pre;
	uint16_t mul, add, sh;
} glog_seg[] = { MT7996_CHSET_GLOG(GLOG) };

static int glog_of(uint32_t a)
{
	if (a <= MT7996_CHSET_GLOG_MIN)
		return MT7996_CHSET_GLOG_LOW;
	for (uint32_t i = 0; i < ARRAY_SIZE(glog_seg); i++)
		if (a <= glog_seg[i].max)
			return (int8_t)((((int32_t)a + glog_seg[i].pre) * glog_seg[i].mul + glog_seg[i].add) >>
					glog_seg[i].sh);
	return MT7996_CHSET_GLOG_HIGH;
}

/* V:e00d1dc4(P, E(1,0), E(1,2), E(0,0), E(0,2), flag): the WF's six gain-step slots (a step of
 * the group's class and the set it comes from) to the A-die flags and nibbles; with flag 0 also
 * the per-chain RX gain offsets. */
static void gain_steps(const struct chanset_ctx *c, uint32_t wf, int e10, int e12, int e00, int e02,
		       uint32_t flag)
{
	uint32_t die = c->band, band = c->band, g = c->group, cls = MT7996_CHSET_GCLASS(g);
	uint32_t nsteps = MT7996_CHSET_GSTEPS(cls), reg, bit, v, i;
	uint8_t step[MT7996_CHSET_GSLOTS], set[MT7996_CHSET_GSLOTS];
	uint8_t base[2][2];	/* the two sets' (first, second): (-E(1,0), E(1,2)), (E(0,0), -E(0,2)) */
	int8_t gain[MT7996_CHSET_GSLOTS], hi[MT7996_CHSET_GSLOTS];
	uint8_t ofs7[MT7996_CHSET_GSLOTS];
	uint16_t ofs9[MT7996_CHSET_GSLOTS];

	if (c->elna != 1 && c->epa != 1)
		return;
	base[0][0] = (uint8_t)-e10;
	base[0][1] = (uint8_t)e12;
	base[1][0] = (uint8_t)e00;
	base[1][1] = (uint8_t)-e02;
	if (e00) {
		/* V:e00d21ac: per slot, the set by the slot threshold, then the highest step of the
		 * class whose first value (+ the set's first) does not exceed it */
		int lim = e00 + gstep[cls][0][0];
		uint8_t last = 0;

		for (i = 0; i < MT7996_CHSET_GSLOTS; i++) {
			int thr = (int8_t)(gthr[cls][i] + MT7996_CHSET_GTHR_OFS), s = thr >= lim, n;

			for (n = 0; n < (int)nsteps; n++) {
				int t = gstep[cls][nsteps - 1 - n][0] + (int8_t)base[s][0];

				if (thr >= t) {
					last = (uint8_t)(nsteps - 1 - n);
					break;
				}
			}
			step[i] = last;
			set[i] = (uint8_t)s;
		}
	} else {
		for (i = 0; i < MT7996_CHSET_GSLOTS; i++) {
			step[i] = (uint8_t)MT7996_CHSET_GSTEP_DEFAULT(i);
			set[i] = MT7996_CHSET_GSET_NONE;
		}
	}
	/* V:e00d1ec4: each slot's two values */
	for (i = 0; i < MT7996_CHSET_GSLOTS; i++) {
		const int8_t *p = gstep[cls][step[i]];

		if (set[i] == MT7996_CHSET_GSET_NONE) {
			gain[i] = p[0];
			hi[i] = (int8_t)(p[1] << 1);
		} else {
			uint8_t a = base[set[i]][0], b = base[set[i]][1];
			uint32_t lin = (glin_of(p[1]) + glin_of((int8_t)(a + b))) & MT7996_CHSET_GLIN_MASK;

			gain[i] = (int8_t)(p[0] + a);
			hi[i] = (int8_t)(((b + p[1]) << 1) - glog_of(lin));
		}
	}
	/* the A-die flags: TOP 0x4a4 [4:0] (group 0) or the WF's six bits */
	if (!g) {
		v = hal_ard(die, TOP, ADIE_TOP_FEM_4A4) & ~ADIE_TOP_FEM_4A4_GSTEP;
		for (i = 0; i < MT7996_CHSET_GSLOTS - 1; i++)
			if (set[i])
				v |= HW_BIT(i);
		hal_awr(die, TOP, ADIE_TOP_FEM_4A4, v);
	} else {
		reg = MT7996_CHSET_GSTEP_TOP(wf);
		bit = MT7996_CHSET_GSTEP_BIT(wf);
		v = hal_ard(die, TOP, reg) & ~(HW_GENMASK(MT7996_CHSET_GSTEP_NFLAGS - 1, 0) << bit);
		for (i = 0; i < MT7996_CHSET_GSTEP_NFLAGS; i++)
			if (set[i])
				v |= HW_BIT(bit + i);
		hal_awr(die, TOP, reg, v);
	}
	/* chain 0x631 (group 0) / 0x636: the slots' steps as nibbles */
	reg = g ? MT7977_TTG_636 : ADIE_LOOP_631;
	v = hal_ard(die, wf, reg) & ADIE_GSTEP_CHAIN_KEEP;
	for (i = 0; i < (g ? MT7996_CHSET_GSLOTS : MT7996_CHSET_GSLOTS - 1); i++)
		v |= (step[i] << __builtin_ctz(ADIE_GSTEP_CHAIN_FIELD(i))) & ADIE_GSTEP_CHAIN_FIELD(i);
	hal_awr(die, wf, reg, v);
	if (flag)
		return;

	/* flag 0: the per-chain RX gain offsets */
	for (i = 0; i < MT7996_CHSET_GSLOTS; i++) {
		int x = gain[i];

		if (x < MT7996_CHSET_GOFS_MIN)
			x = MT7996_CHSET_GOFS_MIN;
		if (x > MT7996_CHSET_GOFS_MAX)
			x = MT7996_CHSET_GOFS_MAX;
		ofs7[i] = (uint8_t)x & MT7996_CHSET_GOFS7;
		ofs9[i] = hi[i] < 0 ? (uint16_t)hi[i] & MT7996_CHSET_GOFS9 : 0;
	}
	hal_rmw(MT7996_PHY_CSD_RXGOFS_188(band, wf),
		MT7996_PHY_CSD_RXGOFS_188_F3 | MT7996_PHY_CSD_RXGOFS_188_F2 | MT7996_PHY_CSD_RXGOFS_188_F1 |
			MT7996_PHY_CSD_RXGOFS_188_F0,
		HW_PREP(MT7996_PHY_CSD_RXGOFS_188_F3, ofs7[0]) | HW_PREP(MT7996_PHY_CSD_RXGOFS_188_F2, ofs7[1]) |
			HW_PREP(MT7996_PHY_CSD_RXGOFS_188_F1, ofs7[2]) |
			HW_PREP(MT7996_PHY_CSD_RXGOFS_188_F0, ofs7[3]));
	hal_rmw(MT7996_PHY_CSD_RXGOFS_0F8(band, wf), MT7996_PHY_CSD_RXGOFS_HI7,
		HW_PREP(MT7996_PHY_CSD_RXGOFS_HI7, ofs7[4]));
	if (g)
		hal_rmw(MT7996_PHY_CSD_RXGOFS_390(band, wf), MT7996_PHY_CSD_RXGOFS_HI7,
			HW_PREP(MT7996_PHY_CSD_RXGOFS_HI7, ofs7[5]));
	hal_rmw(MT7996_PHY_CSD_RXGOFS_164(band, wf), MT7996_PHY_CSD_RXGOFS_HI9 | MT7996_PHY_CSD_RXGOFS_LO9,
		HW_PREP(MT7996_PHY_CSD_RXGOFS_LO9, ofs9[1]) | HW_PREP(MT7996_PHY_CSD_RXGOFS_HI9, ofs9[2]));
	hal_rmw(MT7996_PHY_CSD_RXGOFS_160(band, wf), MT7996_PHY_CSD_RXGOFS_HI9 | MT7996_PHY_CSD_RXGOFS_LO9,
		HW_PREP(MT7996_PHY_CSD_RXGOFS_LO9, ofs9[3]) | HW_PREP(MT7996_PHY_CSD_RXGOFS_HI9, ofs9[4]));
	if (g)
		hal_rmw(MT7996_PHY_CSD_RXGOFS_408(band, wf), MT7996_PHY_CSD_RXGOFS_HI9,
			HW_PREP(MT7996_PHY_CSD_RXGOFS_HI9, ofs9[5]));
	/* MediaTek then reads CSD +0x104 five times and discards the values: left out */
}

/* V:e00d2540(T, P, flag): the inputs mirrored to MT7996_PHY_FW_GAINSTEP_IN / _P, then
 * V:e00d1dc4. */
static void gainstep_pass(const struct chanset_ctx *c, uint32_t wf, uint32_t flag)
{
	int e10 = ee_rxgain(c, wf, 1, 0), e12 = ee_rxgain(c, wf, 1, 2);
	int e00 = ee_rxgain(c, wf, 0, 0), e02 = ee_rxgain(c, wf, 0, 2);

	hal_wr(MT7996_PHY_FW_GAINSTEP_IN,
	       HW_PREP(MT7996_PHY_FW_GAINSTEP_IN_E10, (uint8_t)e10) | HW_PREP(MT7996_PHY_FW_GAINSTEP_IN_E12, (uint8_t)e12) |
		       HW_PREP(MT7996_PHY_FW_GAINSTEP_IN_E00, (uint8_t)e00) | HW_PREP(MT7996_PHY_FW_GAINSTEP_IN_E02, (uint8_t)e02));
	hal_wr(MT7996_PHY_FW_GAINSTEP_P,
	       HW_PREP(MT7996_PHY_FW_GAINSTEP_P_ELNA, c->elna) | HW_PREP(MT7996_PHY_FW_GAINSTEP_P_EPA, c->epa) |
		       HW_PREP(MT7996_PHY_FW_GAINSTEP_P_GROUP, c->group) | HW_PREP(MT7996_PHY_FW_GAINSTEP_P_FLAG, flag));
	gain_steps(c, wf, e10, e12, e00, e02, flag);
}

/* ------------------------------------------------------------------------------------------------
 * PHY tables A and B (V:e00d917e and the fields of V:e00d9238 / V:e00d9702)
 * ------------------------------------------------------------------------------------------------ */

/* Band 0 above 40 MHz: a word v of table B's 2.4 GHz BW40 column with the bits MediaTek changes
 * between its 5 GHz BW40 column (r40) and the 5 GHz column of the bandwidth (rbw), where its 2.4
 * and 5 GHz BW40 values agree. */
static uint32_t tbl_b_wide(uint32_t v, uint32_t r40, uint32_t rbw)
{
	uint32_t m = (r40 ^ rbw) & ~(v ^ r40);

	return (v & ~m) | (rbw & m);
}

/* The walk of column key; with ref40 >= 0 (band 0 above 40 MHz, table B only) each word merged
 * with the same row of columns ref40 and refbw (tbl_b_wide). */
static void phy_walk(const struct phy_table *t, uint32_t key, int ref40, int refbw, uint32_t band,
		     uint32_t chain)
{
	uint32_t row = 0, vi = 0;

	for (uint32_t g = 0; g < t->ngroups; g++) {
		uint32_t col = t->colmap[key * t->ngroups + g], n = t->grp[g][0], ncols = t->grp[g][1];

		for (uint32_t r = 0; r < n; r++, row++, vi += ncols) {
			uint32_t v = t->val[vi + col], a = t->addr[row];

			if (v == MT7996_PHY_REGTBL_SKIP)
				continue;
			if (ref40 >= 0) {
				uint32_t r40 = t->val[vi + t->colmap[(uint32_t)ref40 * t->ngroups + g]];
				uint32_t rbw = t->val[vi + t->colmap[(uint32_t)refbw * t->ngroups + g]];

				if (r40 != MT7996_PHY_REGTBL_SKIP && rbw != MT7996_PHY_REGTBL_SKIP)
					v = tbl_b_wide(v, r40, rbw);
			}
			if (chain != MT7996_CHSET_TBL_NOCHAIN && a >= MT7996_CHSET_TBL_RELOC_LO &&
			    a <= MT7996_CHSET_TBL_RELOC_HI)
				a = MT7996_PHY_CHAIN_REG(0, chain, a & MT7996_CHSET_TBL_RELOC_OFS);
			hal_wr(a + band * MT7996_PHY_BAND_STRIDE, v);
		}
	}
}

#define GCOMP(i, v)		{ (i), (v) },
static const struct {
	uint8_t i;
	uint16_t v;
} gcomp[] = { MT7996_CHSET_GCOMP(GCOMP) };

/* Table A, V:e00d9238(key record, first chain) (CHANSET_F_TABLE_A). */
static void table_a(const struct chanset_ctx *c)
{
	uint32_t band = c->band, chain, i;
	static const uint32_t tpc_mcs_a[] = { MT7996_PHY_DFE_E738(0), MT7996_PHY_DFE_E73C(0), MT7996_PHY_DFE_E74C(0) };
	static const uint32_t tpc_mcs_b[] = { MT7996_PHY_DFE_E750(0), MT7996_PHY_DFE_E758(0) };
	int e2 = radio_id.hw_rev != CBTOP_HW_REV_E1;
	uint32_t k0 = c->group ? MT7996_CHSET_TBL_A_K0_5G6G : MT7996_CHSET_TBL_A_K0_2G4;

	if (!d414_skip(band, MT7996_PHY_RFIF_D414_SKIP_CHTBL)) {
		for (chain = c->wf_first; chain <= MT7996_CHSET_TBL_A_LAST_CHAIN; chain++)
			phy_walk(&tbl_a, (uint32_t)key_index(tbl_a.keys, 1, tbl_a.nkeys, tbl_a_key(c, chain)), -1, -1,
				 band, chain);
		if (c->khz > MT7996_CHSET_TBL_A_K0_HI_KHZ && !c->epa)
			k0 = MT7996_CHSET_TBL_A_K0_HI;
		if (e2 && hal_rd(MT7996_PHY_DPD_FORCE_TBL(band)) != MT7996_CHSET_DPD_FORCE_ON && c->epa == 1) {
			for (i = 0; i < ARRAY_SIZE(gcomp); i++)
				hal_wf(MT7996_PHY_DFE_GAIN_COMP(band, gcomp[i].i),
				       gcomp[i].i & 1 ? MT7996_PHY_DFE_GAIN_COMP_ODD : MT7996_PHY_DFE_GAIN_COMP_EVEN,
				       gcomp[i].v);
			hal_wr(MT7996_PHY_DFE_FD70(band), MT7996_CHSET_FD70_VAL);
			hal_wf(MT7996_PHY_DFE_FF14(band), MT7996_PHY_DFE_FF14_LO24, MT7996_CHSET_FF14_VAL);
			hal_wf(MT7996_PHY_DFE_FF18(band), MT7996_PHY_DFE_FF18_HI24, MT7996_CHSET_FF18_VAL);
			hal_wf(MT7996_PHY_DFE_FF80(band), MT7996_PHY_DFE_FF80_B1, MT7996_CHSET_FF80_B1_VAL);
			hal_wf(MT7996_PHY_DFE_FF20(band), MT7996_PHY_DFE_FF14_LO24, MT7996_CHSET_FF20_VAL);
			hal_wf(MT7996_PHY_DFE_FF24(band), MT7996_PHY_DFE_FF18_HI24, MT7996_CHSET_FF24_VAL);
			hal_wf(MT7996_PHY_DFE_FF80(band), MT7996_PHY_DFE_FF80_B0, MT7996_CHSET_FF80_B0_VAL);
			hal_wf(MT7996_PHY_DFE_FF2C(band), MT7996_PHY_DFE_FF14_LO24, MT7996_CHSET_FF2C_VAL);
			hal_wf(MT7996_PHY_DFE_FF30(band), MT7996_PHY_DFE_FF30_HI8, MT7996_CHSET_FF30_VAL);
		}
		if (e2) {
			if (c->epa == 1) {
				hal_wf(MT7996_PHY_DFE_E738(band), MT7996_PHY_DFE_E738_F18_16, MT7996_CHSET_TPCMCS_VAL);
				for (i = 0; i < ARRAY_SIZE(tpc_mcs_a) + ARRAY_SIZE(tpc_mcs_b); i++) {
					uint32_t a = (i < ARRAY_SIZE(tpc_mcs_a) ? tpc_mcs_a[i] :
						      tpc_mcs_b[i - ARRAY_SIZE(tpc_mcs_a)]) + band * MT7996_PHY_BAND_STRIDE;

					hal_wf(a, MT7996_PHY_DFE_E738_F2_0, MT7996_CHSET_TPCMCS_VAL);
					hal_wf(a, MT7996_PHY_DFE_E738_F6_4, MT7996_CHSET_TPCMCS_VAL);
					hal_wf(a, MT7996_PHY_DFE_E738_F10_8, MT7996_CHSET_TPCMCS_VAL);
					hal_wf(a, MT7996_PHY_DFE_E738_F14_12, MT7996_CHSET_TPCMCS_VAL);
				}
				for (chain = c->wf_first; chain <= MT7996_CHSET_TBL_A_LAST_CHAIN; chain++) {
					hal_set(MT7996_PHY_TSSI_PWR_CTRL(band, chain), MT7996_PHY_TSSI_DPD_SHIFT_RM);
					if (k0 == MT7996_CHSET_TBL_A_K0_5G6G) {
						hal_wf(MT7996_PHY_CHAIN_2000(band, chain, MT7996_CHSET_CHAIN_2000_OFS0),
						       MT7996_PHY_CHAIN_2000_LO24, MT7996_CHSET_CHAIN_2000_VAL);
						hal_wf(MT7996_PHY_CHAIN_2000(band, chain, MT7996_CHSET_CHAIN_2000_OFS1),
						       MT7996_PHY_CHAIN_2000_LO24, MT7996_CHSET_CHAIN_2000_VAL);
						hal_wf(MT7996_PHY_CHAIN_2000(band, chain, MT7996_CHSET_CHAIN_2000_OFS2),
						       MT7996_PHY_CHAIN_2000_LO24, MT7996_CHSET_CHAIN_2000_VAL);
					}
				}
			}
			hal_set(MT7996_PHY_WFCOMM_C058(band), MT7996_PHY_WFCOMM_C058_B16);
			hal_set(MT7996_PHY_WFCOMM_C058(band), MT7996_PHY_WFCOMM_C058_B20);
			hal_clr(MT7996_PHY_WFCOMM_B580(band), MT7996_PHY_WFCOMM_B580_F13_4);
		}
	}
	hal_clr(MT7996_PHY_TPC_CTL(band, MT7996_CHSET_TPC_CTL_N), MT7996_PHY_TPC_CTL9_B14);
	hal_clr(MT7996_PHY_TPC_CTL(band, MT7996_CHSET_TPC_CTL_N), MT7996_PHY_TPC_CTL9_B13);
	hal_clr(MT7996_PHY_TPC_CTL(band, MT7996_CHSET_TPC_CTL_N), MT7996_PHY_TPC_CTL9_F6_4);
	hal_clr(MT7996_PHY_DFE_FF68(band), MT7996_PHY_DFE_FF68_F22_20);
	hal_clr(MT7996_PHY_DFE_FF68(band), MT7996_PHY_DFE_FF68_F17_16);
	hal_clr(MT7996_PHY_DFE_FF68(band), MT7996_PHY_DFE_FF68_B28);
	hal_clr(MT7996_PHY_DFE_FF68(band), MT7996_PHY_DFE_FF68_F27_24);
	hal_clr(MT7996_PHY_DFE_FF68(band), MT7996_PHY_DFE_FF68_B29);
	hal_clr(MT7996_PHY_DFE_FF68(band), MT7996_PHY_DFE_FF68_B30);
	hal_clr(MT7996_PHY_DFE_FF68(band), MT7996_PHY_DFE_FF68_B31);
}

/* R3: table B, V:e00d9702(key record): the walk twice, then its fields. */
static void table_b(const struct chanset_ctx *c)
{
	uint32_t band = c->band, k0, k1;
	int key[3];

	tbl_b_cols(c, key, &k0, &k1);	/* checked by chanset_prepare */
	if (!d414_skip(band, MT7996_PHY_RFIF_D414_SKIP_CHTBL)) {
		phy_walk(&tbl_b, (uint32_t)key[0], key[1], key[2], band, MT7996_CHSET_TBL_NOCHAIN);
		phy_walk(&tbl_b, (uint32_t)key[0], key[1], key[2], band, MT7996_CHSET_TBL_NOCHAIN);
		if (radio_id.hw_rev != CBTOP_HW_REV_E1) {
			if (k1 > MT7996_CHSET_BW40 && k0 == MT7996_CHSET_TBL_B_K0_6G) {
				hal_set(MT7996_PHY_BN_85A0(band), MT7996_PHY_BN_85A0_F15_14);
				hal_set(MT7996_PHY_BN_8E30(band), MT7996_PHY_BN_8E30_F31_30);
			} else {
				hal_clr(MT7996_PHY_BN_85A0(band), MT7996_PHY_BN_85A0_F15_14);
				hal_clr(MT7996_PHY_BN_8E30(band), MT7996_PHY_BN_8E30_F31_30);
			}
		}
	}
	if ((ee8(MT7996_PHY_EE_1A4) & MT7996_PHY_EE_1A4_B6) && band == MT7996_PHY_BAND_6G &&
	    k1 == MT7996_CHSET_BW320)
		hal_wf(MT7996_PHY_WFCOMM_C104(MT7996_PHY_BAND_6G), MT7996_PHY_WFCOMM_C104_F3_0, MT7996_CHSET_C104_VAL);
	else
		hal_clr(MT7996_PHY_WFCOMM_C104(band), MT7996_PHY_WFCOMM_C104_F3_0);
}

/* ------------------------------------------------------------------------------------------------
 * R4: the RX front end
 * ------------------------------------------------------------------------------------------------ */

/* MediaTek's rounding: num / mhz, plus 1 when the next decimal digit is above 4. */
static uint32_t div_round(uint32_t num, uint32_t mhz)
{
	return num / mhz + ((num * MT7996_CHSET_ROUND_DIGITS / mhz) % MT7996_CHSET_ROUND_DIGITS >
			    MT7996_CHSET_ROUND_HALF);
}

/* AGC table entry i of the channel (V:e00d1a5e). Band 0 above 40 MHz (ours): the 2.4 GHz BW40
 * entry plus its own BW20 -> BW40 step once per further class. Every entry of both of MediaTek's
 * tables steps by the same 0x30 per class [static] (3 dB a doubling: the noise power of the
 * bandwidth [inferred]). */
static uint32_t agc_entry(const struct chanset_ctx *c, uint32_t i)
{
	uint32_t v20, v40;

	if (c->band != MT7996_PHY_BAND_2G4)
		return agc_5g6g[i][agc_col[c->bw]];
	if (!wide_2g4(c))
		return agc_2g4[i][agc_col[c->bw]];
	v20 = agc_2g4[i][agc_col[MT7996_CHSET_BW20]];
	v40 = agc_2g4[i][agc_col[MT7996_CHSET_BW40]];
	return v40 + (c->bw - MT7996_CHSET_BW40) * (v40 - v20);
}

static void rx_frontend(const struct chanset_ctx *c)
{
	uint32_t band = c->band, mhz = c->khz / MT7996_CHSET_KHZ_PER_MHZ, v, ch, i, n, k;

	/* V:e00d188e */
	v = div_round(MT7996_CHSET_6188_NUM, mhz);
	hal_wf(MT7996_PHY_BN_6188(band), MT7996_PHY_BN_6188_LO, v);
	hal_wf(MT7996_PHY_BN_6188(band), MT7996_PHY_BN_6188_HI, v);
	hal_wf(MT7996_PHY_DFE_F168(band), MT7996_PHY_DFE_F168_VAL, div_round(MT7996_CHSET_F168_NUM, mhz));
	/* V:e00d196a */
	v = div_round(MT7996_CHSET_F168_NUM, mhz) << MT7996_CHSET_D0AC_SHIFT(c->bw);
	for (i = 0; i < MT7996_CHSET_D0AC_CHAINS; i++)
		hal_wf(MT7996_PHY_CHAIN_D0AC(band, i), MT7996_PHY_CHAIN_D0AC_VAL, v);
	/* V:e00d1a5e: the AGC table, entry by entry, chain by chain */
	n = band == MT7996_PHY_BAND_2G4 ? MT7996_PHY_CSD_AGC_TBL_N_2G4 : MT7996_PHY_CSD_AGC_TBL_N_5G6G;
	for (i = 0; i < n; i++) {
		v = agc_entry(c, i);
		for (k = 0; k < (band == MT7996_PHY_BAND_2G4 ? MT7996_CHSET_AGC_CHAINS_2G4 : MT7996_CHSET_AGC_CHAINS_5G6G);
		     k++) {
			hal_wr(MT7996_PHY_CSD_AGC_TBL(band, k), v);
			hal_udelay(MT7996_CHSET_AGC_DELAY_US);
		}
	}
	/* V:e00d1b72: the 2.4 GHz primary channel */
	if (c->khz - MT7996_CHSET_0080_LO_KHZ <= MT7996_CHSET_0080_SPAN_KHZ)
		ch = (c->khz - MT7996_CHSET_BASE_KHZ_2G4) / MT7996_CHSET_CH_KHZ;
	else
		ch = c->khz == MT7996_CHSET_2G4_CH14_KHZ ? MT7996_CHSET_2G4_CH14 : 0;
	if (ch < MT7996_CHSET_2G4_CH_MIN || ch > MT7996_CHSET_2G4_CH_MAX)
		return;
	if (c->bw != MT7996_CHSET_BW20) {
		/* MediaTek tests the primary index for 0 and 1 (else 6): at BW40 it is one of them */
		if (c->bw == MT7996_CHSET_BW40 && ch >= MT7996_CHSET_0080_BW40_FIRST && ch <= MT7996_CHSET_0080_BW40_LAST)
			ch = c->sec ? ch + MT7996_CHSET_0080_BW40_OFS : ch - MT7996_CHSET_0080_BW40_OFS;
		else
			ch = MT7996_CHSET_0080_CH_OTHER;
	}
	hal_wf(MT7996_PHY_WFCOMM_0080(band), MT7996_PHY_WFCOMM_0080_CH, ch);
}

/* ------------------------------------------------------------------------------------------------
 * R5: the D-die RX setup with its A-die part, V:e00ddd0e (step 12 of the channel flow,
 * V:e00d7414). Its argument is the struct the flow builds at its sp + 0x40 (V:e00d7246..
 * V:e00d728a, V:e00d73fe..V:e00d7412): [0] BW class, [1] TX streams, [2] RX streams (unused
 * here), [3] the primary 20 MHz index (ctx sec), [4] record [0x14] (unused), [7] EPA, u32 [8]
 * the band's channel-0 frequency, [0xc] band, [0xd] P[0x1c] (ctx fem), [0xe] / [0xf] V:e00d41e4
 * (WM configuration byte 0xe0093075 [2:0] == 7; 0 on this board), [0x10] the centre channel.
 * Not ported: the paths for [0xe] / [0xf] != 0 (another SKU: MT7976 TOP 0x4b0 / 0x4b4 = 3 /
 * 0x1c, ADIE_TX_BW / 0x934 instead of ADIE_RX_BW / 0x933, other chain ranges, the MT7977's SX
 * out-force on k 4) and AdieSel 5 (another die type: its FCB pattern and table, V:e00dd432, and
 * its A-die part, V:e00dcdd4).
 * ------------------------------------------------------------------------------------------------ */

static const uint32_t rx_gate_off_b0[] = CONN_PWR_800_GATE_CLR_B0;
static const uint32_t rx_gate_off_b1[] = CONN_PWR_800_GATE_CLR_B1;
static const uint32_t rx_gate_off_b2[] = CONN_PWR_800_GATE_CLR_B2;
static const uint32_t rx_gate_on_b0[] = CONN_PWR_800_GATE_SET_B0;
static const uint32_t rx_gate_on_b1[] = CONN_PWR_800_GATE_SET_B1;
static const uint32_t rx_gate_on_b2[] = CONN_PWR_800_GATE_SET_B2;

static const uint32_t rx_txcal_mt7976[] = MT7996_PHY_TXCAL_TBL_MT7976;
static const uint32_t rx_txcal_mt7977_epa[] = MT7996_PHY_TXCAL_TBL_MT7977_EPA;
static const uint32_t rx_txcal_mt7977_noepa[] = MT7996_PHY_TXCAL_TBL_MT7977_NOEPA;

static const uint32_t rx_gsel_map[] = MT7996_PHY_GSEL_MAP_INIT;
static const uint32_t rx_gsel_grp[] = MT7996_PHY_GSEL1_GRP_MT7977;
static const uint32_t rx_gsel_step[] = MT7996_PHY_GSEL1_STEP_MT7977;
static const uint32_t rx_gsel_grp_bw320[] = MT7996_PHY_GSEL1_GRP_MT7977_BW320;
static const uint32_t rx_gsel_step_bw320[] = MT7996_PHY_GSEL1_STEP_MT7977_BW320;

static const uint32_t rx_36a_lo[] = RFCHAN_RXSET_36A_LO;
static const uint32_t rx_36a_hi[] = RFCHAN_RXSET_36A_HI;

/* Close the band's WFSYS gate: V:e00ddaac (band 0), V:e00ddb08 (band 1), V:e00ddb70 (band 2).
 * MediaTek picks band 1 and band 2 by value and band 0's helper for anything else. */
static void rx_gate_close(uint32_t band)
{
	const uint32_t *f;
	uint32_t n, i;

	if (band == MT7996_PHY_BAND_5G) {
		hal_clr(MT7996_CONN_PWR_110, CONN_PWR_1XX_GATE);
		f = rx_gate_off_b1;
		n = ARRAY_SIZE(rx_gate_off_b1);
	} else if (band == MT7996_PHY_BAND_6G) {
		hal_wr(MT7996_WFSYS_ON_LPCR_DIS(MT7996_PHY_BAND_6G), WFSYS_ON_LPCR_DIS_PLL_CKEN);
		f = rx_gate_off_b2;
		n = ARRAY_SIZE(rx_gate_off_b2);
	} else {
		hal_clr(MT7996_CONN_PWR_10C, CONN_PWR_1XX_GATE);
		f = rx_gate_off_b0;
		n = ARRAY_SIZE(rx_gate_off_b0);
	}
	for (i = 0; i < n; i++)
		hal_clr(MT7996_CONN_PWR_800, f[i]);
	hal_clr(MT7996_CONN_PWR_800, CONN_PWR_800_SHARED);
}

/* Reset the band and open its gate again: V:e00ddbe6 (band 0), V:e00ddc46 (band 1),
 * V:e00ddcaa (band 2): 1 us (cos_delay_usec), the band reset bit pulsed low, the gate fields,
 * then the band's gate word. */
static void rx_gate_open(uint32_t band)
{
	const uint32_t *f;
	uint32_t n, i, b;

	if (band == MT7996_PHY_BAND_5G) {
		f = rx_gate_on_b1;
		n = ARRAY_SIZE(rx_gate_on_b1);
		b = MT7996_PHY_BAND_5G;
	} else if (band == MT7996_PHY_BAND_6G) {
		f = rx_gate_on_b2;
		n = ARRAY_SIZE(rx_gate_on_b2);
		b = MT7996_PHY_BAND_6G;
	} else {
		f = rx_gate_on_b0;
		n = ARRAY_SIZE(rx_gate_on_b0);
		b = MT7996_PHY_BAND_2G4;
	}
	hal_udelay(MT7996_CHSET_GATE_DELAY_US);
	hal_clr(MT7996_WF_TOP_ON_BAND_RST, WF_TOP_ON_BAND_RST_BAND(b));
	hal_set(MT7996_WF_TOP_ON_BAND_RST, WF_TOP_ON_BAND_RST_BAND(b));
	for (i = 0; i < n; i++)
		hal_set(MT7996_CONN_PWR_800, f[i]);
	if (b == MT7996_PHY_BAND_5G)
		hal_set(MT7996_CONN_PWR_110, CONN_PWR_1XX_GATE);
	else if (b == MT7996_PHY_BAND_6G)
		hal_wr(MT7996_WFSYS_ON_LPCR_EN(MT7996_PHY_BAND_6G), WFSYS_ON_LPCR_EN_PLL_CKEN);
	else
		hal_set(MT7996_CONN_PWR_10C, CONN_PWR_1XX_GATE);
}

/* The FCB pattern and the table at BN +0xfc30.. (V:e00dd40a(0, base kHz, EPA)): whole words,
 * by die type, EPA and the band's base frequency. */
static void rx_txcal_tbl(const struct chanset_ctx *c)
{
	uint32_t b = c->band, pat, n, i;
	const uint32_t *t;

	if (!c->mt7977) {
		if (!c->epa || c->base_khz != MT7996_CHSET_BASE_KHZ_2G4)
			return;
		pat = MT7996_PHY_DFE_FCB_PAT_CFG6;
		t = rx_txcal_mt7976;
		n = ARRAY_SIZE(rx_txcal_mt7976);
	} else {
		if (c->base_khz == MT7996_CHSET_BASE_KHZ_2G4)
			return;
		pat = MT7996_PHY_DFE_FCB_PAT_CFG7;
		if (c->epa) {
			t = rx_txcal_mt7977_epa;
			n = ARRAY_SIZE(rx_txcal_mt7977_epa);
		} else {
			t = rx_txcal_mt7977_noepa;
			n = ARRAY_SIZE(rx_txcal_mt7977_noepa);
		}
	}
	hal_wr(MT7996_PHY_DFE_FCB0(b), pat);
	hal_wr(MT7996_PHY_DFE_FCB4(b), pat);
	for (i = 0; i < n; i++)
		hal_wr(MT7996_PHY_TXCAL_TBL(b, i), t[i]);
}

/* DFE +0xe338 [1:0]: 1 below BW320, 0 at BW320 (V:e00de25a / V:e00de572, again V:e00de3c0 /
 * V:e00de584). */
static void rx_e338(const struct chanset_ctx *c)
{
	hal_rmw(MT7996_PHY_DFE_E338(c->band), MT7996_PHY_DFE_E338_LO,
		c->bw == MT7996_CHSET_BW320 ? 0 : MT7996_PHY_DFE_E338_B0);
}

/* MT7977 only (AdieSel 7, V:e00de268): gain-index selector set 1 rewritten whole with the
 * per-band init's values, at BW320 then with its BW320 values (STEP [4] included). */
static void rx_gsel(const struct chanset_ctx *c)
{
	uint32_t b = c->band, i;

	for (i = 0; i < ARRAY_SIZE(rx_gsel_map); i++)
		hal_wr(MT7996_PHY_GSEL_MAP(b, MT7996_PHY_GSEL_SET1, i), rx_gsel_map[i]);
	for (i = 0; i < ARRAY_SIZE(rx_gsel_grp); i++)
		hal_wr(MT7996_PHY_GSEL_GRP(b, MT7996_PHY_GSEL_SET1, i), rx_gsel_grp[i]);
	for (i = 0; i < ARRAY_SIZE(rx_gsel_step); i++)
		hal_wr(MT7996_PHY_GSEL_STEP(b, MT7996_PHY_GSEL_SET1, i), rx_gsel_step[i]);
	if (c->bw != MT7996_CHSET_BW320)
		return;
	for (i = 0; i < ARRAY_SIZE(rx_gsel_map); i++)
		hal_wr(MT7996_PHY_GSEL_MAP(b, MT7996_PHY_GSEL_SET1, i), rx_gsel_map[i]);
	for (i = 0; i < ARRAY_SIZE(rx_gsel_grp_bw320); i++)
		hal_wr(MT7996_PHY_GSEL_GRP(b, MT7996_PHY_GSEL_SET1, i), rx_gsel_grp_bw320[i]);
	for (i = 0; i < ARRAY_SIZE(rx_gsel_step_bw320); i++)
		hal_wr(MT7996_PHY_GSEL_STEP(b, MT7996_PHY_GSEL_SET1, i), rx_gsel_step_bw320[i]);
}

/* The A-die part, V:e00dcd88(0, band, [0xe] = 0, [0xf] = 0, BW class), on the band's die.
 * MT7976 (AdieSel 6, V:e00dd1f6): TOP 0x4b0 / 0x4b4, then chains k 0..3: ADIE_CHSET [31:28]
 * = 0xa, ADIE_RX_BW = the BW class capped at 160 MHz, ADIE_CHSET_933. MT7977
 * (V:e00dd340): ADIE_SX_OUT_FORCE = 3 on k 0 (the chain bank's SX copy), then chains k 0..4:
 * ADIE_RX_BW = the BW class capped at 320 MHz, ADIE_CHSET_933 by that code. Ours: the analog
 * filter (ADIE_RX_BW, ADIE_CHSET_933, and on the MT7976 MT7976_RXF_125 and the RC trim) is not
 * part of the channel set: the FILTER command sets it (rfctl.h rfctl_filter), independent of
 * the bandwidth, and nothing here changes it. */
static void rx_adie(const struct chanset_ctx *c)
{
	uint32_t die = ADIE_DIE_OF_BAND(c->band), k;

	if (!c->mt7977) {
		hal_awr(die, ADIE_K_TOP, ADIE_TOP_CHSET0, RFCHAN_RXSET_TOP_CHSET_MT7976);
		hal_awr(die, ADIE_K_TOP, ADIE_TOP_CHSET1, RFCHAN_RXSET_TOP_CHSET_MT7976);
		for (k = 0; k < MT7976_NUM_CHAINS; k++)
			hal_awf(die, ADIE_K_WF(k), ADIE_CHSET, ADIE_CHSET_HI, ADIE_CHSET_HI_VAL);
		return;
	}
	hal_awr(die, ADIE_K_WF(0), ADIE_SX_OUT_FORCE, ADIE_SX_OUT_FORCE_ON);
}

/* 6 GHz BW320 on centre channel 95 with EEPROM 0x1a4 [7] set (V:e00de3fe..V:e00de5f4):
 * chain word 0x36a on k 0..3, by the half of 320 MHz the primary 20 MHz channel is in. */
static void rx_adie_ch95(const struct chanset_ctx *c)
{
	uint32_t die = ADIE_DIE_OF_BAND(c->band), k;
	const uint32_t *t;

	if (c->bw != MT7996_CHSET_BW320 || c->ch != RFCHAN_RXSET_36A_CENTRE ||
	    !HW_GET(MT7996_PHY_EE_1A4_B7, ee8(MT7996_PHY_EE_1A4)))
		return;
	t = c->sec <= RFCHAN_RXSET_36A_PRI_LO_MAX ? rx_36a_lo : rx_36a_hi;
	for (k = 0; k < ARRAY_SIZE(rx_36a_lo); k++)
		hal_awr(die, ADIE_K_WF(k), ADIE_CHAIN_36A, t[k]);
}

/* V:e00ddd0e. */
static void rx_setup(const struct chanset_ctx *c)
{
	uint32_t b = c->band, ch, rf;

	/* MediaTek reads MT7996_PHY_RFIF_D414 twice here and discards both values (V:e00ddd8a,
	 * V:e00ddd8c): left out. */
	rx_gate_close(b);

	/* Chains (V:e00ddd96..V:e00ddffa): CHAIN_D010 [7] cleared at BW40, else set; CHAIN_E01C
	 * [7]; BN_CLK0 / 1 [17] around a pulse of CHAIN_E034 [29]; CHAIN_E024 [0]. */
	for (ch = 0; ch < MT7996_CHSET_D010_CHAINS(b); ch++) {
		if (c->bw == MT7996_CHSET_BW40)
			hal_clr(MT7996_PHY_CHAIN_D010(b, ch), MT7996_PHY_CHAIN_D010_B7);
		else
			hal_set(MT7996_PHY_CHAIN_D010(b, ch), MT7996_PHY_CHAIN_D010_B7);
	}
	for (ch = 0; ch < MT7996_CHSET_RX_CHAINS; ch++)
		hal_set(MT7996_PHY_CHAIN_E01C(b, ch), MT7996_PHY_CHAIN_E01C_B7);
	hal_set(MT7996_PHY_BN_CLK0(b), MT7996_PHY_BN_CLK_B17);
	hal_set(MT7996_PHY_BN_CLK1(b), MT7996_PHY_BN_CLK_B17);
	for (ch = 0; ch < MT7996_CHSET_RX_CHAINS; ch++)
		hal_set(MT7996_PHY_CHAIN_E034(b, ch), MT7996_PHY_CHAIN_E034_B29);
	for (ch = 0; ch < MT7996_CHSET_RX_CHAINS; ch++)
		hal_clr(MT7996_PHY_CHAIN_E034(b, ch), MT7996_PHY_CHAIN_E034_B29);
	hal_clr(MT7996_PHY_BN_CLK1(b), MT7996_PHY_BN_CLK_B17);
	hal_clr(MT7996_PHY_BN_CLK0(b), MT7996_PHY_BN_CLK_B17);
	for (ch = 0; ch < MT7996_CHSET_RX_CHAINS; ch++)
		hal_set(MT7996_PHY_CHAIN_E024(b, ch), MT7996_PHY_CHAIN_E024_B0);

	rx_txcal_tbl(c);

	/* V:e00de000..V:e00de086 */
	hal_clr(MT7996_PHY_BN_MODE(b), MT7996_PHY_BN_MODE_B8);
	hal_clr(MT7996_PHY_WFCOMM_3000(b), MT7996_PHY_WFCOMM_3000_B31);
	hal_rmw(MT7996_PHY_BN_B000(b), MT7996_PHY_BN_B000_F31_30,
		(c->nss_tx - 1) << __builtin_ctz(MT7996_PHY_BN_B000_F31_30));
	hal_rmw(MT7996_PHY_BN_B000(b), MT7996_PHY_BN_B000_F27_26,
		(c->nss_tx - 1) << __builtin_ctz(MT7996_PHY_BN_B000_F27_26));
	hal_set(MT7996_PHY_BN_8238(b), MT7996_PHY_BN_8238_B15);
	hal_clr(MT7996_PHY_DFE_F000(b), MT7996_PHY_DFE_F000_F3_1);

	/* Per BW class (V:e00de088..V:e00de4f4): BN_MODE [27:24] = the class; at BW160 DFE_F000
	 * [3:1]; off band 0 WF_COMM +0xd24c [15] (set at BW80 only); at BW320 BN_MODE [8] on the
	 * second channelization's centres. Classes >= 5 write nothing. */
	if (c->bw <= MT7996_CHSET_BW320) {
		hal_wf(MT7996_PHY_BN_MODE(b), MT7996_PHY_BN_MODE_CODE, c->bw);
		if (c->bw == MT7996_CHSET_BW160)
			hal_wf(MT7996_PHY_DFE_F000(b), MT7996_PHY_DFE_F000_F3_1,
			       MT7996_PHY_DFE_F000_F3_1_BW160);
		if (b != MT7996_PHY_BAND_2G4)
			hal_wf(MT7996_PHY_WFCOMM_D24C(b), MT7996_PHY_WFCOMM_D24C_B15,
			       c->bw == MT7996_CHSET_BW80);
		if (c->bw == MT7996_CHSET_BW320 && c->ch && MT7996_CHSET_320_2(c->ch))
			hal_set(MT7996_PHY_BN_MODE(b), MT7996_PHY_BN_MODE_B8);
	}

	rx_gate_open(b);

	/* V:e00de184..V:e00de202: the primary's index, the RF band by the base frequency. */
	hal_wf(MT7996_PHY_BN_MODE(b), MT7996_PHY_BN_MODE_PRI, c->sec);
	hal_set(MT7996_PHY_WFCOMM_3000(b), MT7996_PHY_WFCOMM_3000_B16);
	if (c->base_khz == MT7996_CHSET_BASE_KHZ_2G4) {
		hal_wf(MT7996_PHY_BN_MODE(b), MT7996_PHY_BN_MODE_RF, MT7996_PHY_BN_MODE_RF_2G4);
		hal_clr(MT7996_PHY_WFCOMM_3000(b), MT7996_PHY_WFCOMM_3000_F31_28);
		hal_set(MT7996_PHY_BN_8200(b), MT7996_PHY_BN_8200_B21);
	} else {
		if (c->base_khz == MT7996_CHSET_BASE_KHZ_5G ||
		    c->base_khz == MT7996_CHSET_BASE_KHZ_4G9)
			rf = MT7996_PHY_BN_MODE_RF_5G;
		else
			rf = MT7996_PHY_BN_MODE_RF_6G;
		hal_wf(MT7996_PHY_BN_MODE(b), MT7996_PHY_BN_MODE_RF, rf);
		hal_clr(MT7996_PHY_BN_8200(b), MT7996_PHY_BN_8200_B21);
	}

	/* V:e00de204..V:e00de24c */
	if (c->fem == MT7996_CHSET_FEM_2)
		hal_clr(MT7996_PHY_TSSI_CORE1(b), MT7996_PHY_TSSI_CORE1_B11);
	hal_set(MT7996_PHY_DFE_FD08(b), MT7996_PHY_DFE_FD08_B31);
	hal_clr(MT7996_PHY_DFE_FD08(b), MT7996_PHY_DFE_FD08_B31);

	rx_e338(c);
	if (c->mt7977)
		rx_gsel(c);
	hal_set(MT7996_PHY_BN_6098(b), MT7996_PHY_BN_6098_B0);
	rx_e338(c);

	rx_adie(c);

	/* V:e00de3dc */
	hal_wf(MT7996_PHY_WFCOMM_B580(b), MT7996_PHY_WFCOMM_B580_F24_23,
	       MT7996_PHY_WFCOMM_B580_F24_23_VAL);

	rx_adie_ch95(c);
}

/* ------------------------------------------------------------------------------------------------
 * The handler's optional parts (CHANSET_F_MAC, CHANSET_F_BGRX).
 * ------------------------------------------------------------------------------------------------ */

/* The channel set's optional parts (chanset.h CHANSET_F_MAC, CHANSET_F_BGRX): the MAC words of
 * MediaTek's channel-switch handler and its BW320 background-receiver stop / start.
 *
 * MediaTek's handler for UNI 0x34 tag 0 (V:e00b103c -> V:e00b0a74), in its order:
 *   chanset_mac_pre     RMAC SMPS_CTRL (V:e00b1006), the RATE BWCR code before the switch
 *                       (V:e00e54d2), band 0's RX stream count (V:e0123c70), LMAC ARB_SCR through
 *                       the TX / RX state machine, event 4 (V:e01245ec(4, band), V:e00b0b66);
 *   chanset_bgrx_stop   6 GHz BW320 only: V:e00ad640(0, 2) (V:e00b0c5e);
 *     (TX power, the temperature class, the channel flow V:e00e5626 -> V:e00d708a, the thermal TX
 *      power V:e00b0a42: chanset.c, or left out)
 *   chanset_bgrx_start  6 GHz BW320 only: V:e00ad4de(0, 2, 0) (V:e00b0cb8), MediaTek's radar
 *                       detector 2 on band 0's monitor chain (phy.h "Background (monitor)
 *                       receiver");
 *   chanset_mac_post    TMAC TFCR0 (V:e00f07e2); V:e00b193a + V:e00e5c9a (BW320: BN_MODE [8], TMAC
 *                       TCR0; RATE BWCR, WFSYS_ON TICKCR0, RMAC CHFREQ); V:e00f080a (BW320); BN
 *                       +0x8554 / +0x8d08, the EDCCA thresholds (V:e00afca2); ARB_SCR, event 7
 *                       (V:e01245ec(7, band)); 2.4 GHz only band 0's BN_BGRX [6:4] (V:e00caf72);
 *                       the handler tail's RATE BWCR write-back (V:e00b1322..V:e00b1346).
 * Left out: V:e00afefc (writes the EDCCA thresholds only from wifi.cfg values and an EDCCA region
 * our firmware does not have; no access in the stock load), V:e00afbf8 / V:e0126ede (TX power;
 * no access), and the handler tail's MIB (V:e00af2e0), TMAC / RATE (V:e00f9b96), its second SMPS
 * read (V:e0123c70), BF words from EEPROM 0xc00.. (V:e00facc2), AGG (V:e00a7e38), TMAC
 * (V:e00f044e) and its two TMAC reads for a log line.
 *
 * Values: the channel (ctx), MediaTek's immediates and CR-map fields (mac_names.h), registers read
 * here, and state MediaTek keeps in WM RAM, kept here in the four words below. The ARB_SCR words
 * are the state machine's result for a band whose MAC the host has not started (our firmware has
 * no MAC start): TX and RX disabled, as on every stock channel set.
 */

/* MediaTek's state between the handler's steps and across channel sets (all 0 at load, as
 * MediaTek's .bss). */
static uint32_t mac_nrx;	/* V:e0123c70's count: V:e00b0a74's stack argument (pre -> post) */
static uint32_t mac_bw_old;	/* V:e00e54d2: RATE BWCR's code before the switch (pre -> post) */
static uint32_t bgrx_nrx;	/* WM byte 0xe01ebfe5: band 0's last count (post -> bgrx_start) */
static uint32_t bgrx_first_done; /* WM byte 0xe01ec27a: the detector start's first use done */

/* LMAC ARB_SCR through V:e01245ec -> V:e01244b2: TX_DIS = !tx_en, RX_DIS = !rx_en, one GCI
 * transaction (one read, one write). */
static void arb_scr(uint32_t band, uint32_t tx_en, uint32_t rx_en)
{
	hal_rmw(MT7996_LMAC_ARB_SCR(band), MT7996_LMAC_ARB_SCR_TX_DIS | MT7996_LMAC_ARB_SCR_RX_DIS,
		(tx_en ? 0 : MT7996_LMAC_ARB_SCR_TX_DIS) | (rx_en ? 0 : MT7996_LMAC_ARB_SCR_RX_DIS));
}

static void chanset_mac_pre(const struct chanset_ctx *c)
{
	uint32_t b = c->band, mask = 0, v, i;

	/* V:e00b1006 (taken: MediaTek's gate byte 0x0222b43a is 1): nothing for 0 streams; else
	 * SMPS_MODE and SMPS_BY_RTS_ONLY in one transaction (V:e01240a2), then RX_STREAM = the chain
	 * mask of the rx_path count (V:e00edd0c). */
	if (c->nss_rx) {
		for (i = 0; i < c->nss_rx && i < MT7996_CHSET_RX_STREAM_BITS; i++)
			mask |= HW_BIT(i);
		hal_rmw(MT7996_RMAC_SMPS_CTRL(b), RMAC_SMPS_CTRL_MODE | RMAC_SMPS_CTRL_BY_RTS_ONLY,
			HW_PREP(RMAC_SMPS_CTRL_MODE, MT7996_CHSET_SMPS_MODE) |
				HW_PREP(RMAC_SMPS_CTRL_BY_RTS_ONLY, MT7996_CHSET_SMPS_BY_RTS_ONLY));
		hal_wf(MT7996_RMAC_SMPS_CTRL(b), RMAC_SMPS_CTRL_RX_STREAM, mask);
	}
	/* V:e00e54d2: the BWCR code before the switch, for the tail's write-back (post). */
	mac_bw_old = HW_GET(RATE_BWCR_BW, hal_rd(MT7996_RATE_BWCR(b)));
	/* V:e0123c70: the RX chains in RX_STREAM [3:0] (MediaTek's 80+80 pair rule, its width code 3,
	 * is not reachable from a BW class). Only the 2.4 GHz channel set writes a value from it
	 * (post, V:e00b0d66); MediaTek reads it on every band. */
	if (c->base_khz == MT7996_CHSET_BASE_KHZ_2G4) {
		v = HW_GET(RMAC_SMPS_CTRL_RX_STREAM, hal_rd(MT7996_RMAC_SMPS_CTRL(b)));
		mac_nrx = 0;
		for (i = 0; i < MT7996_CHSET_SMPS_COUNT_BITS; i++)
			if (v & HW_BIT(i))
				mac_nrx++;
	}
	/* V:e01245ec(4, band): forced TX / RX disable before the switch. */
	arb_scr(b, 0, 0);
}

/* ---------------------------------------------------------------------------------------------
 * BW320: the background receiver (detector 2 on band 0's monitor chain)
 * --------------------------------------------------------------------------------------------- */

#define BGRX_BAND	MT7996_PHY_BAND_2G4
#define BGRX_CHAIN	MT7996_PHY_MONITOR_CHAIN

/* V:e00b0c48..V:e00b0c50, V:e00b0ca6..V:e00b0cae: the channel record's base is 6 GHz and its BW
 * class 320 MHz. */
static int bgrx_channel(const struct chanset_ctx *c)
{
	return c->base_khz == MT7996_CHSET_BASE_KHZ_6G && c->bw == MT7996_CHSET_BW320;
}

/* V:e00ca95e(0, 2, en): detector 2's AHB client (enable) and its interrupt enable. */
static void bgrx_ahb_slot(uint32_t en)
{
	hal_wf(MT7996_PHY_AHB_CFG0(BGRX_BAND, MT7996_CHSET_BGRX_AHB_CLIENT), MT7996_PHY_AHB_CFG0_ENABLE, en);
	hal_wf(MT7996_PHY_AHB_ISR(BGRX_BAND), MT7996_PHY_AHB_ISR_ENABLE(MT7996_CHSET_BGRX_AHB_CLIENT), en);
}

/* The ROM gate V:e00ecf32 asks first (ROM V:008393c0 -> patch V:00903f1a -> V:00901bac): 1 =
 * detector 2 refused. Refused when die 0 is an MT7976C (0x79768a10, 0x79768b00) or when eFuse
 * 0x471 reads 0xa (die 0 also serves band 1, adie.h); a failed eFuse read allows it. MediaTek
 * reads the chip ID again (die 0 TOP 0x02c); radio_id has it. MediaTek's patch reads 0x471
 * from die 0's A-die eFuse (logical read of block 0x070 behind TOP 0x144 / 0x108); here the
 * ROM's read-only reader, as patch_init.c reads the same byte for die 0's band-1 role. */
static int bgrx_refused(void)
{
	uint32_t id = radio_id.chip_id[MT7996_PHY_BAND_ADIE(BGRX_BAND)];
	uint8_t sel;

	if (id == ADIE_CHIP_ID_MT7976C_C || id == ADIE_CHIP_ID_MT7976C_B)
		return 1;
	if (hal_efuse8_soft(MT7996_EFUSE_7976_SEL, &sel))
		return 0;
	return sel == EFUSE_7976_SEL_BAND1;
}

static void chanset_bgrx_stop(const struct chanset_ctx *c)
{
	if (!bgrx_channel(c))
		return;
	/* V:e00ad640(0, 2) -> V:e00ed1a8(0, 2, 0): ROM halRdm enable (slot 0x0081bcf0) does nothing
	 * for detector 2; the AHB client off. */
	bgrx_ahb_slot(0);
}

static void chanset_bgrx_start(const struct chanset_ctx *c)
{
	uint32_t v0, v1;

	if (!bgrx_channel(c))
		return;
	/* V:e00ad4de (rdmRddStart(0, 2, 0)) */
	hal_wr(MT7996_WF_TOP_ON_SLPPROT_REQ2, WF_TOP_ON_SLPPROT_REQ2_RDD_START);
	bgrx_ahb_slot(0);	/* V:e00ed1a8(0, 2, 0), as at the stop */
	/* V:e00ecf32(0, 2, buffer, 0): the detector hardware start */
	if (bgrx_refused())
		return;
	hal_wf(MT7996_PHY_RDD_TOP12(BGRX_BAND, MT7996_CHSET_BGRX_RDD), MT7996_PHY_RDD_TOP12_ANT_SEL, BGRX_CHAIN);
	/* V:e00cad1a(0, 2, 1) */
	hal_wf(MT7996_PHY_BN_CTRL0(BGRX_BAND), MT7996_PHY_BN_CTRL0_F28_24, MT7996_CHSET_BGRX_CTRL0_ON);
	hal_wf(MT7996_PHY_BN_CTRL0(BGRX_BAND), MT7996_PHY_BN_CTRL0_F20_16, MT7996_CHSET_BGRX_CTRL0_ON);
	hal_set(MT7996_PHY_AHB_CFG0(BGRX_BAND, MT7996_CHSET_BGRX_AHB_CLIENT), MT7996_PHY_AHB_CFG0_STALL_EN);
	/* V:e00caa6e(.., 1), (.., 0) */
	hal_set(MT7996_PHY_RST_CTRL(MT7996_CHSET_BGRX_RST_BAND), MT7996_PHY_RST_RDD);
	hal_clr(MT7996_PHY_RST_CTRL(MT7996_CHSET_BGRX_RST_BAND), MT7996_PHY_RST_RDD);
	/* V:e00ca89c, V:e00caa2a: the pulse window (whole words, MediaTek's read discarded) */
	hal_rmw(MT7996_PHY_AHB_START(BGRX_BAND, MT7996_CHSET_BGRX_AHB_CLIENT), ~0u, MT7996_WM_SYSRAM_RDD2_BUF);
	hal_rmw(MT7996_PHY_AHB_END(BGRX_BAND, MT7996_CHSET_BGRX_AHB_CLIENT), ~0u,
		MT7996_WM_SYSRAM_RDD2_BUF + MT7996_WM_SYSRAM_RDD_BUF_SIZE);
	/* GCI 0x360000 (CR_AHBBRG_AHB_ENABLE), ROM V:008299ca (stall, client 2), V:e00ca95e(0, 2, 1) */
	hal_set(MT7996_PHY_AHB_TOP(BGRX_BAND), MT7996_PHY_AHB_TOP_ENABLE);
	hal_set(MT7996_PHY_AHB_CFG0(BGRX_BAND, MT7996_CHSET_BGRX_AHB_STALL), MT7996_PHY_AHB_CFG0_STALL_EN);
	bgrx_ahb_slot(1);
	/* V:e0133a02(0, 0, 1, 0): both clock words read, then both written */
	v0 = hal_rd(MT7996_PHY_BN_CLK0(BGRX_BAND));
	v1 = hal_rd(MT7996_PHY_BN_CLK1(BGRX_BAND));
	hal_wr(MT7996_PHY_BN_CLK0(BGRX_BAND), v0 | MT7996_PHY_BN_CLK_B8);
	hal_wr(MT7996_PHY_BN_CLK1(BGRX_BAND), v1 | MT7996_PHY_BN_CLK_B8);
	/* detector 2 (V:e00ed09a..): V:e00caca4(0, 2, 1) the monitor front-end enables */
	hal_set(MT7996_PHY_BN_BGRX_EN(BGRX_BAND), MT7996_PHY_BN_BGRX_EN_ON);
	hal_set(MT7996_PHY_BGRX_FE, MT7996_PHY_BGRX_FE_EN24);
	hal_set(MT7996_PHY_BGRX_FE, MT7996_PHY_BGRX_FE_EN25);
	hal_set(MT7996_PHY_BGRX_FE, MT7996_PHY_BGRX_FE_EN15);
	hal_wf(MT7996_PHY_BGRX_FE, MT7996_PHY_BGRX_FE_F14_9, 0);	/* = !en */
	hal_wr(MT7996_PHY_BN_BGRX(BGRX_BAND), MT7996_PHY_BN_BGRX_RDD2_INIT);
	/* V:e00caf72: band 0's last RX stream count - 1 */
	hal_wf(MT7996_PHY_BN_BGRX(BGRX_BAND), MT7996_PHY_BN_BGRX_6_4, bgrx_nrx - 1u);
	hal_set(MT7996_PHY_BGRX_FE0, MT7996_PHY_BGRX_FE0_EN);	/* V:e00cac2a */
	/* the monitor chain's gain: V:e00cab7c, V:e00cab98, V:e00cabc8, V:e00cabe2, V:e00cabfc */
	hal_wf(MT7996_PHY_AGC_RFDGC(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_AGC_RFDGC_A_GAIN, MT7996_CHSET_BGRX_RFDGC);
	hal_set(MT7996_PHY_RXGAIN(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_RXGAIN_A_EN);
	hal_wf(MT7996_PHY_RXGAIN(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_RXGAIN_A_LNA, MT7996_PHY_RXGAIN_LNA_UH);
	hal_wf(MT7996_PHY_RXGAIN(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_RXGAIN_A_LPF, MT7996_CHSET_BGRX_LPF);
	hal_wf(MT7996_PHY_RXGAIN(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_RXGAIN_A_LNA, MT7996_PHY_RXGAIN_LNA_UH);
	hal_wf(MT7996_PHY_RXGAIN(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_RXGAIN_A_LPF, MT7996_CHSET_BGRX_LPF);
	hal_set(MT7996_PHY_AGC_CAGC(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_AGC_CAGC_SW_EN);
	hal_wf(MT7996_PHY_AGC_CAGC(BGRX_BAND, BGRX_CHAIN), MT7996_PHY_AGC_CAGC_SW_STATE,
	       MT7996_CHSET_BGRX_CAGC_STATE);
	/* V:e00cac62(0, 2, 1) */
	hal_wf(MT7996_PHY_BGRX_307C(BGRX_BAND), MT7996_PHY_BGRX_307X_VAL, MT7996_CHSET_BGRX_307X_ON);
	hal_wf(MT7996_PHY_BGRX_3078(BGRX_BAND), MT7996_PHY_BGRX_307X_VAL, MT7996_CHSET_BGRX_307X_ON);
	hal_rmw(MT7996_PHY_CSI_CLK0(BGRX_BAND), ~0u, MT7996_CHSET_BGRX_CSI_CLK);
	hal_rmw(MT7996_PHY_CSI_CLK1(BGRX_BAND), ~0u, MT7996_CHSET_BGRX_CSI_CLK);
	hal_set(MT7996_PHY_BGRX_EN(BGRX_BAND), MT7996_PHY_BGRX_EN_ON);
	/* V:e00caa8c(.., 1), V:e00cac46(.., 0), V:e00caa8c(.., 0) */
	hal_set(MT7996_PHY_BGRX_5DF4(BGRX_BAND), MT7996_PHY_BGRX_5DF4_B30);
	hal_set(MT7996_PHY_BGRX_5DF4(BGRX_BAND), MT7996_PHY_BGRX_5DF4_B31);
	hal_clr(MT7996_PHY_RDD_2148(BGRX_BAND), MT7996_PHY_RDD_2148_B20);
	hal_clr(MT7996_PHY_BGRX_5DF4(BGRX_BAND), MT7996_PHY_BGRX_5DF4_B30);
	hal_clr(MT7996_PHY_BGRX_5DF4(BGRX_BAND), MT7996_PHY_BGRX_5DF4_B31);
	/* V:e00caedc(0, 2, 1): the detector's interrupt enables */
	hal_set(MT7996_PHY_MISC_CTRL(BGRX_BAND), MT7996_PHY_MISC_RDD1_WAKEUP);
	hal_set(MT7996_PHY_MISC_CTRL(BGRX_BAND), MT7996_PHY_MISC_RDD1_ERROR);
	if (bgrx_first_done)
		return;
	/* first use: V:e00caacc(0, 2, 1), V:e00cab5c(0, 2, 0xb0). (MediaTek then saves BGRX_CFG
	 * [18:11] in WM RAM for another routine, V:e0133a5e: not read here.) */
	hal_wf(MT7996_PHY_BGRX_CFG(BGRX_BAND), MT7996_PHY_BGRX_CFG_F6_4, 0);	/* = !en */
	hal_wf(MT7996_PHY_RX_CTRL1(BGRX_BAND), MT7996_PHY_RX_CTRL1_IPI_EN, MT7996_CHSET_BGRX_IPI_EN);
	hal_set(MT7996_PHY_BGRX_CFG(BGRX_BAND), MT7996_PHY_BGRX_CFG_B9);
	hal_set(MT7996_PHY_BGRX_CFG(BGRX_BAND), MT7996_PHY_BGRX_CFG_B8);
	hal_clr(MT7996_PHY_BGRX_CFG(BGRX_BAND), MT7996_PHY_BGRX_CFG_B7);	/* = !en */
	hal_wf(MT7996_PHY_BGRX_CFG(BGRX_BAND), MT7996_PHY_BGRX_CFG_19_10, MT7996_CHSET_BGRX_CFG_19_10_FIRST);
	hal_wf(MT7996_PHY_BGRX_CFG(BGRX_BAND), MT7996_PHY_BGRX_CFG_F29_20, 0);	/* = !en */
	hal_wf(MT7996_PHY_BGRX_5DF4(BGRX_BAND), MT7996_PHY_BGRX_5DF4_F29_21, MT7996_CHSET_BGRX_5DF4_INIT);
	hal_wf(MT7996_PHY_BGRX_CFG(BGRX_BAND), MT7996_PHY_BGRX_CFG_19_10, MT7996_CHSET_BGRX_CFG_19_10);
	bgrx_first_done = 1;
}

/* ---------------------------------------------------------------------------------------------
 * After the channel flow
 * --------------------------------------------------------------------------------------------- */

/* V:e00e5c9a's BWCR code from the channel (mac_names.h MT7996_CHSET_BWCR_*). */
static uint32_t bwcr_code(const struct chanset_ctx *c)
{
	switch (c->bw) {
	case MT7996_CHSET_BW80:
		return MT7996_CHSET_BWCR_BW80;
	case MT7996_CHSET_BW160:
		return MT7996_CHSET_BWCR_BW160;
	case MT7996_CHSET_BW320:
		return MT7996_CHSET_BWCR_BW320;
	}
	return c->bw == MT7996_CHSET_BW40;	/* 20 / 40 MHz: MediaTek's width code (primary != centre) */
}

/* V:e00b193a and V:e00e5d0a..V:e00e5d2c: the control channel as RMAC CHFREQ numbers it; ours: the
 * LO's raster number (no primary), 0 off the raster. */
static uint32_t chfreq(const struct chanset_ctx *c)
{
	uint32_t ch = c->ch;

	if (!ch)
		return 0;
	if (c->base_khz == MT7996_CHSET_BASE_KHZ_6G)
		return HW_GET(MT7996_CHSET_CHFREQ_6G_IDX, ch - 1u) - MT7996_CHSET_CHFREQ_6G_SUB;
	if (c->base_khz == MT7996_CHSET_BASE_KHZ_2G4 && ch > MT7996_CHSET_2G4_CH_MAX)
		return MT7996_CHSET_CHFREQ_2G4_BAD;
	return ch;
}

/* V:e00cba3e: both words read; wr: both written (+0x8554 first) from t[], else read into t[]. */
static void ed_access(uint32_t band, int wr, int32_t *t)
{
	uint32_t a = hal_rd(MT7996_PHY_BN_8554(band)), d = hal_rd(MT7996_PHY_BN_8D08(band));

	if (!wr) {
		t[0] = HW_GET(MT7996_PHY_BN_8554_F0, a);
		t[1] = HW_GET(MT7996_PHY_BN_8554_F1, a);
		t[2] = HW_GET(MT7996_PHY_BN_8554_F2, a);
		t[3] = HW_GET(MT7996_PHY_BN_8D08_F0, d);
		return;
	}
	hal_wr(MT7996_PHY_BN_8554(band), (a & MT7996_PHY_BN_8554_KEEP) | HW_PREP(MT7996_PHY_BN_8554_F2, t[2]) |
						 HW_PREP(MT7996_PHY_BN_8554_F1, t[1]) |
						 HW_PREP(MT7996_PHY_BN_8554_F0, t[0]));
	hal_wr(MT7996_PHY_BN_8D08(band), (d & ~MT7996_PHY_BN_8D08_F0) | HW_PREP(MT7996_PHY_BN_8D08_F0, t[3]));
}

/* V:e00e6362: the current words read; entries MT7996_CHSET_EDCCA_KEEP take them; then V:e00cba3e. */
static void ed_apply(uint32_t band, int wr, int32_t *t)
{
	int32_t cur[MT7996_CHSET_EDCCA_N];
	uint32_t i;

	ed_access(band, 0, cur);
	for (i = 0; i < MT7996_CHSET_EDCCA_N; i++)
		if (t[i] == (int32_t)MT7996_CHSET_EDCCA_KEEP)
			t[i] = cur[i];
	ed_access(band, wr, t);
}

static void chanset_mac_post(const struct chanset_ctx *c)
{
	uint32_t b = c->band, code = bwcr_code(c), bw320 = c->bw == MT7996_CHSET_BW320, loc, i;
	int32_t t[MT7996_CHSET_EDCCA_N] = { 0 };

	loc = bw320 && c->ch && MT7996_CHSET_320_2(c->ch);
	/* V:e00f07e2 */
	hal_wf(MT7996_TMAC_TFCR0(b), TMAC_TFCR0_PRIM20M_CH, c->sec);
	/* V:e00e5c9a: BW320 first band 2's BN_MODE [8] (a fixed address) and TCR0 (V:e00d09ba) */
	if (bw320) {
		hal_wf(MT7996_PHY_BN_MODE(MT7996_PHY_BAND_6G), MT7996_PHY_BN_MODE_B8, loc);
		hal_wf(MT7996_TMAC_TCR0(b), TMAC_TCR0_BW320M_LOC, loc);
	}
	hal_wf(MT7996_RATE_BWCR(b), RATE_BWCR_BW, code);
	for (i = 0; i < MT7996_CHSET_TICKCR0_BANDS; i++) {
		hal_wf(MT7996_WFSYS_ON_TICKCR0(i), WFSYS_ON_TICKCR0_WLAN_CLK_MODE, MT7996_CHSET_TICKCR0_MODE);
		hal_wf(MT7996_WFSYS_ON_TICKCR0(i), WFSYS_ON_TICKCR0_LMAC_1X_MODE, MT7996_CHSET_TICKCR0_MODE);
	}
	hal_wf(MT7996_RMAC_CHFREQ(b), RMAC_CHFREQ_CH, chfreq(c));
	/* V:e00f080a (V:e00b0d04: MediaTek's 320 MHz width code) */
	if (bw320)
		hal_wf(MT7996_TMAC_TCR0(b), TMAC_TCR0_BW320M_LOC, loc);
	/* V:e00afca2, EDCCA region 0: read, minus MT7996_CHSET_EDCCA_OFS, written back */
	ed_apply(b, 0, t);
	for (i = 0; i < MT7996_CHSET_EDCCA_N; i++)
		t[i] -= (int32_t)MT7996_CHSET_EDCCA_OFS;
	ed_apply(b, 1, t);
	/* V:e01245ec(7, band): the state machine re-evaluated (band not started: both disabled) */
	arb_scr(b, 0, 0);
	/* V:e00b0d66..V:e00b0d88: 2.4 GHz, when band 0's count changed: BN_BGRX [6:4] (V:e00caf72) */
	if (c->base_khz == MT7996_CHSET_BASE_KHZ_2G4 && bgrx_nrx != mac_nrx) {
		bgrx_nrx = mac_nrx;
		hal_wf(MT7996_PHY_BN_BGRX(MT7996_PHY_BAND_2G4), MT7996_PHY_BN_BGRX_6_4, mac_nrx - 1u);
	}
	/* the handler tail (V:e00b1322..V:e00b1346): a smaller code than before the switch is
	 * replaced by the old one */
	if (code < mac_bw_old)
		hal_wf(MT7996_RATE_BWCR(b), RATE_BWCR_BW, mac_bw_old);
}

/* ------------------------------------------------------------------------------------------------
 * The channel set
 * ------------------------------------------------------------------------------------------------ */

/* Each band's last channel-set flags, for chanset_rate. */
static uint32_t chan_flags[RADIO_BANDS];

/* R6 ours: the synthesizer to the LO with TUNE's own program (rfctl_tune), run here; the band's
 * state (radio_band) first, so the program's frequency words use the new bandwidth. */
static uint32_t chanset_tune(const struct chanset_ctx *c)
{
	uint64_t actual;
	uint32_t health, n, r;

	radio_band[c->band].bw = c->bw;
	radio_band[c->band].set = 1;
	n = rfctl_tune(c->band, c->hz, tune_ops, RFCTL_MAX_OPS, &actual, &health);
	if (!n)
		return RADIO_ERR_ARG;
	r = rfctl_run(tune_ops, n, tune_res);
	if (r != RADIO_OK)
		return r;
	radio_band[c->band].lo_hz = actual;
	if (HW_GET(ADIE_SX_STATUS_VCO, tune_res[health]) != ADIE_SX_STATUS_VCO_LOCKED) {
		hal_fail(HAL_FAIL_STATE, tune_res[health]);
		radio_band[c->band].lock_hz = 0;
		return RADIO_ERR_HW;
	}
	radio_band[c->band].lock_hz = actual;	/* a HOLD (rfctl_hold) moves the LO from here */
	return RADIO_OK;
}

/* The flow on a prepared context; the synthesizer (R6) only with sx. */
static uint32_t chanset_flow(const struct chanset_ctx *c, int sx)
{
	uint32_t wf, r = RADIO_OK, flags = c->flags;

	/* the handler V:e00b103c / V:e00b0a74 before the channel flow */
	if (flags & CHANSET_F_MAC)
		chanset_mac_pre(c);
	if (flags & CHANSET_F_BGRX)
		chanset_bgrx_stop(c);

	/* the channel flow V:e00d708a: per WF, item 1 (MT7977), the first gain-step pass */
	for (wf = c->wf_first; wf <= c->wf_last; wf++) {
		if (!(hal_rd(MT7996_PHY_FW_D434) & MT7996_PHY_FW_D434_SKIP_ITEM1) && c->mt7977)
			item1(c, wf);
		gainstep_pass(c, wf, 1);
	}
	if (flags & CHANSET_F_TABLE_A)
		table_a(c);
	table_b(c);
	rx_frontend(c);
	rx_setup(c);
	if (sx)
		r = chanset_tune(c);
	else
		radio_band[c->band].bw = c->bw;
	for (wf = c->wf_first; wf <= c->wf_last; wf++)
		gainstep_pass(c, wf, 0);

	/* the handler after the flow */
	if (flags & CHANSET_F_BGRX)
		chanset_bgrx_start(c);
	if (flags & CHANSET_F_MAC)
		chanset_mac_post(c);
	else if (c->base_khz == MT7996_CHSET_BASE_KHZ_2G4)
		/* without the MAC words: band 0's RX chain count as MediaTek's SMPS step would have
		 * written and read it back (RX_STREAM = the rx_path mask), for a later BW320 start */
		bgrx_nrx = c->nss_rx < MT7996_CHSET_SMPS_COUNT_BITS ? c->nss_rx : MT7996_CHSET_SMPS_COUNT_BITS;
	if (r != RADIO_OK || radio_failed())
		return RADIO_ERR_HW;
	return RADIO_OK;
}

uint32_t chanset(uint32_t band, uint64_t hz, uint32_t bw, uint32_t flags)
{
	struct chanset_ctx c;
	uint32_t r;

	if (!radio_id.identified)
		return RADIO_ERR_ORDER;
	r = chanset_prepare(&c, band, hz, bw, flags);
	if (r != RADIO_OK)
		return r;
	radio_band[band].set = 0;
	r = chanset_flow(&c, 1);
	if (r == RADIO_OK)
		chan_flags[band] = flags;
	else
		radio_band[band].set = 0;
	return r;
}

uint32_t chanset_rate(uint32_t band, uint32_t bw)
{
	struct chanset_ctx c;
	uint32_t r;

	if (band >= RADIO_BANDS || bw >= MT7996_CHSET_NBW)
		return RADIO_ERR_ARG;
	if (!radio_id.identified || !radio_band[band].set)
		return RADIO_ERR_ORDER;
	r = chanset_prepare(&c, band, radio_band[band].lo_hz, bw, chan_flags[band]);
	if (r != RADIO_OK)
		return r;
	return chanset_flow(&c, 0);
}
