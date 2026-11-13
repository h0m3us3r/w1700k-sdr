// SPDX-License-Identifier: MIT
/* The WA's packer, called from the WA's soft-interrupt handler, which the WM's producer
 * raises after hooking our code into its table. It follows ICAP itself, packs every chunk
 * the schedule gives the WA into its DLM buffers as soon as the chunk after it is written
 * too, and moves `done` past it; the WM publishes. A buffer is reused only once TX0 has sent
 * the chunk it held. The schedule is the one the WM left in the shared block. It stops on the
 * WM's stop, or if the WM's publish point stands still for a second.
 */
#include "stream.h"

#define SECOND MT7996_MCU_CLOCK_HZ	/* in cycles */

__attribute__((used)) void packer(void)
{
	uint32_t mstatus, mcache, prev = 0, c, wc, t, t0, pub, seen, still, pmax = 0, status = 1, b, inval_at = 0;
	uint32_t held[WA_NBUF];	/* the chunk each buffer last held, until TX0 is past it */
	uint64_t lap = 0, written, packs = 0, cycles = 0, waits = 0;
	const struct sched *s;
	uint32_t wa_mask, wa_n, shift;

	asm volatile("csrr %0, mstatus\n\tcsrci mstatus, %1" : "=r"(mstatus) : "n"(MT7996_MCU_MSTATUS_MIE));
	asm volatile("csrr %0, %1" : "=r"(mcache) : "n"(MT7996_MCU_CSR_MCACHE_CTL));
	asm volatile("csrs %0, %1" : : "n"(MT7996_MCU_CSR_MCACHE_CTL), "r"(MT7996_MCU_MCACHE_CTL_STREAM));
	cache_wbinval_all();
	SH->status = 0;
	c = SH->packing;
	s = &schedules[c < SCHEDULE_COUNT ? c : 0];
	wa_mask = s->wa_mask;
	wa_n = s->wa_n;
	SH->started = 1;
	if (c >= SCHEDULE_COUNT || !wa_mask) {	/* nothing here for the WA to pack */
		status = 4;
		goto out;
	}
	t0 = rdcycle();
	while (!SH->go)
		if (SH->stop || rdcycle() - t0 > SECOND) {
			status = 2;
			goto out;
		}
	c = next_of(~0u, wa_mask);
	for (b = 0; b < WA_NBUF; b++)
		held[b] = c - (1u << 30);	/* far below every chunk: free */
	seen = 0;
	still = rdcycle();
	while (!SH->stop) {
		written = icap_written(&lap, &prev);
		wc = (uint32_t)(written / CHUNK);
		pub = SH->published;
		if (pub != seen) {
			seen = pub;
			still = rdcycle();
		}
		b = packer_index(s, c, wa_n) % WA_NBUF;
		if ((int32_t)(wc - c) < 2 || (int32_t)(SH->sent - held[b]) <= 0) {
			waits++;
			if (rdcycle() - still > SECOND) {
				status = 3;
				break;
			}
			continue;
		}
		if ((int32_t)(wc - inval_at) >= (int32_t)INVAL_EVERY) {
			cache_wbinval_all();
			inval_at = wc;
		}
		t0 = rdcycle();
		if (s->reduce) {
			shift = SH->shift & MT7996_IQ_RADIO14_SHIFT_MAX;
			reduce14_words(ring_at(c), wa_buffer(s, c), CHUNK, shift);
			reduced_trailer(wa_buffer(s, c), shift);
		} else {
			pack_words(ring_at(c), wa_buffer(s, c), CHUNK);
		}
		held[b] = c;
		t = rdcycle() - t0;
		cycles += t;
		if (t > pmax)
			pmax = t;
		packs++;
		c = next_of(c, wa_mask);
		asm volatile("fence w, w" ::: "memory");	/* the buffer lands before `done` says so */
		SH->done = c;
	}
out:
	SH->packs = packs;
	SH->pack_cycles = cycles;
	SH->pack_max = pmax;
	SH->waits = waits;
	SH->status = status;
	asm volatile("csrw %0, %1" : : "n"(MT7996_MCU_CSR_MCACHE_CTL), "r"(mcache));
	asm volatile("csrw mstatus, %0" : : "r"(mstatus));
	SH->exited = 1;
}
