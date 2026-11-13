/* SPDX-License-Identifier: MIT */
/*
 * Reduced sample slices: a raw 8 KiB chunk of 2048 sample words (iq_format.h)
 * requantised to fewer bits per I/Q pair, with one scale per slice, so a
 * stream fits a slower link. Encoded on the unit's SoC (mt7996-stream --send,
 * NEON), decoded by the host's w1700k-stream back to 14-bit sample words.
 *
 *   format  per component          slice bytes  layout
 *   16      8 bits, -127..127      4096         byte 2k = Q, byte 2k+1 = I
 *   14      7 bits, -63..63        3584         4 samples in 56 bits (7 bytes,
 *                                               little-endian), 7-bit fields
 *                                               Q0, I0, Q1, I1, ... from bit 0
 *   8       4 bits, -7..7          2048         byte k = (I & 15) << 4 | (Q & 15)
 *   7       11 levels, -5..5       1792         code = (I + 5) * 11 + (Q + 5),
 *                                               0..120 in 7 bits; 8 samples in
 *                                               56 bits, sample 0 from bit 0
 *
 * Scale: a code c in 0..IQ_REDUCE_SCALE_MAX names a multiplier
 * m = iq_reduce_m[c] = round(8192 * 2^(-c/16)), 8192 down to 1. A component x
 * (14-bit signed) is sent as
 *     q = (x * m + 4096) >> 13                    (arithmetic shift)
 * and read back as
 *     x' = (q * step + 512) >> 10,  step = round(8192 * 1024 / m),
 * clamped to 14 bits. The encoder takes the slice's largest magnitude and the
 * largest multiplier that keeps it inside the format's range, so code 0 is
 * exact (a slice that fits the format as it is), the scale wastes at most
 * 0.38 dB of the range down to m = 16, and nothing clips. Below m = 16 (only
 * the 8- and 7-bit formats, near full scale) the multipliers are whole
 * numbers and the waste grows to at most 1.6 dB.
 */
#ifndef HW_MT7996_IQ_REDUCE_H
#define HW_MT7996_IQ_REDUCE_H

#include <stdint.h>
#include <string.h>

#if defined(__aarch64__) && !defined(IQ_REDUCE_NO_NEON)
#include <arm_neon.h>
#define IQ_REDUCE_NEON 1
#endif

#define IQ_REDUCE_SAMPLES	2048u
#define IQ_REDUCE_MAX_BYTES	4096u
#define IQ_REDUCE_SLACK		8u	/* an encoder may write this far past the slice */
#define IQ_REDUCE_SCALE_MAX	208u
#ifndef IQ_REDUCE_PREFETCH
#define IQ_REDUCE_PREFETCH	256	/* bytes ahead of the first pass */
#endif

static const uint16_t iq_reduce_m[IQ_REDUCE_SCALE_MAX + 1] = {
	8192, 7845, 7512, 7194, 6889, 6597, 6317, 6049, 5793, 5547, 5312, 5087, 4871, 4665, 4467, 4277,
	4096, 3922, 3756, 3597, 3444, 3298, 3158, 3025, 2896, 2774, 2656, 2543, 2436, 2332, 2233, 2139,
	2048, 1961, 1878, 1798, 1722, 1649, 1579, 1512, 1448, 1387, 1328, 1272, 1218, 1166, 1117, 1069,
	1024, 981, 939, 899, 861, 825, 790, 756, 724, 693, 664, 636, 609, 583, 558, 535,
	512, 490, 470, 450, 431, 412, 395, 378, 362, 347, 332, 318, 304, 292, 279, 267,
	256, 245, 235, 225, 215, 206, 197, 189, 181, 173, 166, 159, 152, 146, 140, 134,
	128, 123, 117, 112, 108, 103, 99, 95, 91, 87, 83, 79, 76, 73, 70, 67,
	64, 61, 59, 56, 54, 52, 49, 47, 45, 43, 41, 40, 38, 36, 35, 33,
	32, 31, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 17,
	16, 15, 15, 14, 13, 13, 12, 12, 11, 11, 10, 10, 10, 9, 9, 8,
	8, 8, 7, 7, 7, 6, 6, 6, 6, 5, 5, 5, 5, 5, 4, 4,
	4, 4, 4, 4, 3, 3, 3, 3, 3, 3, 3, 2, 2, 2, 2, 2,
	2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1,
	1,
};

