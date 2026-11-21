/* SPDX-License-Identifier: MIT */
/* Spectrum occupancy beside a stream: now and then a chunk the producer has just published is
 * copied from the capture ring into DSP core 0's memory by MEM_DMA, the engine next to the one
 * that carries the stream, with no CPU touching a sample; the core is asked for its spectrum
 * and keeps the account (dsp/occ.c); and when it has written a report, every so many snapshots,
 * that is brought to the mailbox page, where the host reads it (hw/mt7996/probe_page.h
 * "Occupancy").
 *
 * The host turns it on by writing how many chunks apart the snapshots are to be at the least
 * (MT7996_PROBE_OCC_PERIOD; 0 is off). It runs for as long as a stream does and that word is not
 * 0, also across streams. Seen on a unit: no slice lost with it on at 40, 160 and 320 Msps, and
 * the producer at most 4 chunks behind the capture at 160 Msps (full) and 8 at 320 (split), where
 * it is 3 to 4 and 8 to 9 without. It got to about 100 snapshots a second at 40 Msps, 68 at 160
 * and 40 at 320. [measured]
 *
 * It is a step at a time, and a step is taken once in 1024 idle calls of the control interpreter
 * (ctl.h ctl_step, where the stream's rate is taken too): the producer's loop has no time to give
 * away at 320 Msps and is not touched, and no step waits. The steps: post the copy; see it done
 * and ask the core; see the core done; bring 16 words of a report out. A snapshot therefore takes
 * three steps, however fast the hardware is. The core needs 0.35 ms for one, and once, for the
 * 64th after it was turned on, about 10 ms more (it learns the noise's shape then).
 *
 * The chunk copied is the last one published. The capture is less than
 * MT7996_PROBE_CTL_LAG_CHUNKS chunks past it when a step runs, a lap of the ring is 128, and the
 * copy takes 7 us: the capture does not come round to it while it is read.
 *
 * MEM_DMA as tools/dsp found it to work (hw/mt7996/mcu_dma.h): eight descriptors a ring, the
 * receive ring's capacity published before the transmit ring is enabled, a descriptor's words
 * fenced before its index is written, and a copy is done when both rings' hardware indices have
 * moved AND the receive descriptor says so: the indices were seen to move first.
 *
 * HAZARD (hw/mt7996/dsp.h): a DSP core's memory is not to be touched while the core is held in
 * reset: the radio's bus hangs until a power cycle. Nothing here reaches core 0's memory before
 * the enable and reset words say it runs, and a firmware set without the DSP image leaves the
 * cores in reset: then this reports MT7996_PROBE_OCC_ST_NO_DSP and does nothing.
 *
 * Every access goes through occ_rd / occ_wr, so that tests/occ_check.c can run this on the host
 * against a model of the engine and the core.
 */
#ifndef STREAM_OCC_H
#define STREAM_OCC_H

#include "hw/mt7996/dsp.h"
#include "hw/mt7996/mcu_dma.h"
#include "hw/mt7996/probe_page.h"

#include "../dsp/occ_layout.h"

#ifndef occ_rd
#define occ_rd(a)	R32(a)
#define occ_wr(a, v)	(R32(a) = (v))
#define occ_now()	rdcycle()
#define occ_fence()	asm volatile("fence" ::: "memory")
#endif

struct occ {
	uint32_t state;		/* OCC_* */
	uint32_t from;		/* when the copy was posted, or the core asked */
	uint32_t slot;		/* the rings' next descriptor */
	uint32_t req;		/* the last request number given to the core */
	uint32_t seen;		/* the core's count of reports as last seen */
	uint32_t out;		/* words of a report brought out so far */
	uint32_t next;		/* the chunk from which the next snapshot may be taken */
	uint32_t lfsr;
	uint32_t snapshots, reports;
	uint32_t cfg;		/* MEM_DMA's configuration as it was found */
	uint32_t fresh;		/* the core has not been told to forget yet */
	uint32_t tries;
};

#define OCC_OFF		0u
#define OCC_WAIT	1u	/* until the stream is `next` chunks on */
#define OCC_COPY	2u	/* MEM_DMA has the copy */
#define OCC_ASKED	3u	/* the core has the snapshot */
#define OCC_OUT		4u	/* a report is being brought out */
#define OCC_DEAD	5u	/* it failed: until the host turns it off and on */

