// SPDX-License-Identifier: MIT
/* RF control commands: see rfctl.h. Registers: hw/adie/sx.h, mt7976.h, mt7977.h and adie.h;
 * MediaTek's SX80_E3, RCD and MT7976 preset tables: hw/adie/sxchan.h, with the Groups' and RCD
 * ranges' bounds from mt7977.h; D-die words: hw/mt7996/phy.h and icap.h; the op format:
 * hw/mt7996/probe_page.h. Integer arithmetic only, and no 64-bit division (a shift-and-subtract
 * helper below): the build is freestanding.
 */
#include "hw/adie/mt7976.h"
#include "hw/adie/mt7977.h"
#include "hw/adie/sx.h"
#include "hw/adie/sxchan.h"
#include "hw/mt7996/icap.h"
#include "hw/mt7996/phy.h"
#include "hw/mt7996/phy_chan.h"

#include "radio.h"
#include "rfctl.h"

#define NELEM(a)		(sizeof(a) / sizeof((a)[0]))

/* ---------------------------------------------------------------------------------------------
 * Tables: register lists, values row-major (one value per key column), keys, and bounds.
 * --------------------------------------------------------------------------------------------- */
#define ROW_REG(reg, ...)	reg,
#define ROW_VAL(reg, ...)	{ __VA_ARGS__ },
#define PRE_REG(reg, val)	reg,
#define PRE_VAL(reg, val)	val,
#define KEY(k)			k,
#define BOUND_N(n, lo, hi, ...)	n,
#define BOUND_LO(n, lo, hi, ...) lo,
#define BOUND_HI(n, lo, hi, ...) hi,

static const uint8_t e3_keys[] = { MT7977_SXCH_E3_KEYS(KEY) };
static const uint16_t e3_reg[] = { MT7977_SXCH_E3(ROW_REG) };
static const uint32_t e3_val[][NELEM(e3_keys)] = { MT7977_SXCH_E3(ROW_VAL) };
static const uint16_t rcd_pre_reg[] = { MT7977_SXCH_RCD_PRE(PRE_REG) };
static const uint32_t rcd_pre_val[] = { MT7977_SXCH_RCD_PRE(PRE_VAL) };
static const uint8_t rcd_keys[] = { MT7977_SXCH_RCD_KEYS(KEY) };
static const uint16_t rcd_reg[] = { MT7977_SXCH_RCD(ROW_REG) };
static const uint32_t rcd_val[][NELEM(rcd_keys)] = { MT7977_SXCH_RCD(ROW_VAL) };

static const uint16_t t76_pre_reg[] = { MT7976_SXCH_PRE(PRE_REG) };
static const uint32_t t76_pre_val[] = { MT7976_SXCH_PRE(PRE_VAL) };
static const uint8_t t76_a_keys[] = { MT7976_SXCH_A_KEYS(KEY) };
static const uint16_t t76_a_reg[] = { MT7976_SXCH_A(ROW_REG) };
static const uint32_t t76_a_val[][NELEM(t76_a_keys)] = { MT7976_SXCH_A(ROW_VAL) };
static const uint8_t t76_b_keys[] = { MT7976_SXCH_B_KEYS(KEY) };
static const uint16_t t76_b_reg[] = { MT7976_SXCH_B(ROW_REG) };
static const uint32_t t76_b_val[][NELEM(t76_b_keys)] = { MT7976_SXCH_B(ROW_VAL) };

/* The SX80_E3 Groups and the RCD ranges by integer LO MHz (mt7977.h). */
static const uint8_t group_n[] = { MT7977_SX_E3_ROWS(BOUND_N) };
static const uint16_t group_lo[] = { MT7977_SX_E3_ROWS(BOUND_LO) };
static const uint16_t group_hi[] = { MT7977_SX_E3_ROWS(BOUND_HI) };
static const uint8_t range_n[] = { MT7977_RCD_ROWS(BOUND_N) };
static const uint16_t range_lo[] = { MT7977_RCD_ROWS(BOUND_LO) };
static const uint16_t range_hi[] = { MT7977_RCD_ROWS(BOUND_HI) };

