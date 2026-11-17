// SPDX-License-Identifier: MIT
// The publisher's conversions between sample words (14-bit I in bits [29:16], Q in [13:0]), the
// correction filter's floats and the shared ring's int16 pairs (I then Q). Each has an SSE2 form
// (every x86-64 has it) and a plain one for other hosts; both give the same values, bit for bit.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>

#if defined(__SSE2__)
#include <emmintrin.h>
#endif

inline int16_t sample_i(uint32_t w) { return int16_t(int32_t(w << 2) >> 18); }   // bits [29:16], sign-extended
inline int16_t sample_q(uint32_t w) { return int16_t(int32_t(w << 18) >> 18); }  // bits [13:0]

// `count` words to int16 I, Q pairs.
inline void words_to_int16(const uint32_t *words, unsigned count, int16_t *out)
{
    unsigned k = 0;
#if defined(__SSE2__)
    const __m128i low = _mm_set1_epi32(0xffff);
    for (; k + 4 <= count; k += 4) {
        const __m128i w = _mm_loadu_si128(reinterpret_cast<const __m128i *>(words + k));
        const __m128i i = _mm_srai_epi32(_mm_slli_epi32(w, 2), 18), q = _mm_srai_epi32(_mm_slli_epi32(w, 18), 18);
        // little-endian: I in the low half of each 32 bits, Q in the high half
        _mm_storeu_si128(reinterpret_cast<__m128i *>(out + 2 * k), _mm_or_si128(_mm_and_si128(i, low), _mm_slli_epi32(q, 16)));
    }
#endif
    for (; k < count; ++k) {
        out[2 * k] = sample_i(words[k]);
        out[2 * k + 1] = sample_q(words[k]);
    }
}

// `count` words to float I, Q pairs.
inline void words_to_float(const uint32_t *words, unsigned count, float *out)
{
    unsigned k = 0;
#if defined(__SSE2__)
    for (; k + 4 <= count; k += 4) {
        const __m128i w = _mm_loadu_si128(reinterpret_cast<const __m128i *>(words + k));
        const __m128 i = _mm_cvtepi32_ps(_mm_srai_epi32(_mm_slli_epi32(w, 2), 18));
        const __m128 q = _mm_cvtepi32_ps(_mm_srai_epi32(_mm_slli_epi32(w, 18), 18));
        _mm_storeu_ps(out + 2 * k, _mm_unpacklo_ps(i, q));
        _mm_storeu_ps(out + 2 * k + 4, _mm_unpackhi_ps(i, q));
    }
#endif
    for (; k < count; ++k) {
        out[2 * k] = sample_i(words[k]);
        out[2 * k + 1] = sample_q(words[k]);
    }
}

// `count` floats rounded to the nearest int16 (halves to even), saturating; returns how many
// saturated. The values are far inside the int32 range (14-bit samples under a capped gain).
inline uint64_t floats_to_int16(const float *in, unsigned count, int16_t *out)
{
    uint64_t clipped = 0;
    unsigned k = 0;
#if defined(__SSE2__)
    const __m128i high = _mm_set1_epi32(32767), low = _mm_set1_epi32(-32768);
    for (; k + 8 <= count; k += 8) {
        const __m128i a = _mm_cvtps_epi32(_mm_loadu_ps(in + k)), b = _mm_cvtps_epi32(_mm_loadu_ps(in + k + 4));
        _mm_storeu_si128(reinterpret_cast<__m128i *>(out + k), _mm_packs_epi32(a, b));  // saturates
        // One bit per value outside the int16 range: the eight 16-bit halves that saturating to
        // 16 bits leaves of the two masks.
        const __m128i outside = _mm_packs_epi32(_mm_or_si128(_mm_cmpgt_epi32(a, high), _mm_cmplt_epi32(a, low)),
                                                _mm_or_si128(_mm_cmpgt_epi32(b, high), _mm_cmplt_epi32(b, low)));
        const int mask = _mm_movemask_epi8(outside);  // two bits per value outside
        if (mask) clipped += unsigned(__builtin_popcount(unsigned(mask))) / 2;
    }
#endif
    for (; k < count; ++k) {
        const float v = std::nearbyint(in[k]);
        const float c = std::min(std::max(v, -32768.0f), 32767.0f);
        clipped += c != v;
        out[k] = int16_t(c);
    }
    return clipped;
}
