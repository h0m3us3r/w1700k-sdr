/* SPDX-License-Identifier: MIT */
/* The occupancy program's memory (occ.c), as offsets into the DSP core's DTCM: the core itself
 * has it at MT7996_DSP_DTCM_BASE, our WM has core b's at MT7996_DSP_MCU_DTCM(b) (hw/mt7996/dsp.h).
 * Shared by the program, its start-up code and the WM's side (stream/occ.h).
 *
 *   0x0000  the box: who runs here and whether it still does
 *   0x0040  the request: the WM writes FLAGS, THRESHOLD and EVERY, then a new number into REQ
 *   0x0060  the answer: DONE = REQ once the snapshot is taken in, and REPORTS, which has moved
 *           when that snapshot completed a report
 *   0x0100  the program's tables and working memory (its linker script)
 *   0x8000  the report (struct occ_report)
 *   0xd000  the snapshot: OCC_SAMPLES sample words, put here by MEM_DMA
 *   below 0x10000: the stack
 */
#ifndef DSP_OCC_LAYOUT_H
#define DSP_OCC_LAYOUT_H

#define OCC_BOX_MAGIC	0x0000
#define OCC_BOX_STATE	0x0004
#define OCC_BOX_CAUSE	0x0008		/* a trap's mcause, mepc and mtval */
#define OCC_BOX_EPC	0x000c
#define OCC_BOX_TVAL	0x0010
#define OCC_MAGIC	0x3143434f	/* 'OCC1' */
#define OCC_STATE_RUNNING 1
#define OCC_STATE_TRAPPED 2

#define OCC_REQ		0x0040
#define OCC_FLAGS	0x0044
#define OCC_FLAG_RESET	1		/* forget everything first: the sums, and the noise's shape */
#define OCC_THRESHOLD	0x0048		/* a float: a bin is busy above this many times its noise */
#define OCC_EVERY	0x004c		/* snapshots to a report (0 as 1) */
#define OCC_DONE	0x0060
#define OCC_CYCLES	0x0064		/* the core's cycles from the request to the answer */
#define OCC_REPORTS	0x0068		/* reports written since the program started */

#define OCC_DATA	0x0100
#define OCC_REPORT	0x8000
#define OCC_SAMPLE_AT	0xd000
#define OCC_STACK_TOP	0x10000

#define OCC_BINS	64		/* bin k is at k * fs / 64, k >= 32 below the LO */
#define OCC_BLOCKS	32		/* transforms a snapshot */
#define OCC_SAMPLES	(OCC_BINS * OCC_BLOCKS)		/* one chunk */
#define OCC_LEARN	64		/* snapshots the noise's shape is first learned from */
#define OCC_STRONG	100		/* in power: 20 dB */
#define OCC_QUIET_LEAST	8		/* quiet snapshots among OCC_LEARN that make a shape to trust */
#define OCC_REPORT_WORDS (8 + 5 * OCC_BINS)

#ifndef __ASSEMBLER__
#include <stdint.h>

/* A report: what the snapshots since the last one showed. Powers are of a Hann-windowed 64-point
 * transform of raw sample words, unscaled: a full-scale tone is (8191 * 32)^2. They are as
 * sampled, under whatever gain the receiver's AGC had at each snapshot. */
struct occ_report {
	uint32_t seq;			/* reports made since the program started */
	uint32_t snapshots;		/* snapshots in this one */
	uint32_t peak;			/* the largest |I| or |Q| among their samples */
	uint32_t cycles;		/* the last snapshot's cost */
	uint32_t total;			/* snapshots since the last reset, the OCC_LEARN it learned from included */
	float level_min, level_max;	/* the smallest and largest noise level a snapshot had: how far the
					 * receiver's gain moved */
	uint32_t quiet;			/* of the OCC_LEARN snapshots it learned from, how many were the quiet
					 * group the noise's shape was taken from; under OCC_QUIET_LEAST it
					 * is still looking for a better one */
	float mean[OCC_BINS];		/* each bin's power, averaged over the snapshots */
	float max[OCC_BINS];		/* the largest any snapshot showed */
	uint32_t busy[OCC_BINS];	/* in how many of the snapshots the bin was busy */
	float noise[OCC_BINS];		/* each bin's noise, averaged over the snapshots: what mean[] would
					 * be with nothing in the air */
	uint32_t strong[OCC_BINS];	/* in how many it was busy and within OCC_STRONG of the strongest
					 * busy bin of that snapshot: where a transmission is, without
					 * its skirts */
};
_Static_assert(sizeof(struct occ_report) == 4 * OCC_REPORT_WORDS, "struct occ_report");
#endif

#endif