/* The VCO band-code ramp before the SAR (mt7977.h step 5). */
static const uint16_t sar_ramp[] = { MT7977_SAR_RAMP0, MT7977_SAR_RAMP1, MT7977_SAR_RAMP2, MT7977_SAR_RAMP3 };

/* Band b is die b; its chains, and its synthesizer's bank. */
_Static_assert(MT7976_DIE == 0 && MT7977_DIE_BAND1 == 1 && MT7977_DIE_BAND2 == 2 && RFCTL_BANDS == 3,
	       "band b is A-die die b: the MT7976, then the two MT7977");
static const uint8_t chains[RFCTL_BANDS] = { MT7976_NUM_CHAINS, MT7977_NUM_CHAINS_BAND1, MT7977_NUM_CHAINS_BAND2 };

static uint32_t sx_k(uint32_t band)
{
	return band == MT7976_DIE ? MT7976_K_SX : MT7977_K_SX;
}

/* The column of a key value: its position in the key list (MediaTek's linear search); nkeys if
 * absent. */
static uint32_t column(const uint8_t *keys, uint32_t nkeys, uint32_t key)
{
	uint32_t i;

	for (i = 0; i < nkeys; i++)
		if (keys[i] == key)
			break;
	return i;
}

/* ---------------------------------------------------------------------------------------------
 * Ops: written while they fit; done() is 0 when they did not.
 * --------------------------------------------------------------------------------------------- */
struct prog {
	struct rfctl_op *ops;
	uint32_t n, max;
};

static void put(struct prog *p, uint32_t op, uint32_t a, uint32_t b, uint32_t c)
{
	if (p->n < p->max) {
		p->ops[p->n].op = op;
		p->ops[p->n].a = a;
		p->ops[p->n].b = b;
		p->ops[p->n].c = c;
	}
	p->n++;
}

static uint32_t done(const struct prog *p)
{
	return p->n <= p->max ? p->n : 0;
}

static void aw(struct prog *p, uint32_t die, uint32_t k, uint32_t idx, uint32_t val)
{
	put(p, MT7996_PROBE_CTL_OP_AW, MT7996_PROBE_CTL_AREG(die, k, idx), val, 0);
}

static void ar(struct prog *p, uint32_t die, uint32_t k, uint32_t idx)
{
	put(p, MT7996_PROBE_CTL_OP_AR, MT7996_PROBE_CTL_AREG(die, k, idx), 0, 0);
}

static void armw(struct prog *p, uint32_t die, uint32_t k, uint32_t idx, uint32_t mask, uint32_t val)
{
	put(p, MT7996_PROBE_CTL_OP_ARMW, MT7996_PROBE_CTL_AREG(die, k, idx), mask, val);
}

static void w32(struct prog *p, uint32_t addr, uint32_t val)
{
	put(p, MT7996_PROBE_CTL_OP_W32, addr, val, 0);
}

static void rmw32(struct prog *p, uint32_t addr, uint32_t mask, uint32_t val)
{
	put(p, MT7996_PROBE_CTL_OP_RMW32, addr, mask, val);
}

static void r32(struct prog *p, uint32_t addr)
{
	put(p, MT7996_PROBE_CTL_OP_R32, addr, 0, 0);
}

/* A lock step's wait as the firmware's: ADIE_SX_SETTLE_US, then the done flag polled for up to
 * ADIE_SX_LOCK_TIMEOUT_US, a timeout only counted (SOFT), as the firmware ignores its own. */
static void lock_wait(struct prog *p, uint32_t die, uint32_t k)
{
	put(p, MT7996_PROBE_CTL_OP_DELAY | HW_PREP(MT7996_PROBE_CTL_OP_ARG, ADIE_SX_SETTLE_US), 0, 0, 0);
	put(p, MT7996_PROBE_CTL_OP_APOLL | MT7996_PROBE_CTL_OP_SOFT |
	       HW_PREP(MT7996_PROBE_CTL_OP_ARG, ADIE_SX_LOCK_TIMEOUT_US),
	    MT7996_PROBE_CTL_AREG(die, k, ADIE_SX_DONE), ADIE_SX_DONE_FLAG, ADIE_SX_DONE_FLAG);
}

/* ---------------------------------------------------------------------------------------------
 * Arithmetic
 * --------------------------------------------------------------------------------------------- */

