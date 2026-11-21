/* SPDX-License-Identifier: MIT */
/*
 * MT7976 (MT7976GN) A-die: die 0, band 0, the 2.4 GHz receiver.
 * Chip ID 0x79768a20 at TOP 0x02c.
 *
 * ADDRESS SPACE: 12-bit A-die register indices on SPI master 0 (adie.h);
 * banks k 0..3 = chains WF0..WF3, k 4 = WF4 (the DFS/SM monitor receiver),
 * k 15 = TOP. The die answers on k 0..4. [measured]
 *
 * SYNTHESIZERS (register family in sx.h):
 *   SX0, 2.4 GHz, in k 0; drives WF0..3. f_PFD 80 MHz. Two LO ratios, chosen
 *   together with the VCO core by one bit, 0xa40[2]:
 *     5/16, high core: VCO 6.66..8.78 GHz -> LO 2.08..2.74 GHz  [measured]
 *     3/8,  low core:  VCO 5.86..7.14 GHz -> LO 2.20..2.68 GHz  [measured]
 *   The band search runs inside the lock step (no software SAR), and the
 *   presets do not depend on frequency, so a retune anywhere in range is the
 *   frequency words, 0xb8a and the lock step. [static][measured]
 *   5 GHz SX in k 4: dormant on this board (0xca4 = 0x00010000) until
 *   k 4 0x021[31:28] = 0xf; it then locks VCO 7.4..10.8 GHz by its flags,
 *   but it only drives the chains' 5 GHz path, which the MT7976GN does not
 *   bring out: it reaches no antenna. [measured]
 *   TTG PLL in k 4 (sx.h): the monitor receiver's LO.
 *
 * Reception measured 2.1..2.7 GHz at -17..-40 dBFS for a fixed reference.
 * The LO is an SSB network (5/16 = VCO/4 + VCO/16, inferred); the rejected
 * sideband at 3/16 of the VCO is a weak double-sided image, about -75 dBFS
 * for a full-power local source (1.25..1.65 GHz over the high core), 44..52 dB
 * under the main response. No register setting moves the LO to 1090 MHz or
 * adds another ratio; a spur map shows only the main ratio and that image.
 * [measured]
 *
 * HAZARD: a radio reload or a firmware channel switch transmits a full-scale
 * burst at the antennas (+1 dBFS on a nearby receiver at 41..44 dB over
 * quiet); register programs written at run time never do. Disconnect any
 * receiver cabled to an antenna port before a reload. [measured]
 *
 * Confidence tags: [measured], [static], [inferred].
 */
#ifndef HW_MT7976_ADIE_H
#define HW_MT7976_ADIE_H

#include "../hw_bits.h"
#include "adie.h"
#include "sx.h"

#define MT7976_DIE                      0u
#define MT7976_K_SX                     0u	/* 2.4 GHz SX0 */
#define MT7976_K_SX5G                   4u	/* dormant 5 GHz SX */
#define MT7976_K_MON                    4u	/* DFS/SM monitor receiver, TTG */
#define MT7976_NUM_CHAINS               4u	/* WF0..3 receive; WF4 = monitor */

/* ------------------------------------------------------------------------
 * RX analog filter                                              [measured]
 *
 * Set by ADIE_RX_BW (adie.h) codes 0..3; the die decodes [1:0], so code 4
 * filters as code 0: the code word has no 320 MHz setting. The per-code stage
 * settings are fixed logic in the die (ADIE_BWLUT_* are read-only mirrors).
 * Two more controls set the corner: the RC trim (ADIE_RCTRIM) and the
 * chain word MT7976_RXF_125 [22:20] below, which together give a 320 MHz-class
 * filter at code 3. The R-cal code (ADIE_RCAL) does not move it. Found by a
 * single-bit sweep of every writable bit of chain bank 0 (437 words, 10,041
 * bits; a tone 130 MHz off the LO as witness), stopped at 0x135 by the
 * MT7976_RXF_12F hazard; 0x136..0xfff are not swept yet.
 * Response at 320 Msps, die 0 on band 0 at BW160 (2026-10-11): gain forced
 * LNA 2 / LPF 5, a B205 tone fixed at 2442 MHz and the LO stepped, dB
 * relative to 5 MHz off the LO, symmetric to within 0.5 dB:
 *   off (MHz)      10    20    40    60    80   100   120   140   150
 *   code 0       -2.2 -10.1 -23.0 -32.0 -40.2 -48.2 -57.0 -68.3 -73.3
 *   code 1       -0.5  -1.3  -8.6 -16.3 -23.4 -30.8 -37.7 -50.5 -57.6
 *   code 2       -0.2  -0.7  -2.6  -7.1 -12.1 -19.2 -27.7 -38.2 -45.0
 *   code 3       -0.3  -0.2  -0.7  -2.0  -5.4 -10.7 -18.0 -28.2 -34.7
 *   code 3, trim 0     .   0.1  -0.1  -2.2  -5.7 -11.7 -20.5 -26.7
 *   code 3, trim 31    .  -1.6  -4.1  -8.4 -14.6 -22.6 -33.0 -39.8
 * (trim 17, the calibrated code, unless stated; the 160 MHz class's digital
 * path alone is -2.2 dB at 100 MHz and -6.8 at 120, dfe.h "Response").
 * -3 dB corners: about +-11 / +-27 / +-45 / +-68 MHz for codes 0..3; +-85 MHz
 * at code 3 with trim 0 (+-55 with trim 31). Tones beyond +-160 MHz alias
 * in at least 58 dB down at code 3.
 * The filter is the receiver's only anti-alias filter: the converter samples
 * at 320 MHz, so whatever passes the filter 320 MHz from the LO folds onto
 * the band. At 160 Msps (band 0, BW80, LNA 2 / LPF 5, 2026-11-12; a tone at
 * 2435 MHz from a HackRF in a shielded box, the LO on it and then 320 MHz
 * either side; a ratio at one tone frequency, so the path between the radios
 * drops out), the folded tone against the tone itself, by the host's filter
 * setting (rf.py FILTER_CODE), LO below / above the tone:
 *   320 MHz  -19.7 / -18.4 dB     160 MHz  -33.2 / -32.1     80 MHz  -45.3 / -44.4
 *    40 MHz  -65.5 / -65.5        20 MHz   -78.1 / -76.3
 * (at 2420..2480 MHz, 320 MHz setting: -17.8 .. -20.2). With the filter as
 * wide as the sample rate, as the host sets it by default: -33.8 / -32.1 at
 * 160 Msps, -45.6 / -43.8 at 80 Msps, -65.6 / -65.2 at 40 Msps; the folds at
 * 80 and 160 MHz at or under -56 dB at each. The response 160 MHz
 * from the LO (the decimation's own fold) is -42 dB at the 320 MHz setting
 * and at or under -56 dB, the measurement's floor, at the others. So a strong
 * band 320 MHz from the view shows in it: the 2.4 GHz band at 2720..2804 and
 * 2080..2164 MHz. [measured]
 * ------------------------------------------------------------------------ */
