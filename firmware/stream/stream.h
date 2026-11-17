/* SPDX-License-Identifier: MIT */
/* Shared by the WM producer and the WA packer. A packing schedule repeats one pattern every
 * PERIOD chunks: which positions the WA packs, which the WM packs, the rest going out raw. The
 * schedules come from -D (stream/blob.py): PERIOD (a power of two, at most 32) and SCHEDULES, one
 * struct sched per RADIO_PACKING_* code (radio/radio_cmd.h) in that order. A stream runs the one
 * its STREAM command names: our WM passes the code to the producer, which leaves it in the shared
 * block for the WA's packer. Everything else is in layout.h and the hw/ headers.
 */
#include <stdint.h>

#include "hw/mt7996/icap.h"
#include "hw/mt7996/iq_format.h"
#include "hw/mt7996/mcu_map.h"
#include "hw/mt7996/mcu_periph.h"

#include "layout.h"

#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define R16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define CHUNK MT7996_IQ_CHUNK_BYTES
#define PACKED MT7996_IQ_CHUNK_PACKED_BYTES
#define REDUCED MT7996_IQ_CHUNK_RADIO14_BYTES		/* a radio-reduced chunk: its data, then the trailer */
#define REDUCED_DATA MT7996_IQ_RADIO14_DATA_BYTES

/* The producer's results (RES_ADDR; host/w1700k/stream.py RES32 / RES64 mirror the order). */
struct res {
	uint32_t status, cpu_end, tx_dma_end, cr9_end, cr0_end, sent_after, done_after, err_status;
	uint32_t go_wait_cycles, first_cr9, wa_status, pack_max_cycles, max_lag, wa_pack_max, wa_sp, wa_ra;
	uint32_t wa_exit_cycles, ctl_served;
	uint64_t chunks, cycles, written, credit_waits, wa_waits, wm_waits, wm_packs, wm_pack_cycles;
	uint64_t wa_packs, wa_pack_cycles, wa_idle;
};
#define RES ((volatile struct res *)RES_ADDR)
#define RES_WA_EXITED	(1u << 8)	/* wa_status: the WA left the packer */

/* What the cores tell each other, in the WA's ILM: PMA entry 0 makes it uncached for both. */
struct shared {
	uint32_t started, go, stop, exited;	/* WA up, WM's go and stop, WA gone */
	uint32_t done;			/* every WA chunk below this index is packed */
	uint32_t published;		/* the WM's publish point */
	uint32_t sent;			/* chunks below this index have left TX0 */
	uint32_t packing;		/* the stream's schedule (RADIO_PACKING_*), set before the WA is raised */
	uint32_t status;		/* WA: 1 stopped, 2 go never came, 3 the WM stalled, 4 no schedule */
	uint32_t pack_max;
	uint64_t packs, pack_cycles, waits;
	uint32_t shift;			/* the WM's copy of the host's shift word, for the chunks the WA reduces */
};
#define SH ((volatile struct shared *)SHARED)

/* One packing schedule. */
struct sched {
	uint32_t wa_mask, wm_mask;	/* the positions (bit k % PERIOD) the WA and the WM pack */
	uint32_t wa_n, wm_n;		/* how many each */
	uint32_t reduce;		/* 1: those chunks are reduced to 14 bits (REDUCED bytes), 0: packed to 28 */
	uint8_t slot[PERIOD];		/* a packed position's index among its packer's */
};

static const struct sched schedules[] = { SCHEDULES };
#define SCHEDULE_COUNT	(sizeof(schedules) / sizeof(schedules[0]))
static const uint32_t wm_bufs[WM_NBUF] = { WM_BUFS };

/* Chunk k's job: 2 the WA packs it, 1 the WM does, 0 raw. Indices are modular (2^32 chunks
 * is 7.6 hours) and PERIOD divides 2^32, so the pattern and the buffers run on across the wrap.
 */