/* n / d and its remainder, by shift and subtract (the build has no 64-bit division). */
uint64_t rfctl_div64(uint64_t n, uint32_t d, uint32_t *rem)
{
	uint64_t q = 0, r = 0;

	for (int i = 63; i >= 0; i--) {
		r = r << 1 | (n >> i & 1u);
		if (r >= d) {
			r -= d;
			q |= (uint64_t)1 << i;
		}
	}
	*rem = (uint32_t)r;
	return q;
}

/* n / d rounded to the nearest, ties to even: Python's round() of the exact quotient. */
static uint64_t div_round(uint64_t n, uint32_t d)
{
	uint32_t r;
	uint64_t q = rfctl_div64(n, d, &r);

	if ((uint64_t)r * 2u > d || ((uint64_t)r * 2u == d && (q & 1u)))
		q++;
	return q;
}

/* The frequency words for LO hz at LO = VCO num / den: N = hz den / (num pfd) in Q24, rounded to
 * the nearest point of the grid the SX honours (an even FRAC: ADIE_SX_FRAC_STEP_LSB); INT word =
 * N's integer part | VT of the integer MHz (SXCH_VT, floor(3 den MHz / num + 1.5)), FRAC word =
 * LOAD | N's fraction; and the LO those words give, rounded to the nearest Hz. 0 when N's integer
 * part does not fit ADIE_SX_INT_N. */
static int divider(uint64_t hz, uint32_t mhz, uint32_t num, uint32_t den, uint32_t pfd_hz, uint32_t *int_w,
		   uint32_t *frac_w, uint64_t *actual_hz)
{
	uint32_t d = num * pfd_hz, r, n;
	/* N in Q24 on the grid the SX honours: FRAC bit 0 is ignored (ADIE_SX_FRAC_STEP_LSB) */
	uint64_t q = rfctl_div64(((hz * den) << (SXCH_FRAC_BITS - 1u)) + d / 2u, d, &r) << 1;

	n = (uint32_t)(q >> SXCH_FRAC_BITS);
	if (!n || n > HW_GET(ADIE_SX_INT_N, ADIE_SX_INT_N))
		return 0;
	*int_w = HW_PREP(ADIE_SX_INT_N, n) | HW_PREP(ADIE_SX_INT_VT, SXCH_VT(mhz, num, den));
	*frac_w = ADIE_SX_FRAC_LOAD | ((uint32_t)q & ADIE_SX_FRAC_F);
	*actual_hz = div_round(q * d, den << SXCH_FRAC_BITS);
	return 1;
}

uint32_t rfctl_freq_word(uint32_t num, uint32_t mhz)
{
	return num / mhz + ((num * MT7996_CHSET_ROUND_DIGITS / mhz) % MT7996_CHSET_ROUND_DIGITS >
			    MT7996_CHSET_ROUND_HALF);
}

/* The D-die words that follow the LO (chanset.c R4, V:e00d188e / V:e00d196a): BN +0x6188 both
 * halves, DFE +0xf168, CHAIN +0xd0ac of chains 0..4 at the band's bandwidth class. */
static void freq_words(struct prog *p, uint32_t band, uint32_t mhz, uint32_t bw)
{
	uint32_t v = rfctl_freq_word(MT7996_CHSET_6188_NUM, mhz), f = rfctl_freq_word(MT7996_CHSET_F168_NUM, mhz);

	rmw32(p, MT7996_PHY_BN_6188(band), MT7996_PHY_BN_6188_LO | MT7996_PHY_BN_6188_HI,
	      HW_PREP(MT7996_PHY_BN_6188_LO, v) | HW_PREP(MT7996_PHY_BN_6188_HI, v));
	rmw32(p, MT7996_PHY_DFE_F168(band), MT7996_PHY_DFE_F168_VAL, HW_PREP(MT7996_PHY_DFE_F168_VAL, f));
	for (uint32_t i = 0; i < MT7996_CHSET_D0AC_CHAINS; i++)
		rmw32(p, MT7996_PHY_CHAIN_D0AC(band, i), MT7996_PHY_CHAIN_D0AC_VAL,
		      HW_PREP(MT7996_PHY_CHAIN_D0AC_VAL, f << MT7996_CHSET_D0AC_SHIFT(bw)));
}