#define MT7976_RX_BW_CODE_MAX           ADIE_RX_BW_CODE_160MHZ

/* 0x125  Chain word (k 0..3). RW. Power-on and after our bring-up 0x000129df
 * (MediaTek's RF_INIT does not write it). [22:20] widens the RX filter at
 * code 3 and, above 3, makes it resonate [name guess: LPF bandwidth
 * extension / feedback compensation]. Response at 320 Msps (band 0 BW160,
 * set as in "RX analog filter" above), dB relative to 5 MHz off the LO:
 *   [22:20], trim  off  80   100   120   140   150   160  170 (alias)
 *     0, 17           -5.1 -10.3 -17.8 -27.9 -34.4    .  -57.5
 *     0,  0           -2.4  -5.9 -11.5 -20.1 -26.5 -34.9 -49.0
 *     1, 17           -6.0 -10.3 -16.7 -25.7 -31.7    .  -54.0
 *     2, 17           -0.8  -3.4  -8.6 -17.3 -23.8 -32.8 -47.3
 *     2,  8           -0.8  -2.9  -6.5 -11.9 -15.7 -21.6 -33.7
 *     2,  0           -0.3  -2.4  -5.0  -7.7  -6.2  -1..-7 -33  (peaks
 *                     at the band edge, 5 dB lost at 5 MHz: resonant)
 *     3,  0           -0.2  -2.2  -5.2  -8.8 -10.6  -7.9 -22.4
 *     4..7, 17        peaks of +7..+29 dB at 80..120 MHz, clipping
 * The MT7977's own 320 MHz code on band 1, for comparison: +0.6 / -2.2 /
 * -6.8 / -14.1 / -19.3 at 80 / 100 / 120 / 140 / 150 MHz, alias -40.7.
 * [22:20] = 2 with the trim 9 codes under the calibrated one (8 on unit A) is
 * the widest setting without peaking on that die: MT7976_RXF_WIDE_*, which
 * also keep the trim from going lower than 8 on any die (below). [0] .. [15] and
 * [17..19], [23..26], [28..31] single flips: no visible effect; [16] clear
 * -4 dB at 130 MHz; [27] set mutes. [measured] */
#define MT7976_RXF_125                  0x125u
#define MT7976_RXF_125_EXT              HW_GENMASK(22, 20)
#define MT7976_RXF_125_EXT_STOCK        0u
#define MT7976_RXF_125_EXT_WIDE         2u
#define MT7976_RXF_125_MUTE             HW_BIT(27)
/* The 320 MHz-class filter: code 3, [22:20] = 2, RC trim lowered from the
 * calibrated code by MT7976_RXF_WIDE_TRIM_DROP steps, but not under
 * MT7976_RXF_WIDE_TRIM_MIN (nor raised over the calibrated code).
 *
 * The floor is there because the widened filter does not only peak at a low
 * trim, it oscillates, and where that begins goes by the trim code itself and
 * not by its distance from the calibrated one. Two dies:
 *   unit A (W1700K), calibrated 17: clean at 8 (17 - 9); resonant at 0
 *   a Velo7 Max, calibrated 13:     oscillates at 0, 2 and 4 (13 - 9);
 *                                   clean at 5, 6, 8, 10, 12, 14 and 17
 * Oscillating, the filter puts a line 0.16 MHz wide at +-143..148 MHz from
 * the LO, wandering, 52..67 dB over the floor and about -20 dBFS at LNA 2 /
 * LPF 5. It is no signal from the antenna: it keeps its offset when the LO
 * moves, keeps its level from LNA 0 to 4, and is gone at code 3 unwidened;
 * and the AGC, which sees it, holds the baseband gain at its lowest. A drop of
 * 9 alone put the second die at 4. With the floor at 8 the first die is set
 * as before and the second three codes clear of the highest that oscillated.
 * [measured: 2026-11-21, 320 Msps at 2442 MHz, the trim stepped by hand] */
