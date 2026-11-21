// SPDX-License-Identifier: MIT
/* Spectrum occupancy on an MT7996 DSP core (RV32IMAF, 640 MHz, memory of its own).
 *
 * Our WM hands the core a chunk of the stream now and then: 2048 raw sample words, copied into
 * the core's memory by MEM_DMA with no CPU touching them (stream/occ.h), and a request. The core
 * takes the chunk's mean off (the converter's offset, which is no signal), turns it into a 64-bin
 * power spectrum, 32 Hann-windowed 64-point transforms averaged, and keeps account per bin: the
 * sum, the largest, and in how many snapshots the bin was busy, and strong. Every so many snapshots it writes
 * a report and starts the next.
 *
 * Busy means above the threshold times the bin's noise in that snapshot, and the noise is not a
 * setting: the core finds it. Two things about this receiver decide how.
 *
 * Its gain is not constant. The AGC steps it for what is in the air, and the noise in every bin
 * moves with it: seen on a unit at 2.4 GHz, over 28 dB from one chunk to another. No level that
 * was the noise a moment ago says what it is now. So the noise is taken as a shape, one factor a
 * bin, times a level, one number a snapshot. [measured]
 *
 * The level is the 8th smallest over the bins of power / shape: right for as long as an eighth of
 * the span is free in that snapshot, at whatever gain.
 *
 * The shape has to be learned, because the bins do not share one either: the samples come at
 * twice the channel filter's width, so the outer half of the span is the filter's skirt and
 * stopband, down to 28 dB under the middle. It is what the quiet snapshots share. Two snapshots
 * of noise alone are the same spectrum at two gains, bin for bin in one proportion, and no two
 * with different things in the air are; so the first OCC_LEARN snapshots are kept and put into
 * groups of the mutually proportional, and the largest group is the quiet one: the shape is its
 * median, bin by bin. Those snapshots are then accounted for like any other. Afterwards the shape
 * follows every snapshot in which next to nothing is busy, a 64th of the way. Where fewer than
 * OCC_QUIET_LEAST of the 64 were quiet the band gave it too little to go by: it keeps the next 64
 * as well, and so on, and takes a larger quiet group's shape when one comes. (An earlier form
 * took each bin's factor as the lower quartile over time of power / level and worked level and
 * shape out of each other. A signal over many bins half of the time gave that a second answer,
 * with the signal as the noise, and it found that one.)
 * [measured: on spectra recorded from a unit at 2.4 GHz, 9 to 30 of the first 64 snapshots were
 * the quiet group, and the first report of 100 snapshots was within 0.04 (the median; 0.15 the
 * worst bin of the worst of 66 starts) of the shares under the shape of a run 20 times as long]
 *
 * Busy is energy, and a strong transmitter's skirts are energy too: near one, bins all over the
 * span are busy while it sends, the stopband's most of all, where the noise is lowest. So a
 * second count is kept, of the snapshots in which a bin was busy and within 20 dB of the
 * strongest busy bin: that is where a transmission sits. (Among the busy bins only: the bin at
 * the LO can hold more than any signal, steadily, and that is in its shape, not busy.)
 * [measured: at 5 GHz beside an access point, 17 % busy in every bin of 160 MHz, in and out of
 * the filter]
 *
 * What it cannot see: a signal that leaves less than an eighth of the span free reads weaker than
 * it is; what is there all the time, the same, is noise to it; where the band was never quiet
 * while it learned, the shape is a guess (the report says from how many snapshots it learned);
 * and outside the channel filter (the outer bins) there is little to see at all: at low gain the
 * noise there is under the converter's last bit, and what is left reads as busy.
 *
 * One snapshot costs about 0.35 ms of the core, and the learning once more. [measured]
 *
 * Built for the core (firmware/own.py dsp_code) and, with OCC_NATIVE, for the host, where
 * tests/test_firmware_occ.py runs it against the same arithmetic in Python.
 */
#include <stdint.h>

#include "occ_layout.h"
#include "tables.h"

#ifdef OCC_NATIVE
extern uint8_t occ_dtcm[];
#define DTCM ((uintptr_t)occ_dtcm)
static inline uint32_t cycles(void) { return 0; }
#else
#define DTCM ((uintptr_t)0x00200000u)		/* hw/mt7996/dsp.h MT7996_DSP_DTCM_BASE */
static inline uint32_t cycles(void) { uint32_t v; __asm__ volatile("rdcycle %0" : "=r"(v)); return v; }
#endif

#define W(off) (*(volatile uint32_t *)(DTCM + (off)))
#define F(off) (*(volatile float *)(DTCM + (off)))
#define REPORT ((volatile struct occ_report *)(DTCM + OCC_REPORT))
#define SAMPLES ((const volatile uint32_t *)(DTCM + OCC_SAMPLE_AT))