/* ---------------------------------------------------------------------------------------------
 * TUNE
 * --------------------------------------------------------------------------------------------- */

/* The SX80_E3 Group of an integer LO MHz; 0 outside every Group. */
static uint32_t mt7977_group(uint32_t mhz)
{
	for (uint32_t i = 0; i < NELEM(group_n); i++)
		if (group_lo[i] <= mhz && mhz <= group_hi[i])
			return group_n[i];
	return 0;
}

/* The RCD range of an integer LO MHz; anything outside is range 0 (mt7977.h). */
static uint32_t mt7977_range(uint32_t mhz)
{
	for (uint32_t i = 0; i < NELEM(range_n); i++)
		if (range_lo[i] <= mhz && mhz <= range_hi[i])
			return range_n[i];
	return 0;
}

/* The presets: the SX80_E3 column of the Group (MediaTek's key MT7977_SXCH_KEY), whose band-code
 * row only sets the preset field ADIE_SX_BAND_PRESET (the SAR owns the code above it), then on
 * k 4..0 the RCD pre-row and the RCD rows of the range. */
static int mt7977_presets(struct prog *p, uint32_t die, uint32_t mhz, uint32_t ratio58)
{
	uint32_t g = mt7977_group(mhz), c, range, v, i;

	if (!g)
		g = mhz < group_lo[0] ? group_n[0] : group_n[NELEM(group_n) - 1];
	if (ratio58 && g > MT7977_SX_E3_LAST_5_8)
		g = MT7977_SX_E3_LAST_5_8;
	if (!ratio58 && g <= MT7977_SX_E3_LAST_5_8)
		g = MT7977_SX_E3_LAST_5_8 + 1u;
	c = column(e3_keys, NELEM(e3_keys), MT7977_SXCH_KEY(g, ratio58));
	if (c >= NELEM(e3_keys))
		return 0;
	for (i = 0; i < NELEM(e3_reg); i++) {
		v = e3_val[i][c];
		if (v == SXCH_SKIP)
			continue;
		if (e3_reg[i] == ADIE_SX_BAND)
			armw(p, die, MT7977_K_SX, ADIE_SX_BAND, ADIE_SX_BAND_PRESET, v & ADIE_SX_BAND_PRESET);
		else
			aw(p, die, MT7977_K_SX, e3_reg[i], v);
	}
	range = mt7977_range(mhz);
	for (int k = MT7977_K_SX; k >= (int)ADIE_K_WF(0); k--) {
		for (i = 0; i < NELEM(rcd_pre_reg); i++)
			aw(p, die, k, rcd_pre_reg[i], rcd_pre_val[i]);
		c = column(rcd_keys, NELEM(rcd_keys), MT7977_SXCH_RCD_KEY(range, k));
		if (c >= NELEM(rcd_keys))
			return 0;
		for (i = 0; i < NELEM(rcd_reg); i++)
			aw(p, die, k, rcd_reg[i], rcd_val[i][c]);
	}
	return 1;
}