#define MT7976_RXF_WIDE_TRIM_DROP       9u
#define MT7976_RXF_WIDE_TRIM_MIN        8u

/* 0x12f  Chain word (k 0..3); RF_INIT writes it on k 4 only (0xffc04622).
 * Chain 0 at power-on 0xff2a5324. [27]: spectrum mirror (rfmodes.h
 * ADIE_RX_12F_MIRROR). [25], [26], [29], [30] cleared: -35 dB while cleared.
 * HAZARD: clearing [31] on chain 0 takes the chain out of receive (-38 dB,
 * 0x080 then mirrors chain word 0x013 instead of 0x015), and restoring the
 * bit does not bring it back; the next load's DCOC then timed out at
 * ADIE_DCOC_DONE once, and a second load (after one more [31] pulse)
 * recovered. Never sweep [31]. [measured] */
#define MT7976_RXF_12F                  0x12fu
#define MT7976_RXF_12F_RX_HOLD          HW_BIT(31)	/* [name guess] */

/* ------------------------------------------------------------------------
 * Frequency plan
 * ------------------------------------------------------------------------ */
#define MT7976_PFD_KHZ                  80000u	/* no doubler [static] */
/* f_LO = f_VCO * NUM / DEN */
#define MT7976_RATIO_5_16_NUM           5u
#define MT7976_RATIO_5_16_DEN           16u
#define MT7976_RATIO_3_8_NUM            3u
#define MT7976_RATIO_3_8_DEN            8u
/* 5 GHz SX ratios, as on the MT7977 (5/8: 4915..5925 MHz, 3/4 otherwise). */
#define MT7976_SX5G_RATIO_5_8_NUM       5u
#define MT7976_SX5G_RATIO_5_8_DEN       8u
#define MT7976_SX5G_RATIO_3_4_NUM       3u
#define MT7976_SX5G_RATIO_3_4_DEN       4u

/* Lock ranges, 20..25 MHz steps against the lock flag. [measured] */
#define MT7976_HI_VCO_MIN_MHZ           6660u
#define MT7976_HI_VCO_MAX_MHZ           8780u
#define MT7976_HI_LO_MIN_MHZ            2080u
#define MT7976_HI_LO_MAX_MHZ            2740u
/* TUNE's reach (5/16, high core; firmware/radio/rfctl.c): beyond the die's lock edges, which the
 * host finds by the lock flag at load (host/w1700k/controller.py Controller.lo_lock). Unit A,
 * 2026-10-11, to 0.1 MHz: 2080.70..2748.05 MHz (VCO 6.66..8.79 GHz), a B205 tone at +20 MHz
 * confirming the LO at both edges. [measured] */
#define MT7976_LO_TUNE_MIN_MHZ          2070u
#define MT7976_LO_TUNE_MAX_MHZ          2750u
#define MT7976_LO_VCO_MIN_MHZ           5860u
#define MT7976_LO_VCO_MAX_MHZ           7140u
#define MT7976_LO_LO_MIN_MHZ            2200u
#define MT7976_LO_LO_MAX_MHZ            2680u
#define MT7976_SX5G_VCO_MIN_MHZ         7400u	/* flags only, not tone-checked */
#define MT7976_SX5G_VCO_MAX_MHZ         10800u
/* Firmware use: channels 1..14 = 2412..2484 MHz (VCO 7718..7949 MHz at
 * 5/16); the DPD calibration (phy_cal.h T2{0..3}) also retunes to F - 20 MHz
 * in 3/8 and at its exit back to F, still in 3/8 (e.g. 2442 MHz: 0xa40 =
 * 0x11, 0xa4b = 0x05104c51, 0xa4a = 0x80666666), and leaves the die there
 * until the next calibration prelude retunes. Resolution about 1.5 Hz at
 * the LO. [static][measured] */
#define MT7976_FW_LO_MIN_MHZ            2412u
#define MT7976_FW_LO_MAX_MHZ            2484u

/* Worked words (SX0, 5/16, 2442 MHz, VCO 7814.4 MHz): the firmware writes
 * these (it floors FRAC; rounding gives 0x80ae147b). [static][measured] */
#define MT7976_SX_INT_2442MHZ           0x06105b94u
#define MT7976_SX_FRAC_2442MHZ          0x80ae147au

/* ------------------------------------------------------------------------
 * SX0 mode registers (bank k 0; the 5 GHz SX in k 4 has the same layout)
 * ------------------------------------------------------------------------ */

/* 0xa40  Ratio and VCO core. RW. Observed 0x15. [static][measured]
 * [2] CORE: 1 = 5/16 on the high core, 0 = 3/8 on the low core. This one bit
 * swaps both the ratio and the lock range whatever the rest of the preset
 * column holds. [0], [4], [5]: no effect. No other bit changes the ratio or
 * core. */