#define OCC_CORE	0u
#define OCC_DMA		MT7996_MEM_DMA_BASE
#define OCC_RING	8u
#define OCC_TXD(i)	(MT7996_PROBE_OCC_TX_RING + (i) * MT7996_MCU_TXD_SIZE)
#define OCC_RXD(i)	(MT7996_PROBE_OCC_RX_RING + (i) * MT7996_MCU_TXD_SIZE)
#define OCC_CORE_AT(off) (MT7996_DSP_MCU_DTCM(OCC_CORE) + (off))
#define OCC_PATIENCE	(MT7996_MCU_CLOCK_HZ / 10)	/* 100 ms for a copy, and for the core */
#define OCC_OUT_WORDS	16u				/* of a report, a step */
#define OCC_TRIES	64u				/* steps for the engine and the core to come to rest */

_Static_assert(OCC_SAMPLES * 4 == MT7996_PROBE_CHUNK_BYTES, "a snapshot is a chunk");
_Static_assert(OCC_REPORT_WORDS == MT7996_PROBE_OCC_REPORT_WORDS, "the report's size");
_Static_assert(MT7996_PROBE_OCC_RX_RING - MT7996_PROBE_OCC_TX_RING >= OCC_RING * MT7996_MCU_TXD_SIZE, "the rings");

static void occ_dma_off(const struct occ *o)
{
	occ_wr(MT7996_MCU_DMA_GLO_CFG(OCC_DMA), o->cfg & ~(MT7996_MCU_DMA_GLO_TX_DMA_EN | MT7996_MCU_DMA_GLO_RX_DMA_EN |
							  MT7996_MCU_DMA_GLO_TX_DMA_BUSY | MT7996_MCU_DMA_GLO_RX_DMA_BUSY));
}

static void occ_fail(struct occ *o, uint32_t why, int dma)
{
	if (dma)
		occ_dma_off(o);
	o->state = OCC_DEAD;
	occ_wr(MT7996_PROBE_OCC_STATE, why);
}

/* Off: the engine's queues stopped, as they were found. */
static void occ_stop(struct occ *o)
{
	if (o->state != OCC_OFF && o->state != OCC_DEAD)
		occ_dma_off(o);
	if (o->state != OCC_OFF)
		occ_wr(MT7996_PROBE_OCC_STATE, MT7996_PROBE_OCC_ST_OFF);
	o->state = OCC_OFF;
	o->tries = 0;
}

/* The first step: the core seen running our program and at rest, the engine seen idle, its rings
 * set up over the mailbox page's descriptors. A core or an engine that is busy is tried again at
 * the next step, OCC_TRIES times. */