/* mt7977.h "Retune sequence" steps 1..9 on the SX bank, then each chain's lock step. */
static void mt7977_retune(struct prog *p, uint32_t die, uint32_t int_w, uint32_t frac_w)
{
	const uint32_t sx = MT7977_K_SX;

	aw(p, die, sx, ADIE_SX_CMD, ADIE_SX_CMD_PREP);
	armw(p, die, sx, ADIE_STROBE, ADIE_STROBE_SX_PREP, 0);
	armw(p, die, sx, ADIE_STROBE, ADIE_STROBE_SX_PREP, ADIE_STROBE_SX_PREP);
	aw(p, die, sx, ADIE_SX_A04, MT7977_SX_A04_PRE);
	aw(p, die, sx, ADIE_SX_INT, int_w);
	aw(p, die, sx, ADIE_SX_FRAC, frac_w);
	aw(p, die, sx, ADIE_SX_A04, MT7977_SX_A04_SAR);
	aw(p, die, sx, ADIE_SX_B8A, MT7977_SX_B8A_SAR);
	aw(p, die, sx, ADIE_SX_B83, MT7977_SX_B83_SAR);
	aw(p, die, sx, ADIE_SX_A03, MT7977_SX_A03_SAR);
	aw(p, die, sx, ADIE_SX_A05, MT7977_SX_A05_SAR);
	for (uint32_t i = 0; i < NELEM(sar_ramp); i++) {
		armw(p, die, sx, ADIE_SX_BAND, ADIE_SX_BAND_WORD, HW_PREP(ADIE_SX_BAND_WORD, sar_ramp[i]));
		aw(p, die, sx, ADIE_SX_BAND2, HW_PREP(ADIE_SX_BAND2_WORD, sar_ramp[i]));
	}
	put(p, MT7996_PROBE_CTL_OP_SAR, die, 0, 0);
	aw(p, die, sx, ADIE_SX_A05, MT7977_SX_A05_LOCK);
	aw(p, die, sx, ADIE_SX_A03, MT7977_SX_A03_LOCK);
	aw(p, die, sx, ADIE_SX_B83, MT7977_SX_B83_LOCK);
	aw(p, die, sx, ADIE_SX_B8A, MT7977_SX_B8A_LOCK);
	aw(p, die, sx, ADIE_SX_B8B, MT7977_SX_B8B_LOCK);
	aw(p, die, sx, ADIE_SX_CMD, ADIE_SX_CMD_MEASURE);
	lock_wait(p, die, sx);
	aw(p, die, sx, ADIE_SX_CMD, ADIE_SX_CMD_RUN);
	aw(p, die, sx, ADIE_SX_A04, MT7977_SX_A04_RUN);
	for (int k = MT7977_SXCH_WF_LOCK_FIRST; k >= (int)SXCH_P1_MT7977; k--) {
		aw(p, die, k, ADIE_SX_CMD, ADIE_SX_CMD_PREP);
		aw(p, die, k, ADIE_SX_CMD, ADIE_SX_CMD_WF_LOCK);
		lock_wait(p, die, k);
		aw(p, die, k, ADIE_SX_CMD, ADIE_SX_CMD_RUN);
	}
}

/* mt7976.h "SX0 retune": the presets (pre-rows, table A and B at the 5/16 column item 10 selects
 * for SX0), SX0's LO to the chains and its active mark, the words, the lock step. */
static int mt7976_retune(struct prog *p, uint32_t int_w, uint32_t frac_w)
{
	const uint32_t die = MT7976_DIE, sx = MT7976_K_SX;
	const uint32_t key = MT7976_SXCH_KEY(MT7976_SXCH_SEL_SX0, MT7976_SXCH_RATIO_ITEM10);
	uint32_t ca = column(t76_a_keys, NELEM(t76_a_keys), key), cb = column(t76_b_keys, NELEM(t76_b_keys), key);
	uint32_t i;

	if (ca >= NELEM(t76_a_keys) || cb >= NELEM(t76_b_keys))
		return 0;
	for (i = 0; i < NELEM(t76_pre_reg); i++)
		if (t76_pre_val[i] != SXCH_SKIP)
			aw(p, die, sx, t76_pre_reg[i], t76_pre_val[i]);
	for (i = 0; i < NELEM(t76_a_reg); i++)
		if (t76_a_val[i][ca] != SXCH_SKIP)
			aw(p, die, sx, t76_a_reg[i], t76_a_val[i][ca]);
	for (i = 0; i < NELEM(t76_b_reg); i++)
		if (t76_b_val[i][cb] != SXCH_SKIP)
			aw(p, die, sx, t76_b_reg[i], t76_b_val[i][cb]);
	aw(p, die, sx, MT7976_LO_SRC, MT7976_LO_SRC_SX0);
	armw(p, die, sx, MT7976_LO_EN, MT7976_LO_EN_SX, HW_PREP(MT7976_LO_EN_SX, MT7976_LO_EN_SX_ACTIVE));
	aw(p, die, sx, ADIE_SX_INT, int_w);
	aw(p, die, sx, ADIE_SX_FRAC, frac_w);
	aw(p, die, sx, ADIE_SX_B8A, MT7976_SX_B8A_LOCK);
	aw(p, die, sx, ADIE_SX_CMD, ADIE_SX_CMD_PREP);
	aw(p, die, sx, ADIE_SX_CMD, ADIE_SX_CMD_MEASURE);
	lock_wait(p, die, sx);
	aw(p, die, sx, ADIE_SX_CMD, ADIE_SX_CMD_RUN);
	return 1;
}