#define MT7976_SX_CFG                   ADIE_SX_CFG
#define MT7976_SX_CFG_CORE              HW_BIT(2)
#define MT7976_SX_CFG_5_16              0x00000015u
#define MT7976_SX_CFG_3_8               0x00000011u

/* 0xa55  SSB balance trim. RW. Preset 0x4200 (SX0), 0x4100 (5 GHz SX).
 * [9:8] does not move the LO; it sets the 3/16 image level: 2 -> -69.4 dBFS,
 * 0/1/3 -> -61.5 dBFS (main line -17.7 dBFS). [14] always 1. [measured] */
#define MT7976_SX_SSB                   ADIE_SX_A55
#define MT7976_SX_SSB_TRIM              HW_GENMASK(9, 8)
#define MT7976_SX_SSB_TRIM_SX0          2u

/* 0x133  SDM split d in [1:0]: keep 0 (the frequency words assume it).
 * Preset 0xf00113a4. Writing [1:0] breaks the lock. [static][measured] */
#define MT7976_SX_SPLIT                 ADIE_SX_133
#define MT7976_SX_SPLIT_D               HW_GENMASK(1, 0)

/* 0xb84  [31:24]: writing it breaks the lock. Preset 0x784a8174 (5/16) /
 * 0x784a8143 (3/8). [measured][static] */
#define MT7976_SX_B84_BREAK             HW_GENMASK(31, 24)

/* 0xa08  [9:8] = 3 in the 2.4 GHz block, 0 in the 5 GHz block (preset).
 * [18] set: both VCO edges move down by 24..48 MHz of VCO (< 10 MHz of LO).
 * [static][measured] */
#define MT7976_SX_A08_BLOCK             HW_GENMASK(9, 8)
#define MT7976_SX_A08_EDGE              HW_BIT(18)
/* 0xa5b [8] set: same edge shift as 0xa08[18]. [measured] */
#define MT7976_SX_A5B_EDGE              HW_BIT(8)

/* 0xb80  HAZARD: in SX0's bank, writing back the value just read to any of
 * 0xb80..0xb8f does NOT restore the PLL: the baseline stopped locking until
 * the next radio reload. Re-check the lock every 16 registers when sweeping.
 * [14], [29], [30] set: every target reads "locked" (detector fault, not a
 * lock). [measured] */
#define MT7976_SX_NORESTORE_FIRST       0xb80u
#define MT7976_SX_NORESTORE_LAST        0xb8fu
#define MT7976_SX_B80_FALSE_LOCK        (HW_BIT(14) | HW_BIT(29) | HW_BIT(30))

/* 0xb8a  Loop word before the lock step. [static][measured] */
#define MT7976_SX_B8A_LOCK              0x53295647u	/* after a word rewrite */
#define MT7976_SX_B8A_KEEP              0x008629c6u	/* rewrite skipped */
#define MT7976_SX_B8A_WF                0x0000d872u	/* per-chain lock step, every channel set (k 1..P[1]) */

/* SX1: a second 2.4 GHz instance, registers +0xa0 (INT 0xaeb, FRAC 0xaea),
 * routed with 0x002 = 0x03300000. Not used on this board. [static] */
#define MT7976_SX1_INT                  0xaebu
#define MT7976_SX1_FRAC                 0xaeau

/* ------------------------------------------------------------------------
 * SX presets. Table A: pre-rows, then one column; table B: 0xa4e.
 * Written to the SX's own bank (k 0 for SX0, k 4 for the 5 GHz SX).
 * Live die-0 k 0 matches pre-rows + column 5/16 + table B. [static][measured]
 * ------------------------------------------------------------------------ */
/* X(reg, value) */
#define MT7976_SX_PRE_ROWS(X)           \
	X(0xb88u, 0x14051a67u)          \
	X(0xa51u, 0x00060300u)          \
	X(0xa68u, 0xffffffffu)          \
	X(0xa69u, 0xffffffffu)          \
	X(0xa6au, 0xffffffffu)          \
	X(0xa6bu, 0x0000ffffu)          \
	X(0xa48u, 0x02030000u)          \
	X(0xa57u, 0x00000000u)          \
	X(0x133u, 0xf00113a4u)
/* X(reg, sx0_3_8, sx0_5_16, sx5g_3_4, sx5g_5_8); firmware row order. */
#define MT7976_SX_TABLE_A(X)                                            \
	X(0xa40u, 0x00000011u, 0x00000015u, 0x00000011u, 0x00000015u)   \
	X(0xa52u, 0x00190103u, 0x001b0103u, 0x00190103u, 0x001b0103u)   \
	X(0xa5bu, 0x2e00030cu, 0x2e00020au, 0x2e00030cu, 0x2e00020au)   \
	X(0xb84u, 0x784a8143u, 0x784a8174u, 0x784a8143u, 0x784a8174u)   \
	X(0xa1bu, 0x3000020au, 0x2800020au, 0x3000020au, 0x2800020au)   \
	X(0xa55u, 0x00004200u, 0x00004200u, 0x00004100u, 0x00004100u)   \
	X(0xa08u, 0x12200300u, 0x12200300u, 0x12200000u, 0x12200000u)   \
	X(0xa5au, 0x08070103u, 0x08060103u, 0x08070202u, 0x08060202u)