#define N OCC_BINS
#define LOWEST (N / 8)		/* of a snapshot's bins: its noise level */
#define ALIKE_BINS (N / 2)	/* two snapshots are compared in every other bin */
#define ALIKE_BAND 1.6f		/* ... and are alike where their proportion is within this of its median, */
#define ALIKE_NEED 26u		/* in this many of them */
#define QUIET_BUSY 2u		/* a snapshot with at most this many busy bins is quiet: the shape follows it */
#define FOLLOW (1.0f / 64)
#define LEAST 1e-9f		/* neither a factor nor a level is ever nothing: silence divides by them */

static float re[N], im[N], power[N], sorted[N > OCC_LEARN ? N : OCC_LEARN], sum[N], most[N], shape[N];
static float kept[OCC_LEARN][N], kept_scale[OCC_LEARN], found[N];
static unsigned char kept_group[OCC_LEARN], leader[OCC_LEARN], members[OCC_LEARN];
static uint32_t busy[N], strong[N], count, total, peak, reports, quiet, filled;
static float levels, level_min, level_max;

static inline int sext14(uint32_t v) { return (int)(v << 18) >> 18; }

static void fft(void)
{
	for (unsigned len = 2, step = N / 2; len <= N; len <<= 1, step >>= 1)
		for (unsigned i = 0; i < N; i += len)
			for (unsigned j = 0, k = 0; j < len / 2; j++, k += step) {
				unsigned a = i + j, b = a + len / 2;
				float wr = tw_re[k], wi = tw_im[k];
				float xr = re[b] * wr - im[b] * wi, xi = re[b] * wi + im[b] * wr;

				re[b] = re[a] - xr;
				im[b] = im[a] - xi;
				re[a] += xr;
				im[a] += xi;
			}
}

/* The chunk's spectrum into p[], its mean taken off first. */
static void snapshot(float *p)
{
	int si = 0, sq = 0;
	float mi, mq;

	for (unsigned n = 0; n < OCC_SAMPLES; n++) {
		uint32_t v = SAMPLES[n];
		int i = sext14(v >> 16), q = sext14(v);
		unsigned ai = (unsigned)(i < 0 ? -i : i), aq = (unsigned)(q < 0 ? -q : q);

		si += i;
		sq += q;
		if (ai > peak)
			peak = ai;
		if (aq > peak)
			peak = aq;
	}
	mi = (float)si * (1.0f / OCC_SAMPLES);
	mq = (float)sq * (1.0f / OCC_SAMPLES);
	for (unsigned k = 0; k < N; k++)
		p[k] = 0.0f;
	for (unsigned b = 0; b < OCC_BLOCKS; b++) {
		const volatile uint32_t *w = SAMPLES + b * N;

		for (unsigned n = 0; n < N; n++) {
			uint32_t v = w[n];
			unsigned r = rev[n];

			re[r] = ((float)sext14(v >> 16) - mi) * hann[n];
			im[r] = ((float)sext14(v) - mq) * hann[n];
		}
		fft();
		for (unsigned k = 0; k < N; k++)
			p[k] += re[k] * re[k] + im[k] * im[k];
	}
	for (unsigned k = 0; k < N; k++)
		p[k] *= 1.0f / OCC_BLOCKS;
}

/* The mth smallest of sorted[0..n), which it leaves in disorder; never less than LEAST. */
static float smallest(unsigned n, unsigned m)
{
	for (unsigned i = 0; i < m; i++) {
		unsigned at = i;
		float t;

		for (unsigned k = i + 1; k < n; k++)
			if (sorted[k] < sorted[at])
				at = k;
		t = sorted[i];
		sorted[i] = sorted[at];
		sorted[at] = t;
	}
	return sorted[m - 1] > LEAST ? sorted[m - 1] : LEAST;
}

/* A snapshot's noise level under the shape as it is. */
static float level(const float *p)
{
	for (unsigned k = 0; k < N; k++)
		sorted[k] = p[k] / shape[k];
	return smallest(N, LOWEST);
}

static void scale_shape(void)
{
	float all = 0.0f;

	for (unsigned k = 0; k < N; k++)
		all += shape[k];
	for (unsigned k = 0; k < N; k++)
		shape[k] *= (float)N / all;
}

/* Whether snapshot a is b at another gain: in how many of the compared bins their proportion is
 * within ALIKE_BAND of its median, which goes to *scale. */
static unsigned alike(const float *a, const float *b, float *scale)
{
	unsigned n = 0;
	float m;

	for (unsigned j = 0; j < ALIKE_BINS; j++)
		sorted[j] = a[2 * j] / (b[2 * j] > LEAST ? b[2 * j] : LEAST);
	m = smallest(ALIKE_BINS, ALIKE_BINS / 2);
	for (unsigned j = 0; j < ALIKE_BINS; j++)
		n += sorted[j] * ALIKE_BAND >= m && sorted[j] <= m * ALIKE_BAND;
	*scale = m;
	return n;
}

/* The shape the kept snapshots give, into found[]: the median, bin by bin, of the largest group
 * of them that are each other at another gain. Returns how many that group is. */