uint32_t rfctl_tune(uint32_t band, uint64_t hz, struct rfctl_op *ops, uint32_t max, uint64_t *actual_hz,
		    uint32_t *health)
{
	struct prog p = { ops, 0, max };
	uint32_t rem, mhz, khz, int_w, frac_w, ratio58, sx = sx_k(band);
	uint64_t lo_min, lo_max;

	if (band >= RFCTL_BANDS)
		return 0;
	lo_min = (uint64_t)(band == MT7976_DIE ? MT7976_LO_TUNE_MIN_MHZ : MT7977_LO_TUNE_MIN_MHZ) * RFCTL_HZ_PER_MHZ;
	lo_max = (uint64_t)(band == MT7976_DIE ? MT7976_LO_TUNE_MAX_MHZ : MT7977_LO_TUNE_MAX_MHZ) * RFCTL_HZ_PER_MHZ;
	if (hz < lo_min || hz > lo_max)
		return 0;
	mhz = (uint32_t)rfctl_div64(hz, RFCTL_HZ_PER_MHZ, &rem);
	khz = (uint32_t)div_round(hz, RFCTL_HZ_PER_KHZ);
	if (band == MT7976_DIE) {
		if (!divider(hz, mhz, MT7976_RATIO_5_16_NUM, MT7976_RATIO_5_16_DEN, MT7976_PFD_KHZ * RFCTL_HZ_PER_KHZ,
			     &int_w, &frac_w, actual_hz) || !mt7976_retune(&p, int_w, frac_w))
			return 0;
	} else {
		ratio58 = hz < (uint64_t)MT7977_FW_SPLIT_MHZ * RFCTL_HZ_PER_MHZ;
		if (!divider(hz, mhz, ratio58 ? MT7977_RATIO_5_8_NUM : MT7977_RATIO_3_4_NUM,
			     ratio58 ? MT7977_RATIO_5_8_DEN : MT7977_RATIO_3_4_DEN, MT7977_PFD_KHZ * RFCTL_HZ_PER_KHZ,
			     &int_w, &frac_w, actual_hz) || !mt7977_presets(&p, band, mhz, ratio58))
			return 0;
		mt7977_retune(&p, band, int_w, frac_w);
	}
	/* The D-die words that follow the LO, at the band's bandwidth (after a SETUP: radio_band). */
	if (radio_band[band].set)
		freq_words(&p, band, mhz, radio_band[band].bw);
	/* Health: SX status (its [1:0] the lock), Vtune, comparator, then the MT7977's done word or
	 * the MT7976's band word; then the firmware's frequency note on chain bank 0 (LO in kHz). */
	*health = p.n;
	ar(&p, band, sx, ADIE_SX_STATUS);
	ar(&p, band, sx, ADIE_SX_VTMON);
	ar(&p, band, sx, ADIE_SX_CMP);
	ar(&p, band, sx, band == MT7976_DIE ? ADIE_SX_BAND : ADIE_SX_DONE);
	aw(&p, band, ADIE_K_WF(0), band == MT7976_DIE ? ADIE_FREQ_NOTE_MT7976 : ADIE_FREQ_NOTE_MT7977, khz);
	return done(&p);
}

/* ---------------------------------------------------------------------------------------------
 * GAIN, FILTER, TRIM, STATUS, LOCKS, BAND
 * --------------------------------------------------------------------------------------------- */

uint32_t rfctl_gain(uint32_t band, uint32_t lna, uint32_t lpf, struct rfctl_op *ops, uint32_t max)
{
	struct prog p = { ops, 0, max };
	uint32_t word, c;

	if (band >= RFCTL_BANDS)
		return 0;
	if (lna == RFCTL_GAIN_AGC)
		word = MT7996_PHY_RXGAIN_AGC;
	else if (lna <= MT7996_PHY_RXGAIN_LNA_UH && lpf <= MT7996_PHY_RXGAIN_LPF_MAX)
		word = MT7996_PHY_RXGAIN_FORCE_AB(lna, lpf);
	else
		return 0;
	for (c = 0; c < chains[band]; c++)
		w32(&p, MT7996_PHY_RXGAIN(band, c), word);
	for (c = 0; c < chains[band]; c++)
		r32(&p, MT7996_PHY_RXGAIN(band, c));
	return done(&p);
}

