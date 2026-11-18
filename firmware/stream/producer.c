// SPDX-License-Identifier: MIT
/* The WM's stream producer, entered by our WM's STREAM command with the packing schedule's code
 * (RADIO_PACKING_*, stream.h schedules), interrupts off for its whole run.
 *
 * 1. Wait for the host's go word, check MCU_DMA0 TX0 idle and the firmware's ring adapter in
 *    step, start the WA packer if the schedule has one, then reset ICAP0 and start it as a
 *    ring over the first RING bytes of the ICAP partition.
 * 2. Follow CR9 and publish every chunk on TX0, in order, at most 7 in flight on its 8
 *    entries: a raw chunk straight from the ring, a packed one (7168 bytes) from its packer's
 *    buffer once that packer is past it. Whenever the next chunk cannot go yet, run a slice of
 *    an RF control program (ctl.h) or pack a piece of the WM's own next chunk.
 * 3. Stop on the host's stop word (written where the go word was) or on an overrun (the
 *    capture within SAFETY bytes of a chunk not yet published). There is no deadline: the
 *    host's stream pump writes the stop word whenever it ends.
 * 4. Let TX0 drain with ICAP still running, stop ICAP, stop the WA, give the firmware's
 *    adapter what was published, and reclaim the last few entries through ROM.
 * Counts that can pass 2^32 are 64-bit; chunk indices are modular. Results go to RES.
 *
 * status: 2 go word never came, 3 overrun, 4 TX0 busy at start, 5 adapter out of step,
 * 6 TX0 did not drain, 7 stopped by the host, 8 WA did not start, 9 WA did not exit,
 * 10 WA dispatch not at rest.
 */
#include "hw/mt7996/mcu_dma.h"
#include "hw/mt7996/mcu_irq.h"

#include "stream.h"
#include "ctl.h"


#define PIECE (CHUNK / PIECES)
#define PIECE_PACKED (PIECE / MT7996_IQ_PACK_GROUP_IN * MT7996_IQ_PACK_GROUP_OUT)
#define PIECE_REDUCED (PIECE / 16 * 7)

static void rom_reclaim(void)
{
	uint32_t saved;

	asm volatile("mv %0, gp" : "=r"(saved));
	asm volatile("li gp, %[g]\n\tli a0, 0\n\tli a1, 0\n\tjalr ra, 0(%[f])\n\tmv gp, %[s]"
		     : : [g] "i"(MT7996_WM_ROM_GP), [f] "r"(MT7996_MCU_ROM_DMA_RECLAIM), [s] "r"(saved)
		     : "ra", "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "t0", "t1", "t2", "t3", "t4",
		       "t5", "t6", "memory");
}

/* The WA enters through bit 1 of the WM->WA soft interrupt (no firmware sender uses that bit),
 * raised from here once the host's go word came; its packer holds the WA until our stop.
 * Afterwards the firmware's command path is dead until the next load, every time, so the host
 * reads the results through the BAR.
 */
static int start_wa(uint32_t packing, uint32_t wa_mask)
{
	uint32_t g0;

	if (R32(MT7996_MCU_WA_SWI_SLOT1) != MT7996_MCU_WA_SWI_SLOT1_STOCK || R32(MT7996_MCU_SWDEF_WA_ACTION) ||
	    R32(MT7996_MCU_WA_SWI))
		return 10;			/* the WA's dispatch is not at rest */
	SH->started = 0;
	SH->go = 0;
	SH->stop = 0;
	SH->exited = 0;
	SH->done = next_of(~0u, wa_mask);
	SH->published = 0;
	SH->sent = 0;
	SH->packing = packing;
	SH->shift = MT7996_IQ_RADIO14_SHIFT_MAX;	/* until the first relay: the shift that cannot wrap */
	R32(MT7996_MCU_WA_SWI_SLOT1) = WA_CODE;
	/* The interrupt word before the action word. Our WA polls the action word and, when it
	 * finds the bit, clears the interrupt word (wa.c). With the action written first, a WA
	 * that saw it between our two writes cleared the interrupt word before we raised it, and
	 * it then stayed raised: every later start found the dispatch "not at rest" (10) until a
	 * reload. */
	R32(MT7996_MCU_WA_SWI) = MT7996_MCU_WA_SWI_RAISE;
	R32(MT7996_MCU_SWDEF_WA_ACTION) = MT7996_MCU_SWDEF_WA_ACTION_SLOT1;
	g0 = rdcycle();
	while (!SH->started)
		if (rdcycle() - g0 > MT7996_MCU_CLOCK_HZ / 10)	/* 100 ms */
			break;
	R32(MT7996_MCU_WA_SWI_SLOT1) = MT7996_MCU_WA_SWI_SLOT1_STOCK;
	RES->wa_sp = R32(WA_STACK_TOP - 16);	/* wa_lead keeps the caller's sp and ra there */
	RES->wa_ra = R32(WA_STACK_TOP - 12);
	return SH->started ? 0 : 8;
}