static void occ_start(struct occ *o, uint32_t chunk)
{
	const uint32_t core = MT7996_DSP_CORE_BIT(OCC_CORE);
	const uint32_t busy = MT7996_MCU_DMA_GLO_TX_DMA_BUSY | MT7996_MCU_DMA_GLO_RX_DMA_BUSY;
	uint32_t cfg, i;

	if (!(occ_rd(MT7996_DSP_CORE_ENABLE) & core) || (occ_rd(MT7996_DSP_CORE_RESET) & core) ||
	    occ_rd(OCC_CORE_AT(OCC_BOX_MAGIC)) != OCC_MAGIC ||
	    occ_rd(OCC_CORE_AT(OCC_BOX_STATE)) != OCC_STATE_RUNNING) {
		occ_fail(o, MT7996_PROBE_OCC_ST_NO_DSP, 0);
		return;
	}
	o->req = occ_rd(OCC_CORE_AT(OCC_REQ));
	cfg = occ_rd(MT7996_MCU_DMA_GLO_CFG(OCC_DMA));
	if (occ_rd(OCC_CORE_AT(OCC_DONE)) != o->req || (cfg & busy) ||
	    occ_rd(MT7996_MCU_DMA_TX_CIDX(OCC_DMA, 0)) != occ_rd(MT7996_MCU_DMA_TX_DIDX(OCC_DMA, 0))) {
		if (++o->tries >= OCC_TRIES)
			occ_fail(o, occ_rd(OCC_CORE_AT(OCC_DONE)) != o->req ? MT7996_PROBE_OCC_ST_DSP_SILENT :
				    MT7996_PROBE_OCC_ST_DMA_BUSY, 0);
		return;
	}
	o->cfg = cfg;
	occ_dma_off(o);
	for (i = 0; i < OCC_RING; i++) {
		occ_wr(OCC_TXD(i) + MT7996_MCU_TXD_W0_BUF0, 0);
		occ_wr(OCC_TXD(i) + MT7996_MCU_TXD_W1_CTRL, MT7996_MCU_TXD_DDONE);
		occ_wr(OCC_TXD(i) + MT7996_MCU_TXD_W2_BUF1, 0);
		occ_wr(OCC_TXD(i) + MT7996_MCU_TXD_W3_INFO, 0);
		occ_wr(OCC_RXD(i) + MT7996_MCU_TXD_W0_BUF0, OCC_CORE_AT(OCC_SAMPLE_AT));
		occ_wr(OCC_RXD(i) + MT7996_MCU_TXD_W1_CTRL, HW_PREP(MT7996_MCU_TXD_SDL0, MT7996_PROBE_CHUNK_BYTES));
		occ_wr(OCC_RXD(i) + MT7996_MCU_TXD_W2_BUF1, 0);
		occ_wr(OCC_RXD(i) + MT7996_MCU_TXD_W3_INFO, 0);
	}
	occ_fence();
	occ_wr(MT7996_MCU_DMA_TX_BASE(OCC_DMA, 0), MT7996_PROBE_OCC_TX_RING);
	occ_wr(MT7996_MCU_DMA_TX_CNT(OCC_DMA, 0), OCC_RING);
	occ_wr(MT7996_MCU_DMA_TX_CIDX(OCC_DMA, 0), 0);
	occ_wr(MT7996_MCU_DMA_RX_BASE(OCC_DMA, 0), MT7996_PROBE_OCC_RX_RING);
	occ_wr(MT7996_MCU_DMA_RX_CNT(OCC_DMA, 0), OCC_RING);
	occ_wr(MT7996_MCU_DMA_RX_CIDX(OCC_DMA, 0), 0);
	occ_wr(MT7996_MCU_DMA_RST_DTX_PTR(OCC_DMA), 1);
	occ_wr(MT7996_MCU_DMA_RST_DRX_PTR(OCC_DMA), 1);
	occ_wr(MT7996_MCU_DMA_RX_CIDX(OCC_DMA, 0), OCC_RING - 1);
	occ_wr(MT7996_MCU_DMA_GLO_CFG(OCC_DMA),
	       (cfg & ~busy) | MT7996_MCU_DMA_GLO_TX_DMA_EN | MT7996_MCU_DMA_GLO_RX_DMA_EN);
	o->slot = 0;
	o->seen = occ_rd(OCC_CORE_AT(OCC_REPORTS));
	o->snapshots = 0;
	o->reports = 0;
	o->fresh = 1;
	o->next = chunk;
	o->state = OCC_WAIT;
	occ_wr(MT7996_PROBE_OCC_SNAPSHOTS, 0);
	occ_wr(MT7996_PROBE_OCC_SEQ, 0);
	occ_wr(MT7996_PROBE_OCC_STATE, MT7996_PROBE_OCC_ST_RUNNING);
}

/* The next snapshot: `period` chunks on, give or take half of that, so that what repeats in the
 * air (a beacon every 102.4 ms is every 8000th chunk at 160 Msps) is not always met or always
 * missed. */
static void occ_wait(struct occ *o, uint32_t chunk, uint32_t period)
{
	o->lfsr = o->lfsr >> 1 ^ (-(o->lfsr & 1u) & 0xd0000001u);
	o->next = chunk + (period + 1) / 2 + o->lfsr % period;
	o->state = OCC_WAIT;
}