uint32_t rfctl_filter_regs(uint32_t band, uint32_t code, struct rfctl_filter *f)
{
	uint32_t cal;

	if (band >= RFCTL_BANDS || code > ADIE_RX_BW_CODE_320MHZ)
		return 0;
	if (band != MT7976_DIE) {
		f->code = code;
		f->r933 = ADIE_CHSET_933_MT7977(code);
		f->ext = f->trim = 0;
		return 1;
	}
	if (!(radio_cal.rc_valid & 1u << band))
		return 0;
	cal = radio_cal.rc_code[band];
	f->r933 = ADIE_CHSET_933_MT7976;
	if (code <= MT7976_RX_BW_CODE_MAX) {
		f->code = code;
		f->ext = MT7976_RXF_125_EXT_STOCK;
		f->trim = cal;
	} else {
		f->code = MT7976_RX_BW_CODE_MAX;
		f->ext = MT7976_RXF_125_EXT_WIDE;
		/* Lowered from the calibrated code, but never into the codes at which the
		 * widened filter oscillates (mt7976.h MT7976_RXF_WIDE_TRIM_MIN), and never
		 * raised: a die calibrated under the floor keeps its own code. */
		f->trim = cal > MT7976_RXF_WIDE_TRIM_DROP ? cal - MT7976_RXF_WIDE_TRIM_DROP : 0;
		if (f->trim < MT7976_RXF_WIDE_TRIM_MIN)
			f->trim = cal < MT7976_RXF_WIDE_TRIM_MIN ? cal : MT7976_RXF_WIDE_TRIM_MIN;
	}
	return 1;
}

uint32_t rfctl_filter(uint32_t band, uint32_t mhz, struct rfctl_op *ops, uint32_t max)
{
	struct prog p = { ops, 0, max };
	struct rfctl_filter f;
	uint32_t code, k;

	for (code = ADIE_RX_BW_CODE_20MHZ; code <= ADIE_RX_BW_CODE_320MHZ; code++)
		if (mhz == RFCTL_FILTER_MIN_MHZ << code)
			break;
	if (!rfctl_filter_regs(band, code, &f))
		return 0;
	for (k = 0; k < chains[band]; k++)
		aw(&p, band, ADIE_K_WF(k), ADIE_RX_BW, f.code);
	for (k = 0; k < chains[band]; k++)
		aw(&p, band, ADIE_K_WF(k), ADIE_CHSET_933, f.r933);
	if (band == MT7976_DIE) {
		for (k = 0; k < chains[band]; k++)
			armw(&p, band, ADIE_K_WF(k), MT7976_RXF_125, MT7976_RXF_125_EXT,
			     HW_PREP(MT7976_RXF_125_EXT, f.ext));
		for (k = 0; k < chains[band]; k++)
			armw(&p, band, ADIE_K_WF(k), ADIE_RCTRIM, ADIE_RCTRIM_CODE | ADIE_RCTRIM_SW,
			     HW_PREP(ADIE_RCTRIM_CODE, f.trim) | ADIE_RCTRIM_SW);
	}
	for (k = 0; k < chains[band]; k++)
		ar(&p, band, ADIE_K_WF(k), ADIE_RX_BW_RB);
	return done(&p);
}

uint32_t rfctl_trim(uint32_t band, uint32_t code, struct rfctl_op *ops, uint32_t max)
{
	struct prog p = { ops, 0, max };

	if (band >= RFCTL_BANDS || code > ADIE_RCTRIM_CODE_MAX)
		return 0;
	for (uint32_t k = 0; k < chains[band]; k++)
		armw(&p, band, ADIE_K_WF(k), ADIE_RCTRIM, ADIE_RCTRIM_CODE | ADIE_RCTRIM_SW,
		     HW_PREP(ADIE_RCTRIM_CODE, code) | ADIE_RCTRIM_SW);
	return done(&p);
}