/* A format's largest component magnitude; 0: not a reduced format. */
static inline unsigned iq_reduce_range(unsigned format)
{
	switch (format) {
	case 16: return 127;
	case 14: return 63;
	case 8: return 7;
	case 7: return 5;
	default: return 0;
	}
}

/* Bytes of one reduced slice; 0: not a reduced format. */
static inline unsigned iq_reduce_bytes(unsigned format)
{
	switch (format) {
	case 16: return IQ_REDUCE_SAMPLES * 2;
	case 14: return IQ_REDUCE_SAMPLES / 4 * 7;
	case 8: return IQ_REDUCE_SAMPLES;
	case 7: return IQ_REDUCE_SAMPLES / 8 * 7;
	default: return 0;
	}
}

static inline int iq_reduce_sext14(uint32_t v)
{
	return (int)((v & 0x3fffu) ^ 0x2000u) - 0x2000;
}

/* The scale code for a slice whose largest magnitude is `peak` (at most 8191):
 * the first multiplier with peak * m < (2 * range + 1) * 4096, which keeps
 * every q within the range, -8192 included. */
static inline unsigned iq_reduce_scale(unsigned peak, unsigned range)
{
	const uint32_t limit = (2 * range + 1) * 4096u;
	unsigned lo = 0, hi = IQ_REDUCE_SCALE_MAX, mid;

	while (lo < hi) {
		mid = (lo + hi) / 2;
		if (peak * (uint32_t)iq_reduce_m[mid] < limit)
			hi = mid;
		else
			lo = mid + 1;
	}
	return lo;
}

static inline unsigned iq_reduce_peak_scalar(const uint32_t *w)
{
	unsigned peak = 0, k;
	int v;

	for (k = 0; k < IQ_REDUCE_SAMPLES; k++) {
		v = iq_reduce_sext14(w[k] >> 16);
		v = v < 0 ? -v : v;
		peak = (unsigned)v > peak ? (unsigned)v : peak;
		v = iq_reduce_sext14(w[k]);
		v = v < 0 ? -v : v;
		peak = (unsigned)v > peak ? (unsigned)v : peak;
	}
	return peak > 8191 ? 8191 : peak;
}

static inline int iq_reduce_q(uint32_t field, int m)
{
	return (iq_reduce_sext14(field) * m + 4096) >> 13;
}

static inline void iq_reduce_put56(uint8_t *out, uint64_t v)
{
	unsigned k;

	for (k = 0; k < 7; k++)
		out[k] = (uint8_t)(v >> (8 * k));
}

static inline void iq_reduce_encode_scalar(unsigned format, int m, const uint32_t *w, uint8_t *out)
{
	uint64_t v;
	unsigned k, j;

	switch (format) {
	case 16:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k++) {
			out[2 * k] = (uint8_t)iq_reduce_q(w[k], m);
			out[2 * k + 1] = (uint8_t)iq_reduce_q(w[k] >> 16, m);
		}
		break;
	case 14:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 4, out += 7) {
			for (v = 0, j = 0; j < 4; j++)
				v |= (uint64_t)((unsigned)iq_reduce_q(w[k + j], m) & 0x7f) << (14 * j) |
				     (uint64_t)((unsigned)iq_reduce_q(w[k + j] >> 16, m) & 0x7f) << (14 * j + 7);
			iq_reduce_put56(out, v);
		}
		break;
	case 8:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k++)
			out[k] = (uint8_t)(((unsigned)iq_reduce_q(w[k], m) & 15) |
					   ((unsigned)iq_reduce_q(w[k] >> 16, m) & 15) << 4);
		break;
	case 7:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 8, out += 7) {
			for (v = 0, j = 0; j < 8; j++)
				v |= (uint64_t)(unsigned)((iq_reduce_q(w[k + j] >> 16, m) + 5) * 11 +
							  iq_reduce_q(w[k + j], m) + 5) << (7 * j);
			iq_reduce_put56(out, v);
		}
		break;
	}
}

#ifdef IQ_REDUCE_NEON
/* Four samples as eight 16-bit lanes Q0, I0, Q1, I1, ..., each times 4. */
static inline int16x8_t iq_reduce_load(const uint32_t *w)
{
	return vshlq_n_s16(vreinterpretq_s16_u32(vld1q_u32(w)), 2);
}