__attribute__((used)) void producer(uint32_t packing)
{
	/* The caller checked the code. The masks live in registers; the slot table is read per chunk. */
	const struct sched *s = &schedules[packing];
	const uint32_t wa_mask = s->wa_mask, wm_mask = s->wm_mask, wm_n = s->wm_n;
	volatile struct res *r = RES;
	uint32_t mstatus, mcache, status = 0, cpu = 0, desc = 0, slots = 0, g0, prev = 0, k = 0, m, wc;
	uint32_t piece = 0, addr, len, t, pack_start = 0, max_lag = 0, pmax = 0, sent, inval_at = 0;
	uint32_t shift = MT7996_IQ_RADIO14_SHIFT_MAX;
	const uint32_t packed_len = s->reduce ? REDUCED : PACKED;
	uint32_t held[WM_NBUF];	/* the chunk each WM buffer last held, until TX0 is past it */
	uint64_t lap = 0, written = 0, chunks = 0, start = 0, lag;
	uint64_t credit_waits = 0, wa_waits = 0, wm_waits = 0, wm_packs = 0, wm_cycles = 0;
	struct ctl ctl;
	int i, ready;

	asm volatile("csrr %0, mstatus\n\tcsrci mstatus, %1" : "=r"(mstatus) : "n"(MT7996_MCU_MSTATUS_MIE));
	asm volatile("csrr %0, %1" : "=r"(mcache) : "n"(MT7996_MCU_CSR_MCACHE_CTL));
	for (i = 0; i < (int)(sizeof(struct res) / 4); i++)
		((volatile uint32_t *)r)[i] = 0;
	if (wm_mask) {
		asm volatile("csrs %0, %1" : : "n"(MT7996_MCU_CSR_MCACHE_CTL), "r"(MT7996_MCU_MCACHE_CTL_STREAM));
		cache_wbinval_all();
	}

	/* 1. */
	g0 = rdcycle();
	while (R32(GO_ADDR) != GO_MAGIC)
		if (rdcycle() - g0 > GO_CYCLES) {
			status = 2;
			goto finish;
		}
	r->go_wait_cycles = rdcycle() - g0;
	R32(GO_ADDR) = 0;
	ctl_init(&ctl);
	cpu = R32(MT7996_MCU_DMA0_TX0_CIDX);
	if (cpu != R32(MT7996_MCU_DMA0_TX0_DIDX)) {
		status = 4;
		goto finish;
	}
	if (cpu != R32(MT7996_MCU_DMA0_TX0_ADAPTER + MT7996_MCU_ADAPTER_CPU)) {
		status = 5;
		goto finish;
	}
	desc = R32(MT7996_MCU_DMA0_TX0_BASE);
	slots = R32(MT7996_MCU_DMA0_TX0_ADAPTER + MT7996_MCU_ADAPTER_SLOTS);
	if (wa_mask && (status = start_wa(packing, wa_mask)))
		goto finish;
	R32(MT7996_ICAP_CR0(0)) = MT7996_ICAP_CR0_HELD;	/* reset (clears CR9/WRAP), then ring + EN + CAPTURE */
	R32(MT7996_ICAP_CR0(0)) = MT7996_ICAP_CR0_RING_IDLE;
	R32(MT7996_ICAP_CR0(0)) = MT7996_ICAP_CR0_RING_ARM;
	R32(MT7996_ICAP_CR0(0)) = MT7996_ICAP_CR0_RING_TRIGGER;
	r->first_cr9 = R32(MT7996_ICAP_CR9(0));
	if (wa_mask)
		SH->go = 1;

	/* 2. k: the next chunk to publish; m: the first of the WM's next group to pack. */
	m = wm_mask ? next_of(~0u, wm_mask) : 0;
	for (i = 0; i < (int)WM_NBUF; i++)
		held[i] = m - (1u << 30);	/* far below every chunk: free */
	start = rdcycle64();
	for (;;) {
		if (R32(GO_ADDR) == STOP_MAGIC) {
			status = 7;
			break;
		}
		written = icap_written(&lap, &prev);
		lag = written - chunks * CHUNK;
		if (lag >= RING - SAFETY) {
			status = 3;
			break;
		}
		if ((uint32_t)lag > max_lag)
			max_lag = (uint32_t)lag;
		wc = (uint32_t)(written / CHUNK);
		/* what TX0 has finished */
		sent = k - (cpu - R32(MT7996_MCU_DMA0_TX0_DIDX)) % MT7996_MCU_DMA0_TX0_ENTRIES;
		if (wa_mask)
			SH->sent = sent;
		if ((int32_t)(wc - k) > 0) {
			addr = ring_at(k);
			len = CHUNK;
			ready = 1;
			switch (role(s, k)) {
			case 2:
				if ((int32_t)(SH->done - k) <= 0) {
					ready = 0;
					wa_waits++;
				}
				addr = wa_buffer(s, k);
				len = packed_len;
				break;
			case 1:
				if ((int32_t)(m - k) <= 0) {
					ready = 0;
					wm_waits++;
				}
				addr = wm_buffer(s, k);
				len = packed_len;
				break;
			}
			if (ready) {
				if ((cpu + 1) % MT7996_MCU_DMA0_TX0_ENTRIES != R32(MT7996_MCU_DMA0_TX0_DIDX)) {
					R32(slots + cpu * 4) = 0;		/* slot NULL */
					R32(desc + cpu * MT7996_MCU_TXD_SIZE + MT7996_MCU_TXD_W0_BUF0) = addr;
					/* w1[31:16]: LS0 | length, DDONE clear */
					R16(desc + cpu * MT7996_MCU_TXD_SIZE + MT7996_MCU_TXD_W1_HI) =
						(MT7996_MCU_TXD_LS0 | HW_PREP(MT7996_MCU_TXD_SDL0, len)) >> 16;
					asm volatile("fence w, w" ::: "memory");
					cpu = (cpu + 1) % MT7996_MCU_DMA0_TX0_ENTRIES;
					R32(MT7996_MCU_DMA0_TX0_CIDX) = cpu;
					k++;
					chunks++;
					if (wa_mask) {
						SH->published = k;
						if (s->reduce)
							SH->shift = host_shift();
					}
					continue;
				}
				credit_waits++;
			}
		}
		/* Nothing to publish: a slice of an RF control program while we are well ahead of
		 * the capture (a retune is ~1.5 ms of SPI, and with packing the WM has only ~18% to
		 * spare, so control waits rather than let the lag approach an overrun), then the
		 * D-cache, or a piece of the WM's next chunk once the chunk after it is written too
		 * (the prefetcher reads past the end) and TX0 has sent what its buffer held.
		 */
		ctl.chunk = (uint32_t)chunks;
		if ((uint32_t)lag < MT7996_PROBE_CTL_LAG_CHUNKS * CHUNK) {
			ctl_step(&ctl);
			if (ctl.stalled) {	/* a slice so long that a lap may have gone uncounted (ctl.h) */
				status = 3;
				break;
			}
		}
		if (wm_mask) {
			if ((int32_t)(wc - inval_at) >= (int32_t)INVAL_EVERY) {
				cache_wbinval_all();
				inval_at = wc;
				continue;
			}
			if ((int32_t)(wc - m) < 2)
				continue;
			if (!piece) {
				if ((int32_t)(sent - held[packer_index(s, m, wm_n) % WM_NBUF]) <= 0)
					continue;
				pack_start = rdcycle();
				shift = host_shift();		/* one shift for the whole chunk */
			}
			if (s->reduce)
				reduce14_words(ring_at(m) + piece * PIECE, wm_buffer(s, m) + piece * PIECE_REDUCED,
					       PIECE, shift);
			else
				pack_words(ring_at(m) + piece * PIECE, wm_buffer(s, m) + piece * PIECE_PACKED, PIECE);
			if (++piece < PIECES)
				continue;
			piece = 0;
			if (s->reduce)
				reduced_trailer(wm_buffer(s, m), shift);
			t = rdcycle() - pack_start;
			wm_cycles += t;
			if (t > pmax)
				pmax = t;
			wm_packs++;
			held[packer_index(s, m, wm_n) % WM_NBUF] = m;
			m = next_of(m, wm_mask);
		}
	}

	ctl_settle(&ctl);	/* a transaction of a program that is not going to be resumed */

	/* 3., 4. */
	r->cycles = rdcycle64() - start;
	r->chunks = chunks;
	r->written = written;
	r->max_lag = max_lag;
	r->credit_waits = credit_waits;
	r->wa_waits = wa_waits;
	r->wm_waits = wm_waits;
	r->wm_packs = wm_packs;
	r->wm_pack_cycles = wm_cycles;
	r->pack_max_cycles = pmax;
	r->ctl_served = ctl.served;
	r->ctl_slice_max = ctl.slice_max;
	r->ctl_slice_limit = ctl.slice_limit;
	for (t = SPIN; t; t--)			/* TX0 drains with ICAP still running */
		if (R32(MT7996_MCU_DMA0_TX0_DIDX) == cpu)
			break;
	if (!t)
		status = 6;
	r->tx_dma_end = R32(MT7996_MCU_DMA0_TX0_DIDX);
	/* The WA leaves the packer before ICAP stops. Stopping ICAP first left the WA stalled on a pool
	 * read inside pack_words for good in 2 of 9 stops; in this order 12 of 12 stopped cleanly. */
	if (wa_mask) {
		SH->stop = 1;
		g0 = rdcycle();
		while (!SH->exited)
			if (rdcycle() - g0 > MT7996_MCU_CLOCK_HZ) {	/* 1 s */
				if (status == 7)
					status = 9;
				break;
			}
		r->wa_exit_cycles = rdcycle() - g0;
		r->wa_status = SH->status | (SH->exited ? RES_WA_EXITED : 0);
		r->wa_packs = SH->packs;
		r->wa_pack_cycles = SH->pack_cycles;
		r->wa_pack_max = SH->pack_max;
		r->wa_idle = SH->waits;
	}
	R32(MT7996_ICAP_CR0(0)) = MT7996_ICAP_CR0_IDLE;	/* EN = 0; SW_RST_B stays 1 so CR9 is readable */
	r->cr9_end = R32(MT7996_ICAP_CR9(0));
	r->cr0_end = R32(MT7996_ICAP_CR0(0));
	/* The firmware learns what we published. ROM reclaim walks done..sent one entry at a time
	 * and does not return after millions of entries: every chunk completed and owns no buffer,
	 * so all but the last 8 are marked done here and it walks only those.
	 */
	r->cpu_end = cpu;
	R32(MT7996_MCU_DMA0_TX0_ADAPTER + MT7996_MCU_ADAPTER_CPU) = cpu;
	R32(MT7996_MCU_DMA0_TX0_ADAPTER + MT7996_MCU_ADAPTER_SENT) += (uint32_t)chunks;
	if (chunks >= MT7996_MCU_DMA0_TX0_ENTRIES)
		R32(MT7996_MCU_DMA0_TX0_ADAPTER + MT7996_MCU_ADAPTER_DONE) +=
			(uint32_t)chunks - MT7996_MCU_DMA0_TX0_ENTRIES;
	rom_reclaim();
	r->sent_after = R32(MT7996_MCU_DMA0_TX0_ADAPTER + MT7996_MCU_ADAPTER_SENT);
	r->done_after = R32(MT7996_MCU_DMA0_TX0_ADAPTER + MT7996_MCU_ADAPTER_DONE);
	r->err_status = R32(MT7996_MCU_DMA0_INT_STA);
finish:
	r->status = status;
	R32(DONE_AT) = DONE_MAGIC;
	R32(DONE_AT + 4) = status;
	/* After a WA run the host's register window breaks once the firmware resumes, so the host
	 * reads the results while we still hold the WM.
	 */
	if (wa_mask)
		for (t = rdcycle(); rdcycle() - t < HOLD_CYCLES;)
			;
	asm volatile("csrw %0, %1" : : "n"(MT7996_MCU_CSR_MCACHE_CTL), "r"(mcache));
	asm volatile("csrw mstatus, %0" : : "r"(mstatus));
}
