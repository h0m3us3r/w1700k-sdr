/* SPDX-License-Identifier: MIT */
/* RF control while streaming, as firmware commands: the register programs our WM runs for one
 * MT7996_PROBE_CTL_OP_RF op (hw/mt7996/probe_page.h "RF commands"), so the host names no radio,
 * PHY or A-die register in regular operation.
 *
 * Each builder writes raw control ops ({op, a, b, c} as posted to the CTL mailbox, an A-die
 * register as MT7996_PROBE_CTL_AREG(die, k, index)) into the caller's buffer: op for op the
 * program host/w1700k/rf.py built on the host before (mt7977_retune / mt7976_retune, set_gain,
 * set_filter, set_trim, status, synth_locks, select_band_program), except for the one change under
 * rfctl_tune. The control interpreter (stream/ctl.h) runs the program inside the RF op exactly as
 * it runs a posted one: DELAY, an unmet APOLL and the SAR's settling return and resume on its next
 * call, so a retune costs a running stream only the time its register accesses take. Nothing here
 * touches hardware or keeps state between builds (rfctl_build's buffer aside).
 *
 * Bands: band b is A-die die b. Band 0: the MT7976, LO 2.08..2.74 GHz, SX0 in k 0, 4 chains;
 * bands 1 and 2: MT7977, LO 4.65..7.41 GHz, SX in k 4, 4 and 5 chains.
 *
 * A builder returns the number of ops written; 0 when it refuses its arguments or the program does
 * not fit `max` (RFCTL_MAX_OPS fits every program).
 *
 * rfctl_tune(band, hz)     The LO of band to hz (Hz). MT7977: ratio 5/8 below
 *     MT7977_FW_SPLIT_MHZ, 3/4 from it; the SX80_E3 presets of the frequency's Group (mt7977.h
 *     MT7977_SX_E3_ROWS bounds; below the first Group the first, above the last the last, clamped
 *     to the ratio's Groups) on the SX bank, the RCD rows of its range on k 4..0, then mt7977.h
 *     "Retune sequence" steps 1..9 with the SAR as one op. MT7976: mt7976.h "SX0 retune" with
 *     its presets at ratio 5/16. Then four health reads and the firmware's frequency note (LO in
 *     kHz, rounded half to even like rf.py's round()). *health: the index of the first health
 *     read, the SX status (its [1:0] != 0: the VCO did not lock), then Vtune, the comparator and
 *     the done word (MT7977) or band word (MT7976). *actual_hz: the LO the words give, rounded
 *     to the nearest Hz (ties to even, rf.py's round()).
 *     The presets are always written. rf.py first read the six bin-dependent preset registers in
 *     a separate program and left the 45 preset writes out when they already held the target's
 *     column (_presets_needed): a second round trip, and a guess that no one else wrote them.
 *     Writing them always costs a retune within one bin the presets' SPI time (sx.h: 1.5 ms of WM
 *     time instead of 0.8 ms); the stream does not notice either.
 * rfctl_hold(band, hz)     The LO of band to hz by the frequency words alone (INT, then FRAC with
 *     its load bit), the lock kept: no presets, no VCO search, no lock step, so the phases of the
 *     chains' LOs against each other stay as the last rfctl_tune left them (probe_page.h HOLD).
 *     0 unless hz is within the band's reach (probe_page.h RF_HOLD_REACH) of the LO it last locked
 *     at and, on an MT7977, is on that LO's side of the ratio split. Then the D-die words
 *     that follow the LO, the settling time, and rfctl_tune's health reads and frequency note.
 * rfctl_rephase(band, chain)  The chain's block enables under software control (ADIE_CHAIN_OVR),
 *     receiving, off, and back to the D-die's control: its LO path starts again, in either of
 *     its two phases against the other chains (probe_page.h REPHASE).
 * rfctl_gain(band, lna, lpf)  Every chain's RXGAIN forced to (lna 0..4, lpf 0..9) in sets A
 *     and B (phy.h MT7996_PHY_RXGAIN_FORCE_AB), or handed to the AGC with lna = RFCTL_GAIN_AGC;
 *     then every chain's word read back.
 * rfctl_filter(band, mhz)  Every chain's analog filter to mhz (20, 40, 80, 160, 320) at whatever
 *     bandwidth the channel has: rfctl_filter_regs' ADIE_RX_BW and ADIE_CHSET_933, on the MT7976
 *     also MT7976_RXF_125 [22:20] and the RC trim; then every chain's ADIE_RX_BW_RB read (on band
 *     0 at 320 MHz it reads 3).
 * rfctl_trim(band, code)   Every chain's ADIE_RCTRIM code (0..31) with ADIE_RCTRIM_SW set.
 * rfctl_status(band)       SX INT, FRAC, CFG and status, the frequency note, every chain's
 *     ADIE_RX_BW, then every chain's RXGAIN.
 * rfctl_locks()            The SX status of bands 0, 1 and 2, then the tuning-voltage reading of each.
 * rfctl_band(band)         ICAPCR8 of the three ICAPs = band (the band the stream captures),
 *     between two reads of ICAP0's write pointer CR9 that place the switch in the ring.
 */
#ifndef RADIO_RFCTL_H
#define RADIO_RFCTL_H

#include <stdint.h>

#include "hw/adie/adie.h"
#include "hw/mt7996/probe_page.h"