static inline unsigned iq_reduce_peak_neon(const uint32_t *w)
{
	int16x8_t a = vdupq_n_s16(0), b = a;
	unsigned k;

	for (k = 0; k < IQ_REDUCE_SAMPLES; k += 16) {
		/* The slice comes from DMA: nothing of it is in any cache, and the
		 * hardware prefetcher restarts at every page. This first pass is
		 * what brings it in; the encoder's pass then finds it in L1. */
		if (k * 4 + IQ_REDUCE_PREFETCH < IQ_REDUCE_SAMPLES * 4)	/* never beyond the slice */
			__builtin_prefetch((const uint8_t *)(w + k) + IQ_REDUCE_PREFETCH);
		a = vmaxq_s16(a, vqabsq_s16(iq_reduce_load(w + k)));
		b = vmaxq_s16(b, vqabsq_s16(iq_reduce_load(w + k + 4)));
		a = vmaxq_s16(a, vqabsq_s16(iq_reduce_load(w + k + 8)));
		b = vmaxq_s16(b, vqabsq_s16(iq_reduce_load(w + k + 12)));
	}
	return (unsigned)vmaxvq_s16(vmaxq_s16(a, b)) >> 2;
}

/* vqrdmulh of 4x by m: (4xm + 2^14) >> 15 = (xm + 4096) >> 13. */
static inline int16x8_t iq_reduce_q4(const uint32_t *w, int16x8_t m)
{
	return vqrdmulhq_s16(iq_reduce_load(w), m);
}

/* Two 56-bit values in the 64-bit lanes: their 14 bytes. */
static inline void iq_reduce_store56(uint8_t *out, uint64x2_t v)
{
	static const uint8_t pick[16] = { 0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 0xff, 0xff };

	vst1q_u8(out, vqtbl1q_u8(vreinterpretq_u8_u64(v), vld1q_u8(pick)));
}

/* Four samples' 7-bit fields as two 28-bit values (samples 0-1, 2-3). */
static inline uint64x2_t iq_reduce_pair14(const uint32_t *w, int16x8_t m)
{
	uint32x4_t u = vreinterpretq_u32_s16(vandq_s16(iq_reduce_q4(w, m), vdupq_n_s16(0x7f)));
	uint64x2_t l;

	u = vandq_u32(vorrq_u32(u, vshrq_n_u32(u, 9)), vdupq_n_u32(0x3fff));	/* Q | I << 7 */
	l = vreinterpretq_u64_u32(u);
	return vandq_u64(vorrq_u64(l, vshrq_n_u64(l, 18)), vdupq_n_u64(0x0fffffff));
}

/* Four samples' 11-level codes as two 14-bit values (samples 0-1, 2-3). */
static inline uint64x2_t iq_reduce_pair7(const uint32_t *w, int16x8_t m, int16x8_t weights)
{
	int32x4_t c = vaddq_s32(vpaddlq_s16(vmulq_s16(iq_reduce_q4(w, m), weights)), vdupq_n_s32(60));
	uint64x2_t l = vreinterpretq_u64_s32(c);

	return vandq_u64(vorrq_u64(l, vshrq_n_u64(l, 25)), vdupq_n_u64(0x3fff));
}

/* [a0, a1], [b0, b1] -> [a0 | a1 << n, b0 | b1 << n] */
#define IQ_REDUCE_JOIN(a, b, n) \
	vorrq_u64(vuzp1q_u64(a, b), vshlq_n_u64(vuzp2q_u64(a, b), n))

static inline void iq_reduce_encode_neon(unsigned format, int m, const uint32_t *w, uint8_t *out)
{
	static const int16_t weights[8] = { 1, 11, 1, 11, 1, 11, 1, 11 };
	const int16x8_t mv = vdupq_n_s16((int16_t)m), wv = vld1q_s16(weights);
	uint32x4_t u, v;
	unsigned k;

	switch (format) {
	case 16:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 8, out += 16)
			vst1q_s8((int8_t *)out, vcombine_s8(vqmovn_s16(iq_reduce_q4(w + k, mv)),
							   vqmovn_s16(iq_reduce_q4(w + k + 4, mv))));
		break;
	case 14:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 8, out += 14)
			iq_reduce_store56(out, IQ_REDUCE_JOIN(iq_reduce_pair14(w + k, mv),
							      iq_reduce_pair14(w + k + 4, mv), 28));
		break;
	case 8:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 8, out += 8) {
			u = vreinterpretq_u32_s16(vandq_s16(iq_reduce_q4(w + k, mv), vdupq_n_s16(15)));
			v = vreinterpretq_u32_s16(vandq_s16(iq_reduce_q4(w + k + 4, mv), vdupq_n_s16(15)));
			u = vorrq_u32(u, vshrq_n_u32(u, 12));	/* low byte: Q | I << 4 */
			v = vorrq_u32(v, vshrq_n_u32(v, 12));
			vst1_u8(out, vmovn_u16(vcombine_u16(vmovn_u32(u), vmovn_u32(v))));
		}
		break;
	case 7:
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 16, out += 14) {
			uint64x2_t a = IQ_REDUCE_JOIN(iq_reduce_pair7(w + k, mv, wv),
						      iq_reduce_pair7(w + k + 4, mv, wv), 14);
			uint64x2_t b = IQ_REDUCE_JOIN(iq_reduce_pair7(w + k + 8, mv, wv),
						      iq_reduce_pair7(w + k + 12, mv, wv), 14);

			iq_reduce_store56(out, IQ_REDUCE_JOIN(a, b, 28));
		}
		break;
	}
}
#endif