/* One step. chunk: how many chunks the stream has published (0: no stream publishes). */
static __attribute__((noinline)) void occ_step(struct occ *o, uint32_t chunk)
{
	const uint32_t period = occ_rd(MT7996_PROBE_OCC_PERIOD), slot = o->slot, next = (slot + 1) % OCC_RING;
	uint32_t v, i;

	if (!period) {
		occ_stop(o);
		return;
	}
	if (!chunk)
		return;
	switch (o->state) {
	case OCC_OFF:
		occ_start(o, chunk);
		return;
	case OCC_WAIT:
		if ((int32_t)(chunk - o->next) < 0)
			return;
		occ_wr(OCC_TXD(slot) + MT7996_MCU_TXD_W0_BUF0, ring_at(chunk - 1));
		occ_wr(OCC_TXD(slot) + MT7996_MCU_TXD_W1_CTRL,
		       MT7996_MCU_TXD_LS0 | HW_PREP(MT7996_MCU_TXD_SDL0, MT7996_PROBE_CHUNK_BYTES));
		occ_fence();
		occ_wr(MT7996_MCU_DMA_TX_CIDX(OCC_DMA, 0), next);
		o->from = occ_now();
		o->state = OCC_COPY;
		return;
	case OCC_COPY:
		v = occ_rd(OCC_RXD(slot) + MT7996_MCU_TXD_W1_CTRL);
		if (occ_rd(MT7996_MCU_DMA_TX_DIDX(OCC_DMA, 0)) != next ||
		    occ_rd(MT7996_MCU_DMA_RX_DIDX(OCC_DMA, 0)) != next || !(v & MT7996_MCU_TXD_DDONE)) {
			if (occ_now() - o->from > OCC_PATIENCE)
				occ_fail(o, MT7996_PROBE_OCC_ST_DMA_FAILED, 1);
			return;
		}
		if (HW_GET(MT7996_MCU_TXD_SDL0, v) != MT7996_PROBE_CHUNK_BYTES) {
			occ_fail(o, MT7996_PROBE_OCC_ST_DMA_FAILED, 1);
			return;
		}
		occ_wr(OCC_RXD(slot) + MT7996_MCU_TXD_W1_CTRL, HW_PREP(MT7996_MCU_TXD_SDL0, MT7996_PROBE_CHUNK_BYTES));
		occ_fence();
		occ_wr(MT7996_MCU_DMA_RX_CIDX(OCC_DMA, 0), slot);
		o->slot = next;
		occ_wr(OCC_CORE_AT(OCC_FLAGS), o->fresh ? OCC_FLAG_RESET : 0);
		occ_wr(OCC_CORE_AT(OCC_THRESHOLD), occ_rd(MT7996_PROBE_OCC_THRESHOLD));
		occ_wr(OCC_CORE_AT(OCC_EVERY), occ_rd(MT7996_PROBE_OCC_EVERY));
		occ_wr(OCC_CORE_AT(OCC_REQ), ++o->req);
		o->from = occ_now();
		o->state = OCC_ASKED;
		return;
	case OCC_ASKED:
		if (occ_rd(OCC_CORE_AT(OCC_DONE)) != o->req) {
			if (occ_now() - o->from > OCC_PATIENCE)
				occ_fail(o, MT7996_PROBE_OCC_ST_DSP_SILENT, 1);
			return;
		}
		o->fresh = 0;
		occ_wr(MT7996_PROBE_OCC_SNAPSHOTS, ++o->snapshots);
		v = occ_rd(OCC_CORE_AT(OCC_REPORTS));
		if (v == o->seen) {
			occ_wait(o, chunk, period);
			return;
		}
		o->seen = v;		/* that snapshot completed a report */
		o->out = 0;
		occ_wr(MT7996_PROBE_OCC_SEQ, 2 * o->reports + 1);	/* odd: being written */
		o->state = OCC_OUT;
		return;
	case OCC_OUT:
		for (i = 0; i < OCC_OUT_WORDS && o->out < OCC_REPORT_WORDS; i++, o->out++)
			occ_wr(MT7996_PROBE_OCC_REPORT + 4 * o->out, occ_rd(OCC_CORE_AT(OCC_REPORT) + 4 * o->out));
		if (o->out < OCC_REPORT_WORDS)
			return;
		occ_fence();
		occ_wr(MT7996_PROBE_OCC_SEQ, 2 * ++o->reports);
		occ_wait(o, chunk, period);
		return;
	}
}

/* A stream's end: the engine stopped and seen at rest, so that the next stream finds it so. */
static void occ_settle(struct occ *o)
{
	const uint32_t busy = MT7996_MCU_DMA_GLO_TX_DMA_BUSY | MT7996_MCU_DMA_GLO_RX_DMA_BUSY;
	uint32_t t0 = occ_now(), was = o->state;

	occ_stop(o);
	if (was == OCC_OFF || was == OCC_DEAD)
		return;
	while (occ_rd(MT7996_MCU_DMA_GLO_CFG(OCC_DMA)) & busy)
		if (occ_now() - t0 > 1000u * MT7996_MCU_CYCLES_PER_US)
			break;
}

static void occ_init(struct occ *o)
{
	o->state = OCC_OFF;
	o->tries = 0;
	o->lfsr = occ_now() | 1u;
}

#endif