uint32_t rfctl_status(uint32_t band, struct rfctl_op *ops, uint32_t max)
{
	struct prog p = { ops, 0, max };
	uint32_t sx = sx_k(band), k;

	if (band >= RFCTL_BANDS)
		return 0;
	ar(&p, band, sx, ADIE_SX_INT);
	ar(&p, band, sx, ADIE_SX_FRAC);
	ar(&p, band, sx, ADIE_SX_CFG);
	ar(&p, band, sx, ADIE_SX_STATUS);
	ar(&p, band, ADIE_K_WF(0), band == MT7976_DIE ? ADIE_FREQ_NOTE_MT7976 : ADIE_FREQ_NOTE_MT7977);
	for (k = 0; k < chains[band]; k++)
		ar(&p, band, ADIE_K_WF(k), ADIE_RX_BW);
	for (k = 0; k < chains[band]; k++)
		r32(&p, MT7996_PHY_RXGAIN(band, k));
	return done(&p);
}

uint32_t rfctl_locks(struct rfctl_op *ops, uint32_t max)
{
	struct prog p = { ops, 0, max };

	for (uint32_t band = 0; band < RFCTL_BANDS; band++)
		ar(&p, band, sx_k(band), ADIE_SX_STATUS);
	return done(&p);
}

/* ICAP0 is the stream's capture engine; every instance gets the band. */
uint32_t rfctl_band(uint32_t band, struct rfctl_op *ops, uint32_t max)
{
	struct prog p = { ops, 0, max };

	if (band >= RFCTL_BANDS)
		return 0;
	r32(&p, MT7996_ICAP_CR9(0));
	for (uint32_t n = 0; n < MT7996_ICAP_NUM; n++)
		w32(&p, MT7996_ICAP_CR8(n), HW_PREP(MT7996_ICAP_CR8_BAND, band));
	r32(&p, MT7996_ICAP_CR9(0));
	return done(&p);
}

/* ---------------------------------------------------------------------------------------------
 * The interpreter's entry (published at MT7996_PROBE_OWN_WM_RF_BUILD)
 * --------------------------------------------------------------------------------------------- */

static struct rfctl_op rfctl_buf[RFCTL_MAX_OPS];

uint32_t rfctl_build(uint32_t a, uint32_t b, uint32_t c, struct rfctl_prog *p)
{
	uint32_t band = HW_GET(MT7996_PROBE_CTL_RF_A_BAND, a), n, out = 0, check = RFCTL_NO_CHECK;
	uint64_t lo = 0;

	switch (HW_GET(MT7996_PROBE_CTL_RF_A_CMD, a)) {
	case MT7996_PROBE_CTL_RF_TUNE:
		n = rfctl_tune(band, (uint64_t)c << 32 | b, rfctl_buf, RFCTL_MAX_OPS, &lo, &out);
		check = out;
		if (n)
			radio_band[band].lo_hz = lo;	/* the band's LO, for RATE and SETUP's state */
		break;
	case MT7996_PROBE_CTL_RF_GAIN:
		n = rfctl_gain(band, b, c, rfctl_buf, RFCTL_MAX_OPS);
		out = n / 2u;			/* the read-backs */
		break;
	case MT7996_PROBE_CTL_RF_FILTER:
		n = rfctl_filter(band, b, rfctl_buf, RFCTL_MAX_OPS);
		out = n ? n - chains[band] : 0;	/* the read-backs */
		break;
	case MT7996_PROBE_CTL_RF_TRIM:
		n = rfctl_trim(band, b, rfctl_buf, RFCTL_MAX_OPS);
		break;
	case MT7996_PROBE_CTL_RF_STATUS:
		n = rfctl_status(band, rfctl_buf, RFCTL_MAX_OPS);
		break;
	case MT7996_PROBE_CTL_RF_LOCKS:
		n = rfctl_locks(rfctl_buf, RFCTL_MAX_OPS);
		break;
	case MT7996_PROBE_CTL_RF_BAND:
		n = rfctl_band(band, rfctl_buf, RFCTL_MAX_OPS);
		break;
	default:
		n = 0;
	}
	if (!n || n - out > MT7996_PROBE_CTL_RF_OUT_MAX)
		return MT7996_PROBE_CTL_BAD_ARG;
	p->ops = rfctl_buf;
	p->count = n;
	p->out = out;
	p->check = check;
	p->lo = (uint32_t)lo;
	p->lo_hi = (uint32_t)(lo >> 32);
	return MT7996_PROBE_CTL_OK;
}