/* Table B: 0xa4e = 0x0000a0a0 in every column (the firmware's value pool
 * also holds an unused 0x0000c0c0). Observed 0x0000d0e0 in the dormant k 4. */
#define MT7976_SX_A4E                   0x0000a0a0u

/* ------------------------------------------------------------------------
 * LO routing (chain banks)
 * ------------------------------------------------------------------------ */

/* 0x002  LO source of a chain. RW. Observed 0x02200000 on k 0..3, 0 on k 4.
 * [27:20]: 0x22 = SX0 (k 0), 0x32 = 5 GHz SX (k 4), 0x33 = SX1 at k 4.
 * [24] set switches the chain to its 5 GHz path (no 5 GHz RF input on this
 * part: reception stops). 0x02400000 offsets the WF0 LO by -1.27 MHz
 * (not a ratio). [static][measured] */
#define MT7976_LO_SRC                   0x002u
#define MT7976_LO_SRC_SEL               HW_GENMASK(27, 20)
#define MT7976_LO_SRC_SEL_SX0           0x22u
#define MT7976_LO_SRC_SEL_SX5G          0x32u
#define MT7976_LO_SRC_SEL_SX1           0x33u
#define MT7976_LO_SRC_5G_PATH           HW_BIT(24)
#define MT7976_LO_SRC_SX0               0x02200000u

/* 0x021  LO path enables, meaningful in a synthesizer's bank. RW.
 * Observed k 0 0xf0000000, k 4 0xa0000000, others 0. [static][measured]
 * [31:28] = 0xf marks the active SX, 0xa the inactive one (firmware).
 * Bits 25..30 are LO-path enables (clearing them stops reception); [30]
 * clear was also seen to add 16 dB of mixer/LO-buffer noise that follows
 * the LPF gain. Code never writes [27:25]. Setting k 4's [31:28] = 0xf wakes the 5 GHz SX. */
#define MT7976_LO_EN                    0x021u
#define MT7976_LO_EN_SX                 HW_GENMASK(31, 28)
#define MT7976_LO_EN_SX_ACTIVE          0xfu
#define MT7976_LO_EN_SX_INACTIVE        0xau
#define MT7976_LO_EN_GATES              HW_GENMASK(30, 25)

/* 0x022  Init value 0x72000000 (k 0..3), 0x73000000 (k 4); [24] follows the
 * SX selection (inferred). [static][measured] */
#define MT7976_LO_SRC2                  0x022u

/* 0x601  5 GHz LO distribution: 1 on k 0..4 for the 5 GHz SX path; init 0.
 * 0x602 init 1. [static][measured] */
#define MT7976_LO_DIST5G                0x601u
#define MT7976_LO_DIST5G_EN             HW_BIT(0)

/* Per-chain LO-buffer / quadrature codes from the firmware's RF table,
 * 2.4 GHz values: 0xa06 0x08088408 (WF1 0x0808868f), 0xa07 0x508 (WF0) /
 * 0x408, 0xa13 0x84860400 (WF1 0x86860400, WF2 0x08860400), 0xb87
 * 0x50006208, 0xa47 0x508, 0xb86 0x0002061f, 0xa54 0x40040002, 0xbb4
 * 0x801e3106. In the 5 GHz block 0xa06[11:8] and 0xa13[27:24] fall 0xc -> 0x1
 * from 4.9 to 7.1 GHz. Roles inferred. [static] */
#define MT7976_LOBUF_A06                0xa06u
#define MT7976_LOBUF_A13                0xa13u	/* MT7977 RF_INIT walker writes it on dies 1 / 2 too */

/*
 * SX0 retune (bank k 0, firmware order). [static][measured]
 *  0. presets once (pre-rows, table-A column for the ratio, table B);
 *  (firmware, after the presets and before the words: 0xbc6 on k 0 = LO
 *     in kHz, on k 1 = P[1]; adie.h ADIE_FREQ_NOTE_MT7976)
 *  1. 0x002 = MT7976_LO_SRC_SX0; 0x021 |= 0xf0000000;
 *  2. 0xa4b = INT word; 0xa4a = FRAC word (f_VCO = f_LO * DEN / NUM,
 *     N = f_VCO / 80 MHz);
 *  3. 0xb8a = MT7976_SX_B8A_LOCK;
 *  4. 0xb81 = 0x100; 0xb81 = 0x101; wait 10 us; poll 0xca7[16] <= 1 ms;
 *     0xb81 = 0x10;
 *  5. 0xca4[1:0] == 0, 0xca4[31:20] = band found;
 *  6. the per-chain lock step on k 1..P[1] (0xb8a = MT7976_SX_B8A_WF, then
 *     the lock step in that bank), at every channel set.
 * Waking the 5 GHz SX: k 4 0x021[31:28] = 0xf, then the same steps on k 4
 * with a 5 GHz table-A column.
 */

/* ------------------------------------------------------------------------
 * TOP
 * ------------------------------------------------------------------------ */