static unsigned learn(void)
{
	unsigned groups = 0, best = 0, c, n;

	for (unsigned t = 0; t < OCC_LEARN; t++) {
		for (c = 0; c < groups; c++)
			if (alike(kept[t], kept[leader[c]], &kept_scale[t]) >= ALIKE_NEED)
				break;
		if (c == groups) {
			leader[groups] = (unsigned char)t;
			members[groups++] = 0;
			kept_scale[t] = 1.0f;
		}
		kept_group[t] = (unsigned char)c;
		if (++members[c] > members[best])
			best = c;
	}
	for (unsigned k = 0; k < N; k++) {
		n = 0;
		for (unsigned t = 0; t < OCC_LEARN; t++)
			if (kept_group[t] == best)
				sorted[n++] = kept[t][k] / kept_scale[t];
		found[k] = smallest(n, (n + 1) / 2);
	}
	return members[best];
}

/* OCC_LEARN snapshots are kept: their shape is taken if their quiet group is the largest so far. */
static void learned(void)
{
	unsigned n = learn();

	filled = 0;
	if (n <= quiet)
		return;
	quiet = n;
	for (unsigned k = 0; k < N; k++)
		shape[k] = found[k];
	scale_shape();
}

/* A snapshot into the account; follow: a quiet one moves the shape towards itself. */
static void account(const float *p, float threshold, int follow)
{
	float l = level(p), f, top = 0.0f, all = 0.0f;
	unsigned n = 0;

	for (unsigned k = 0; k < N; k++) {
		all += p[k] / shape[k];
		if (p[k] > shape[k] * l * threshold) {
			n++;
			if (p[k] > top)
				top = p[k];
		}
	}
	for (unsigned k = 0; k < N; k++) {
		f = shape[k] * l;
		sum[k] += p[k];
		if (p[k] > most[k])
			most[k] = p[k];
		if (p[k] > f * threshold) {
			busy[k]++;
			if (p[k] * (float)OCC_STRONG >= top)
				strong[k]++;
		}
	}
	if (follow && n <= QUIET_BUSY && all > LEAST) {
		for (unsigned k = 0; k < N; k++) {
			f = shape[k] + FOLLOW * (p[k] * (float)N / all - shape[k]);
			shape[k] = f > LEAST ? f : LEAST;
		}
		scale_shape();
	}
	levels += l;
	if (!count || l < level_min)
		level_min = l;
	if (!count || l > level_max)
		level_max = l;
	count++;
}

static void clear(void)
{
	for (unsigned k = 0; k < N; k++) {
		sum[k] = 0.0f;
		most[k] = 0.0f;
		busy[k] = 0;
		strong[k] = 0;
	}
	count = 0;
	peak = 0;
	levels = 0.0f;
}

static void report(uint32_t took)
{
	volatile struct occ_report *r = REPORT;
	float each = 1.0f / (float)count;

	for (unsigned k = 0; k < N; k++) {
		r->mean[k] = sum[k] * each;
		r->max[k] = most[k];
		r->busy[k] = busy[k];
		r->noise[k] = shape[k] * levels * each;
		r->strong[k] = strong[k];
	}
	r->snapshots = count;
	r->peak = peak;
	r->cycles = took;
	r->total = total;
	r->level_min = level_min;
	r->level_max = level_max;
	r->quiet = quiet;
	r->seq = ++reports;
	clear();
}

/* One request served. */
void occ_serve(uint32_t req)
{
	uint32_t flags = W(OCC_FLAGS), every = W(OCC_EVERY), t0 = cycles(), took;
	float threshold = F(OCC_THRESHOLD);

	if (flags & OCC_FLAG_RESET) {
		clear();
		total = filled = quiet = 0;
	}
	if (total < OCC_LEARN) {			/* the first round: accounted for once there is a shape */
		snapshot(kept[filled++]);
		if (++total == OCC_LEARN) {
			learned();
			for (unsigned t = 0; t < OCC_LEARN; t++)
				account(kept[t], threshold, 0);
		}
	} else if (quiet < OCC_QUIET_LEAST) {		/* still looking: accounted for under the shape so far */
		snapshot(kept[filled]);
		account(kept[filled], threshold, 0);
		total++;
		if (++filled == OCC_LEARN)
			learned();
	} else {
		snapshot(power);
		account(power, threshold, 1);
		total++;
	}
	took = cycles() - t0;
	if (count && count >= every)
		report(took);
	W(OCC_CYCLES) = took;
	W(OCC_REPORTS) = reports;
	W(OCC_DONE) = req;
}

#ifndef OCC_NATIVE
/* From start.S, its memory cleared, and for good. The request the core finds at its start is not
 * one for it: the WM asks with a number it has not seen answered. */
void occ_main(void)
{
	uint32_t last = W(OCC_REQ), req;

	W(OCC_REPORTS) = 0;
	W(OCC_DONE) = last;
	W(OCC_BOX_MAGIC) = OCC_MAGIC;
	W(OCC_BOX_STATE) = OCC_STATE_RUNNING;
	for (;;) {
		req = W(OCC_REQ);
		if (req == last)
			continue;
		last = req;
		occ_serve(req);
	}
}
#endif