static inline int role(const struct sched *s, uint32_t k)
{
	uint32_t b = 1u << (k % PERIOD);

	return s->wa_mask & b ? 2 : s->wm_mask & b ? 1 : 0;
}

/* The next chunk after c in `mask`, which must not be empty. */
static inline uint32_t next_of(uint32_t c, uint32_t mask)
{
	do
		c++;
	while (!(mask >> (c % PERIOD) & 1));
	return c;
}

/* Chunk k's index among its packer's chunks, n of every PERIOD. */
static inline uint32_t packer_index(const struct sched *s, uint32_t k, uint32_t n)
{
	return (k / PERIOD) * n + s->slot[k % PERIOD];
}

/* Where a packed chunk goes: buffers in the packer's own local memory, written at local speed
 * and read by TX0 straight from there, so packing costs the pool only the read. The
 * cores' path into the pool is the limit: with both packers writing into it, both slowed by
 * half and the capture lapped the reader. A buffer is reused once TX0 has sent its chunk.
 */
static inline uint32_t wm_buffer(const struct sched *s, uint32_t k)
{
	return wm_bufs[packer_index(s, k, s->wm_n) % WM_NBUF];
}

static inline uint32_t wa_buffer(const struct sched *s, uint32_t k)
{
	return WA_BUF + packer_index(s, k, s->wa_n) % WA_NBUF * PACKED;
}

static inline uint32_t ring_at(uint32_t k)
{
	return MT7996_MCU_POOL_BASE + ((k * CHUNK) & (RING - 1));
}

static inline uint32_t rdcycle(void)
{
	uint32_t v;

	asm volatile("csrr %0, mcycle" : "=r"(v));
	return v;
}

static inline uint64_t rdcycle64(void)
{
	uint32_t hi, lo, hi2;

	do {
		asm volatile("csrr %0, mcycleh" : "=r"(hi));
		asm volatile("csrr %0, mcycle" : "=r"(lo));
		asm volatile("csrr %0, mcycleh" : "=r"(hi2));
	} while (hi != hi2);
	return (uint64_t)hi << 32 | lo;
}

/* Bytes ICAP has written since its start, from CR9 (the last beat, 4-byte units). A lap is
 * 819 us at 1.28 GB/s (the 1 MiB ring at 320 Msps); both cores read it far more often than that.
 */
static inline uint64_t icap_written(uint64_t *lap, uint32_t *prev)
{
	uint32_t last = MT7996_ICAP_CR9_NEXT_BYTE(R32(MT7996_ICAP_CR9(0)), RING);

	if (last < *prev)
		*lap += RING;
	*prev = last;
	return *lap + last;
}

/* D-cache: a packer reads the ring through it, so a line it holds would go stale when ICAP
 * comes round again a lap (819 us) later. Dropping every line each INVAL_EVERY chunks (410 us)
 * keeps that from happening; the outputs are in uncached local memory and need nothing. A
 * packer reads only chunks whose successor is written too, so even a line the prefetcher
 * fetched past the end holds this lap's data.
 */
#define INVAL_EVERY 64u

static inline void cache_wbinval_all(void)
{
	asm volatile("csrwi %0, %1\n\tfence"
		     : : "n"(MT7996_MCU_CSR_MCCTLCOMMAND), "n"(MT7996_MCU_CCTL_L1D_WBINVAL_ALL) : "memory");
}

void pack_words(uint32_t src, uint32_t dst, uint32_t bytes);
void reduce14_words(uint32_t src, uint32_t dst, uint32_t bytes, uint32_t shift);

/* The host's shift for reduced chunks (the mailbox word, 0..7). */
static inline uint32_t host_shift(void)
{
	return R32(MT7996_PROBE_WM_SHIFT) & MT7996_IQ_RADIO14_SHIFT_MAX;
}

/* A reduced chunk's last word: it names the shift its samples were cut under. */
static inline void reduced_trailer(uint32_t buffer, uint32_t shift)
{
	R32(buffer + REDUCED_DATA) = MT7996_IQ_RADIO14_TRAILER | shift;
}