/* TOP 0x744  FEM control word. RW. Observed 0xaa800800. Sixteen 2-bit pin
 * fields, pin j at [2j + 1 : 2j] (MT7976_TOP_FEM_PIN), written whole by the
 * FEM pin mux of the common PHY init (adie_init.h, set ADIE_FEM_SET_744: the
 * turned-off pins' MT7976_TOP_FEM_PIN_OFF). The calibrations write pins 0,
 * 2, 7 and 9 ([1:0], [5:4], [15:14], [19:18]; MT7976_TOP_FEM_WF_FIELDS, one
 * set to 3, the others 2: (v & ~0x000cc033) | 0x00088032 etc.); the DPD
 * calibration
 * sets [21:20] = 3 for the call and, per step, [24] (calibration group 0;
 * [26] otherwise) on the die in calibration variable 0x19, then writes the
 * entry value back (rfmodes.h). The FEM helper V:e00526c2 in mode 0 (P[6]
 * = 1: T1{6,10,14}, T1{7,11,15}, T1{8,12,16}, T1{9,13,17}) writes
 * 0xaa888822 from the rest value (those four pins 2, as the RX DC
 * calibration below). External PA/LNA/TR-switch states
 * (inferred). Not the same register as chain-bank 0x744 on the MT7977.
 * [static][measured] */
#define MT7976_TOP_FEM                  0x744u
#define MT7976_TOP_FEM_DPD              HW_GENMASK(21, 20)
#define MT7976_TOP_FEM_FB               HW_BIT(24)	/* group 0 */
#define MT7976_TOP_FEM_FB_GN            HW_BIT(26)	/* group != 0 */
/* The FEM pin mux (adie_init.h, set ADIE_FEM_SET_744) writes the word as sixteen
 * 2-bit pin fields, pin j at [2j + 1 : 2j]; a pin it turns off gets
 * MT7976_TOP_FEM_PIN_OFF. The calibrations' FEM helper and the MT7976 DCOC bodies
 * on EPA boards set pins 0, 2, 7 and 9 ([1:0], [5:4], [15:14], [19:18]) to 2
 * (V:e00526c2 in mode 0, one read, one write). [static][measured] */
#define   MT7976_TOP_FEM_PIN(j)         HW_GENMASK(2u * (j) + 1u, 2u * (j))	/* j 0..15 */
#define   MT7976_TOP_FEM_PIN_OFF        2u
#define   MT7976_TOP_FEM_WF_FIELDS      (MT7976_TOP_FEM_PIN(0u) | MT7976_TOP_FEM_PIN(2u) | \
					 MT7976_TOP_FEM_PIN(7u) | MT7976_TOP_FEM_PIN(9u))
#define   MT7976_TOP_FEM_WF_CAL         (HW_PREP(MT7976_TOP_FEM_PIN(0u), 2u) | HW_PREP(MT7976_TOP_FEM_PIN(2u), 2u) | \
					 HW_PREP(MT7976_TOP_FEM_PIN(7u), 2u) | HW_PREP(MT7976_TOP_FEM_PIN(9u), 2u))

/* TOP 0xb08 / TOP 0x664  FEM words of the PHY-init routine's per-band FEM
 * call (V:e00cd44e -> V:e00cd2de, die 0, every call; phy_cal.h "Calibration
 * init"): 0xb08 [19:18] and [9:8] = 2 iff EEPROM 0x199 [2] (the FEM type),
 * else 0 (stock 0 -> 0x00080200); 0x664 [26] = !(EEPROM 0x199 [2])
 * (0x34d00fe0 -> 0x30d00fe0). Other bits unknown. [static][measured] */
#define MT7976_TOP_B08                  0xb08u
#define MT7976_TOP_B08_FEM_A            HW_GENMASK(19, 18)
#define MT7976_TOP_B08_FEM_B            HW_GENMASK(9, 8)
#define   MT7976_TOP_B08_FEM_ON         2u	/* both fields with MT7996_PHY_EE_199_FEM, else 0 */
#define MT7976_TOP_664                  0x664u
#define MT7976_TOP_664_FEM              HW_BIT(26)

/* TOP 0x4c4  0x2 / 0x20 during a calibration loop path, 0x01100000 in the
 * R-cal measurement; observed 0. [static][measured] */
#define MT7976_TOP_LOOP                 0x4c4u

/* ------------------------------------------------------------------------
 * RX DC-offset calibration (adie.h DCOC) on the MT7976. [static][measured]
 * ------------------------------------------------------------------------
 * 0xe90 [31:30] = 1 for both items; the word is read first and written back
 *   whole at the end (rest 0x01630210; the PHY-init routine's tail sets [30]
 *   after the calibrations, adie.h ADIE_CHAIN_E90).
 * 0x607 = 0x40008000 for the coarse conversion, 0x80008000 for the fine step
 *   (rest 0x00008000).
 * k0 0xa0f [1:0] = 3 (sx.h ADIE_SX_OUT_FORCE), cleared at the end.
 * TOP 0x070 is read (unused), then = ADIE_TOP_CHAIN_IF_CAL; TOP 0x744
 *   (MT7976_TOP_FEM) = (v & 0xfff33fcc) | 0x00088022 on EPA boards (pins 0,
 *   2, 7, 9 = 2: MT7976_TOP_FEM_WF_CAL); both restored.
 * The coarse codes are kept for the fine step (no whole-word 0x923 write);
 * 10 us before / 100 us after each start pulse.
 * DPD (phy_cal.h T2{0..3}) reruns the fine engine on its feedback chain in
 * every step, at the DPD gain, with the same 0xe90, 0x607 and TOP 0x070
 * values (rfmodes.h); it restores none of the result words. The TX LO / IQ
 * calibration (phy_cal.h T1{9,13,17}) runs it once per call on the WF's own
 * chain in the TX-detector state (0x010 = 0xa0f803c7, the same helper
 * V:e005ef86) and restores none of them either.
 */