/* One control op, as in the CTL mailbox (MT7996_PROBE_CTL_OP(i)). */
struct rfctl_op {
	uint32_t op, a, b, c;
};

#define RFCTL_BANDS		ADIE_NUM_DIES	/* band b is die b */
/* The longest program, an MT7977 tune: 45 preset, 50 retune, 7 frequency-word and 5 health / note
 * ops. */
#define RFCTL_MAX_OPS		112u
#define RFCTL_GAIN_AGC		MT7996_PROBE_CTL_RF_GAIN_AGC
#define RFCTL_UNLOCKED		MT7996_PROBE_CTL_RF_UNLOCKED
/* Units of the arguments. */
#define RFCTL_HZ_PER_KHZ	1000u
#define RFCTL_HZ_PER_MHZ	1000000u
/* The narrowest RX bandwidth class, ADIE_RX_BW_CODE_20MHZ; each code up doubles it (adie.h). */
#define RFCTL_FILTER_MIN_MHZ	20u

/* A band's analog RX filter as A-die values, written by both the channel set and rfctl_filter
 * (code: ADIE_RX_BW_CODE_*, 20..320 MHz, independent of the band's bandwidth). MT7977: ADIE_RX_BW
 * = code, ADIE_CHSET_933 by code (MediaTek writes it with every code). MT7976 (no 320 MHz code,
 * mt7976.h "RX analog filter"): ADIE_RX_BW = min(code, 3), ADIE_CHSET_933 = 2, MT7976_RXF_125
 * [22:20] widened at 320 MHz, else MediaTek's, and the RC trim from the die's calibrated code
 * (radio_cal), lowered at 320 MHz. 0 when refused (a code above 320 MHz, a band, or the MT7976
 * before the bring-up measured its RC code), else 1. */
struct rfctl_filter {
	uint32_t code, r933;		/* ADIE_RX_BW, ADIE_CHSET_933 */
	uint32_t ext, trim;		/* MT7976: MT7976_RXF_125 [22:20], ADIE_RCTRIM code */
};
uint32_t rfctl_filter_regs(uint32_t band, uint32_t code, struct rfctl_filter *f);

/* MediaTek's frequency words (chanset.c R4): num / mhz, plus 1 when the next decimal digit is
 * above 4. */
uint32_t rfctl_freq_word(uint32_t num, uint32_t mhz);
/* n / d and n mod d without the compiler's 64-bit division (a freestanding build has no libgcc). */
uint64_t rfctl_div64(uint64_t n, uint32_t d, uint32_t *rem);
/* Runs a built program synchronously in our WM (rfrun.c): RADIO_OK, or RADIO_ERR_HW with the HAL's
 * failure record; res[i] = op i's result. */
uint32_t rfctl_run(const struct rfctl_op *ops, uint32_t n, uint32_t *res);

uint32_t rfctl_tune(uint32_t band, uint64_t hz, struct rfctl_op *ops, uint32_t max, uint64_t *actual_hz,
		    uint32_t *health);
uint32_t rfctl_hold(uint32_t band, uint64_t hz, struct rfctl_op *ops, uint32_t max, uint64_t *actual_hz,
		    uint32_t *health);
uint32_t rfctl_rephase(uint32_t band, uint32_t chain, struct rfctl_op *ops, uint32_t max);
uint32_t rfctl_gain(uint32_t band, uint32_t lna, uint32_t lpf, struct rfctl_op *ops, uint32_t max);
uint32_t rfctl_filter(uint32_t band, uint32_t mhz, struct rfctl_op *ops, uint32_t max);
uint32_t rfctl_trim(uint32_t band, uint32_t code, struct rfctl_op *ops, uint32_t max);
uint32_t rfctl_status(uint32_t band, struct rfctl_op *ops, uint32_t max);
uint32_t rfctl_locks(struct rfctl_op *ops, uint32_t max);
uint32_t rfctl_band(uint32_t band, struct rfctl_op *ops, uint32_t max);

/* What the interpreter gets for one RF op (probe_page.h "RF commands"): the program in the
 * builder's own buffer (valid until the next build), the first op whose result is reported (the
 * RF block's OUT words are the results of ops [out, count)), the op whose result [1:0] != 0 makes
 * the outcome RFCTL_UNLOCKED (RFCTL_NO_CHECK: none), and a tune's actual LO in Hz. */
struct rfctl_prog {
	const struct rfctl_op *ops;
	uint32_t count, out, check;
	uint32_t lo, lo_hi;	/* Hz [31:0], [63:32] */
};
#define RFCTL_NO_CHECK		0xffffffffu

/* Builds the program of the RF op {MT7996_PROBE_CTL_OP_RF, a, b, c} into a buffer of our WM's;
 * MT7996_PROBE_CTL_OK, or MT7996_PROBE_CTL_BAD_ARG (unknown command, refused arguments). Our WM
 * publishes its address at MT7996_PROBE_OWN_WM_RF_BUILD: the interpreter of the separately linked
 * stream producer calls it there, as our WM's own main loop does. */
typedef uint32_t (*rfctl_build_fn)(uint32_t a, uint32_t b, uint32_t c, struct rfctl_prog *p);
uint32_t rfctl_build(uint32_t a, uint32_t b, uint32_t c, struct rfctl_prog *p);

#endif