/* One raw slice (2048 words) to `format` at out[0 .. iq_reduce_bytes(format));
 * `out` has IQ_REDUCE_SLACK bytes beyond that. Returns the scale code. */
static inline unsigned iq_reduce_encode(unsigned format, const uint32_t *words, uint8_t *out)
{
	unsigned code;

#ifdef IQ_REDUCE_NEON
	code = iq_reduce_scale(iq_reduce_peak_neon(words), iq_reduce_range(format));
	iq_reduce_encode_neon(format, iq_reduce_m[code], words, out);
#else
	code = iq_reduce_scale(iq_reduce_peak_scalar(words), iq_reduce_range(format));
	iq_reduce_encode_scalar(format, iq_reduce_m[code], words, out);
#endif
	return code;
}

/* The decoder's step under a scale code, in 1/1024ths. */
static inline int32_t iq_reduce_step(unsigned code)
{
	return (int32_t)((8192u * 1024u + iq_reduce_m[code] / 2) / iq_reduce_m[code]);
}

/* q back to a 14-bit field. */
static inline uint32_t iq_reduce_field(int q, int32_t step)
{
	int32_t x = (q * step + 512) >> 10;

	x = x > 8191 ? 8191 : x < -8192 ? -8192 : x;
	return (uint32_t)x & 0x3fffu;
}

static inline uint64_t iq_reduce_get56(const uint8_t *in)
{
	uint64_t v = 0;

	memcpy(&v, in, 7);	/* little-endian hosts */
	return v;
}

/* One reduced slice back to 2048 sample words. */
static inline void iq_reduce_decode(unsigned format, unsigned code, const uint8_t *in, uint32_t *words)
{
	uint32_t lut[256];
	int32_t step;
	uint64_t v;
	unsigned k, j;

	step = iq_reduce_step(code > IQ_REDUCE_SCALE_MAX ? IQ_REDUCE_SCALE_MAX : code);
	switch (format) {
	case 16:
		for (k = 0; k < 256; k++)
			lut[k] = iq_reduce_field((int8_t)k, step);
		for (k = 0; k < IQ_REDUCE_SAMPLES; k++)
			words[k] = lut[in[2 * k + 1]] << 16 | lut[in[2 * k]];
		break;
	case 14:
		for (k = 0; k < 128; k++)
			lut[k] = iq_reduce_field((int)(k ^ 64) - 64, step);
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 4, in += 7) {
			v = iq_reduce_get56(in);
			for (j = 0; j < 4; j++, v >>= 14)
				words[k + j] = lut[(v >> 7) & 0x7f] << 16 | lut[v & 0x7f];
		}
		break;
	case 8:
		for (k = 0; k < 256; k++)
			lut[k] = iq_reduce_field((int)((k >> 4) ^ 8) - 8, step) << 16 |
				 iq_reduce_field((int)((k & 15) ^ 8) - 8, step);
		for (k = 0; k < IQ_REDUCE_SAMPLES; k++)
			words[k] = lut[in[k]];
		break;
	case 7:
		for (k = 0; k < 128; k++)
			lut[k] = k > 120 ? 0 : iq_reduce_field((int)(k / 11) - 5, step) << 16 |
						iq_reduce_field((int)(k % 11) - 5, step);
		for (k = 0; k < IQ_REDUCE_SAMPLES; k += 8, in += 7) {
			v = iq_reduce_get56(in);
			for (j = 0; j < 8; j++, v >>= 7)
				words[k + j] = lut[v & 0x7f];
		}
		break;
	}
}

#endif /* HW_MT7996_IQ_REDUCE_H */