#define MT7976_DCOC_E90                 0xe90u
#define MT7976_DCOC_E90_MODE            HW_GENMASK(31, 30)	/* 1 */
#define   MT7976_DCOC_E90_MODE_DCOC     1u	/* both DCOC bodies */
/* The DCOC bodies' start pulses: 10 us before the fine start, 100 us after each
 * coarse or fine start; in the coarse body also 10 us after each step index.
 * [static][measured] */
#define MT7976_DCOC_PRE_US              10u
#define MT7976_DCOC_START_US            100u
/* 0xe90 [31:30] is also the TX-detector enable of T2{6}'s detector select
 * (V:e00589aa): 1 on, 0 off (rfmodes.h TX power detector). [static][measured] */
#define MT7976_DCOC_607                 0x607u
#define MT7976_DCOC_607_COARSE          0x40008000u
#define MT7976_DCOC_607_FINE            0x80008000u
#define MT7976_DCOC_607_REST            0x00008000u

/* 0x083  TX power detector reading of the chain, [31:20]. DPD's TX power
 * sweep (rfmodes.h) reads it five times, 5 us apart, 100 us after the tone
 * starts, and keeps the median. [static] */
#define MT7976_TXDET_083                0x083u
#define MT7976_TXDET_083_VAL            HW_GENMASK(31, 20)

/* Chain 0x060 (bank WF, not TOP 0x060 ADIE_TOP_IFMODE): [6:3] = 2 in the TX
 * gain-step calibration (the same-chain loopback, rfmodes.h, phy_cal.h
 * T1{7,11,15}; V:e006366a mode 1, RMW from 0); restored. Name unknown
 * [guess: loop / aux input select]. [static][measured] */
#define MT7976_CHAIN_060                0x060u
#define MT7976_CHAIN_060_LOOP           HW_GENMASK(6, 3)

/* TX LO / IQ calibration (phy_cal.h T1{9,13,17}, rfmodes.h), chain bank WF.
 * 0x213 [19:4]: a 16-bit trim word per PA-gain range [name guess: TX LO /
 * IQ DAC trim]: written before every step from [31:16] of 0x280..0x286 read
 * at the body's start, by the TX gain index >> 3; stock (P[6] = 1, group
 * 0): 7 -> 0x284, 6 -> 0x283, 5 -> 0x284, other -> 0x284 (LO phase) / 0x285
 * (IQ phase); the P[6] = 0 path (not exercised) takes 0x280, 0x282, 0x283 or
 * 0. Then [31:28] = 0xf. Saved at the start and written back at the end,
 * with 0x218, which the body clears when P[6] = 1 (name unknown).
 * 0x280..0x286: constants of item 0's MT7976 RF-init table walk (V:e0051002,
 * per chain: 0x00302000, 0x24302000, 0x68302000, 0xec302000, 0xf0312000,
 * 0xf0332000, 0xf1332000), read here as the trim words' source (chain-bank
 * reads, not the TOP 0x200..0x2ff no-read range of adie.h).
 * 0x202 [22:20] and [18:16] (TX mode HAL V:e0052582, group 0 only) = a WM RAM
 * byte per step (phy_cal.h MT7996_PHY_TXLOIQ_T76_202, 0) [name guess: TX
 * mode]. All restored. [static, measured] */
#define MT7976_TXTRIM_213               0x213u
#define MT7976_TXTRIM_213_WORD          HW_GENMASK(19, 4)
#define MT7976_TXTRIM_213_EN            HW_GENMASK(31, 28)	/* 0xf [guess] */
/* 0x280..0x286 are the rows of item 0's MT7976 RF_COMMON walk (rfinit.h); the MT7977
 * item-1 tables (rfchan.h) also write 0x286 on dies 1 / 2, role there not decoded. */
#define MT7976_TXTRIM_SRC(i)            (0x280u + (i))	/* i 0..6, word in [31:16] */
#define MT7976_CHAIN_218                0x218u
#define MT7976_TXMODE_202               0x202u
#define MT7976_TXMODE_202_2220          HW_GENMASK(22, 20)
#define MT7976_TXMODE_202_1816          HW_GENMASK(18, 16)

/* ------------------------------------------------------------------------
 * DFS/SM monitor receiver: chain WF4 (k 4), "RF_DET" input
 * ------------------------------------------------------------------------
 * A dual-band receive chain whose LO is the TTG PLL (sx.h), not an SX.
 * The firmware starts it for zero-wait DFS (UNI OFFCH_SCAN_CTRL, scan type 2)
 * on a 2.4 GHz channel or 5000..6275 MHz, bandwidth class 0..3; it then
 * programs 0x941 as predicted (0x3ceccccc for 2437 MHz) and leaves the chain
 * configured after a stop. [static][measured] Its input is switched by board
 * GPIO 4 (1 = 5 GHz) and blanked by the SPI injector (adie.h) while the AP's
 * own bands transmit. [static] Its IQ has not been delivered to the capture
 * path by any register setting tried, so the TTG range buys no reception yet.
 * [measured]
 *
 * Start, firmware order (k 4 unless noted):
 *   0x010 = 0x0003fbcb, 0x011 = 0x80000000;
 *   TTG programming (sx.h) with mult 2 (2.4 GHz) or 1 (5 GHz);
 *   TOP 0x000 = 0x0000ffef;
 *   0x101 |= 1, 0x122 |= 0x02000000, 0x000 = 0x88000000, 0x105 = 0x80000000;
 *   2.4 GHz: 0x605 &= ~4, 0x003 = 0xe0000000;
 *   5 GHz:   0x605 |= 4, 0x606 = 0xc0020001, 0x172 |= 0x04000000,
 *            pulse 0xb80[7], 0x003 = 0xc0000000;
 *   0x100 = bandwidth class; a DC calibration of the chain;
 *   0x600 |= 0x10000, then 0x600[11:8] = 4 (0x600 is the RX step override
 *   ADIE_DCOC_STEP of adie.h: [16] enable, [11:8] step index).
 * Off: 0x000 = 0, 0x105 = 0, 0x600[16] = 0.
 * Observed running (2437 MHz): 0x000 0x88000000, 0x003 0xe0000000, 0x010
 * 0x0003fbcb, 0x105 0x80000000, 0x122 0xea0483df, 0x942 0x2620000c, 0x943 0.
 * DPD (phy_cal.h T2{0..3}) on WF3, group 0, uses it as the feedback
 * receiver: 0x010 = 0x0003fbcb, 0x101 |= 1, 0x122 |= 0x02000000, 0x000 =
 * 0x88000000, 0x105 = 0x80000000, 0x605 &= ~4, 0x003 |= 0xc0000000, with the
 * TTG at F - 20 MHz (mult 2); its 17-register save writes all but 0x010
 * back after every step (rfmodes.h). [static, measured]
 */
#define MT7976_MON_PWR                  0x105u
#define MT7976_MON_PWR_ON               HW_BIT(31)
#define MT7976_MON_EN                   0x122u	/* MT7977 RF_INIT writes it on dies 1 / 2 too, role there not decoded */
#define MT7976_MON_EN_ON                HW_BIT(25)
#define MT7976_MON_BAND                 0x605u
#define MT7976_MON_BAND_5G              HW_BIT(2)
/* (0x605 in a chain bank: the TX test mode of T2{4} clears [1] and writes
 * [11:8] from table 0xe00812c0, rfmodes.h; the TTG RX calibrations clear
 * [0] on every chain, below.) [static][measured] */
#define MT7976_MON_BAND_B0              HW_BIT(0)
#define MT7976_MON_CTL                  0x600u
#define MT7976_MON_CTL_ON               HW_BIT(16)
#define MT7976_MON_CTL_MODE             HW_GENMASK(11, 8)
#define MT7976_MON_CHAIN_MODE           0x88000000u
#define MT7976_MON_INPUT_2G4            0xe0000000u
#define MT7976_MON_INPUT_5G             0xc0000000u
#define MT7976_MON_TTG_MULT_2G4         2u
#define MT7976_MON_TTG_MULT_5G          1u

/* ------------------------------------------------------------------------
 * TTG RX calibrations (phy_cal.h T1{18..23}, group 0 only) on the MT7976.
 * [static][measured]
 * ------------------------------------------------------------------------
 * Mode-5 path (V:e006366a; T1[23]'s mode 4 takes the same path): every
 * chain 0x001 [9:8] = 3, [17:16] = 2 and 0x605 [0] = 0 (V:e0063694 calls
 * the all-chain helper V:e0047f62 without setting its mode argument a5; the
 * trace shows every chain); k 4 0x105 (MT7976_MON_PWR) = 0; TOP 0x070 =
 * ADIE_TOP_CHAIN_IF_TTG_RX; with P[5] = 1, WF0 0x003 |= 0x80000000, then
 * |= 0x20000000. Chain state (V:e0062e84): every chain 0x010 =
 * ADIE_CHAIN_EN_RX_AUX (rfmodes.h), 0x011 = 0x80000000. Per gain step every chain's
 * 0x600 (MT7976_MON_CTL / adie.h ADIE_DCOC_STEP) = gain, the route helper
 * (0x940 [8] = 0, 0x606 [5:4], [16] = 0), then the TTG in k 4 with mult 2:
 * LO + 5 MHz (DC phase), LO - SUB with an offset word (IQ phase; 20 MHz:
 * LO - 20 MHz, 2.5 MHz; phy_cal.h MT7996_PHY_TTG_RX_SUB_KHZ), sx.h. The
 * TTG is the monitor receiver's LO; here it is the injected tone, with
 * 0x105 = 0 (MON_PWR off [inferred]).
 */

#endif /* HW_MT7976_ADIE_H */
