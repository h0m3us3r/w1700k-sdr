// SPDX-License-Identifier: MIT
/* The WM's side of occupancy (firmware/stream/occ.h) on the host, for tests/test_firmware_occ.py:
 * its steps run against a model of what they reach: the mailbox page's occupancy block, MEM_DMA,
 * and DSP core 0 with its enable and reset words. The model says what was done to it, one line
 * each, and stops the run if the core's memory is touched while the core does not run.
 *
 *   occ_wm_check PERIOD EVERY STEPS [nodsp|otherimage|dmabusy|stuckcopy|silent|lateddone]
 *                [off@STEP] [on@STEP] [settle]
 *
 * A step is 1 ms and three chunks of stream. Lines:
 *   cfg V              MEM_DMA's configuration written
 *   copy C S D N       at chunk C: N bytes from S to D
 *   ask C R F E S      at chunk C: request R with flags F, EVERY E, over the copy from S
 *   early              the core was asked before its copy was done
 *   state V            the page's state word written
 *   end STATE SNAPSHOTS SEQ SAME CFG   the page at the end; SAME: its report is the core's
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t model_rd(uint32_t a);
static void model_wr(uint32_t a, uint32_t v);
static uint32_t now_, chunk_;
#define occ_rd(a) model_rd(a)
#define occ_wr(a, v) model_wr(a, v)
#define occ_now() (now_)
#define occ_fence() ((void)0)

#include "stream.h"
#include "occ.h"

#define EN (MT7996_MCU_DMA_GLO_TX_DMA_EN | MT7996_MCU_DMA_GLO_RX_DMA_EN)
#define BUSY (MT7996_MCU_DMA_GLO_TX_DMA_BUSY | MT7996_MCU_DMA_GLO_RX_DMA_BUSY)
#define DMA(reg) MT7996_MCU_DMA_##reg

static uint32_t page[0x1000 / 4], box[0x100 / 4], core_report[OCC_REPORT_WORDS];
static uint32_t enable = 7, reset, cfg = 0x10, tx_cidx, tx_didx, rx_cidx, rx_didx;
static uint32_t copied_from, copy_done = 1, ddone_reads, answer_at, answer_req, counted, reports;
static int nodsp, otherimage, dmabusy, stuckcopy, silent, lateddone;

static uint32_t *core_word(uint32_t a)
{
	uint32_t off = a - MT7996_DSP_MCU_DTCM(OCC_CORE);

	if (!(enable & 1) || (reset & 1)) {
		printf("HAZARD %#x\n", (unsigned int)a);
		exit(9);
	}
	if (off < sizeof(box))
		return &box[off / 4];
	if (off - OCC_REPORT < sizeof(core_report))
		return &core_report[(off - OCC_REPORT) / 4];
	printf("stray %#x\n", (unsigned int)a);
	exit(8);
}

static void core_answer(void)
{
	if (!answer_req || now_ < answer_at)
		return;
	if (box[OCC_FLAGS / 4] & OCC_FLAG_RESET)
		counted = 0;
	if (++counted >= box[OCC_EVERY / 4]) {
		counted = 0;
		core_report[0] = ++reports;
		for (uint32_t i = 1; i < OCC_REPORT_WORDS; i++)
			core_report[i] = reports << 16 | i;
		box[OCC_REPORTS / 4] = reports;
	}
	box[OCC_DONE / 4] = answer_req;
	answer_req = 0;
}

static uint32_t model_rd(uint32_t a)
{
	if (a - MT7996_PROBE_OCC_BASE < sizeof(page)) {
		if (a == OCC_RXD(rx_didx ? rx_didx - 1 : OCC_RING - 1) + MT7996_MCU_TXD_W1_CTRL && lateddone && ddone_reads &&
		    !--ddone_reads) {
			page[(a - MT7996_PROBE_OCC_BASE) / 4] |= MT7996_MCU_TXD_DDONE;
			copy_done = 1;
		}
		return page[(a - MT7996_PROBE_OCC_BASE) / 4];
	}
	if (a == MT7996_DSP_CORE_ENABLE)
		return enable;
	if (a == MT7996_DSP_CORE_RESET)
		return reset;
	if (a == DMA(GLO_CFG)(OCC_DMA))
		return cfg | (dmabusy ? MT7996_MCU_DMA_GLO_TX_DMA_BUSY : 0);
	if (a == DMA(TX_CIDX)(OCC_DMA, 0))
		return tx_cidx;
	if (a == DMA(TX_DIDX)(OCC_DMA, 0))
		return tx_didx;
	if (a == DMA(RX_DIDX)(OCC_DMA, 0))
		return rx_didx;
	core_answer();
	return *core_word(a);
}

static void model_wr(uint32_t a, uint32_t v)
{
	uint32_t slot, ctrl, len, *rxd;

	if (a - MT7996_PROBE_OCC_BASE < sizeof(page)) {
		page[(a - MT7996_PROBE_OCC_BASE) / 4] = v;
		if (a == MT7996_PROBE_OCC_STATE)
			printf("state %u\n", (unsigned int)v);
		return;
	}
	if (a == DMA(GLO_CFG)(OCC_DMA)) {
		if ((v & EN) == EN && rx_cidx != OCC_RING - 1)
			printf("enabled before the receive ring had its capacity\n");
		cfg = v;
		printf("cfg %#x\n", (unsigned int)v);
	} else if (a == DMA(RST_DTX_PTR)(OCC_DMA)) {
		tx_didx = 0;
	} else if (a == DMA(RST_DRX_PTR)(OCC_DMA)) {
		rx_didx = 0;
	} else if (a == DMA(RX_CIDX)(OCC_DMA, 0)) {
		rx_cidx = v;
	} else if (a == DMA(TX_CIDX)(OCC_DMA, 0)) {
		slot = tx_cidx;
		tx_cidx = v;
		if ((cfg & EN) != EN || v == slot)
			return;
		ctrl = page[(OCC_TXD(slot) + MT7996_MCU_TXD_W1_CTRL - MT7996_PROBE_OCC_BASE) / 4];
		len = HW_GET(MT7996_MCU_TXD_SDL0, ctrl);
		rxd = &page[(OCC_RXD(slot) - MT7996_PROBE_OCC_BASE) / 4];
		if (!(ctrl & MT7996_MCU_TXD_LS0) || (ctrl & MT7996_MCU_TXD_DDONE) || rxd[1] != HW_PREP(MT7996_MCU_TXD_SDL0, len))
			printf("bad descriptor %u\n", (unsigned int)slot);
		if (stuckcopy)
			return;
		copied_from = page[(OCC_TXD(slot) - MT7996_PROBE_OCC_BASE) / 4];
		printf("copy %u %#x %#x %u\n", (unsigned int)chunk_, (unsigned int)copied_from, (unsigned int)rxd[0],
		       (unsigned int)len);
		tx_didx = rx_didx = v;
		copy_done = !lateddone;
		ddone_reads = lateddone ? 2 : 0;
		if (!lateddone)
			rxd[1] |= MT7996_MCU_TXD_DDONE;
	} else if (a == DMA(TX_BASE)(OCC_DMA, 0) || a == DMA(TX_CNT)(OCC_DMA, 0) || a == DMA(RX_BASE)(OCC_DMA, 0) ||
		   a == DMA(RX_CNT)(OCC_DMA, 0)) {
		if ((a == DMA(TX_BASE)(OCC_DMA, 0) && v != MT7996_PROBE_OCC_TX_RING) ||
		    (a == DMA(RX_BASE)(OCC_DMA, 0) && v != MT7996_PROBE_OCC_RX_RING) ||
		    ((a == DMA(TX_CNT)(OCC_DMA, 0) || a == DMA(RX_CNT)(OCC_DMA, 0)) && v != OCC_RING))
			printf("bad ring %#x %#x\n", (unsigned int)a, (unsigned int)v);
	} else {
		*core_word(a) = v;
		if (a == OCC_CORE_AT(OCC_REQ)) {
			if (!copy_done)
				printf("early\n");
			printf("ask %u %u %u %u %#x\n", (unsigned int)chunk_, (unsigned int)v, (unsigned int)box[OCC_FLAGS / 4],
			       (unsigned int)box[OCC_EVERY / 4], (unsigned int)copied_from);
			if (!silent) {
				answer_req = v;
				answer_at = now_ + 2 * MT7996_MCU_CLOCK_HZ / 1000;	/* 2 ms */
			}
		}
	}
}

int main(int argc, char **argv)
{
	uint32_t period, every, steps, off_at = ~0u, on_at = ~0u;
	struct occ o;
	int settle = 0;

	if (argc < 4)
		return 2;
	period = (uint32_t)strtoul(argv[1], NULL, 0);
	every = (uint32_t)strtoul(argv[2], NULL, 0);
	steps = (uint32_t)strtoul(argv[3], NULL, 0);
	for (int i = 4; i < argc; i++) {
		nodsp |= !strcmp(argv[i], "nodsp");
		otherimage |= !strcmp(argv[i], "otherimage");
		dmabusy |= !strcmp(argv[i], "dmabusy");
		stuckcopy |= !strcmp(argv[i], "stuckcopy");
		silent |= !strcmp(argv[i], "silent");
		lateddone |= !strcmp(argv[i], "lateddone");
		settle |= !strcmp(argv[i], "settle");
		if (!strncmp(argv[i], "off@", 4))
			off_at = (uint32_t)strtoul(argv[i] + 4, NULL, 0);
		if (!strncmp(argv[i], "on@", 3))
			on_at = (uint32_t)strtoul(argv[i] + 3, NULL, 0);
	}
	if (nodsp)
		enable = 0;
	box[OCC_BOX_MAGIC / 4] = otherimage ? 0x44535031 : OCC_MAGIC;
	box[OCC_BOX_STATE / 4] = OCC_STATE_RUNNING;
	box[OCC_REQ / 4] = box[OCC_DONE / 4] = 41;
	memset(page, 0xa5, sizeof(page));	/* what the page held */
	page[(MT7996_PROBE_OCC_PERIOD - MT7996_PROBE_OCC_BASE) / 4] = period;
	page[(MT7996_PROBE_OCC_EVERY - MT7996_PROBE_OCC_BASE) / 4] = every;
	page[(MT7996_PROBE_OCC_THRESHOLD - MT7996_PROBE_OCC_BASE) / 4] = 0x40800000;	/* 4.0f */
	now_ = 12345;
	occ_init(&o);
	occ_step(&o, 0);		/* no stream publishes yet: nothing */
	for (uint32_t step = 0; step < steps; step++) {
		now_ += MT7996_MCU_CLOCK_HZ / 1000;
		chunk_ += 3;
		if (step == off_at)
			page[(MT7996_PROBE_OCC_PERIOD - MT7996_PROBE_OCC_BASE) / 4] = 0;
		if (step == on_at)
			page[(MT7996_PROBE_OCC_PERIOD - MT7996_PROBE_OCC_BASE) / 4] = period;
		occ_step(&o, chunk_);
	}
	if (settle)
		occ_settle(&o);
	printf("end %u %u %u %d %#x\n", (unsigned int)page[(MT7996_PROBE_OCC_STATE - MT7996_PROBE_OCC_BASE) / 4],
	       (unsigned int)page[(MT7996_PROBE_OCC_SNAPSHOTS - MT7996_PROBE_OCC_BASE) / 4],
	       (unsigned int)page[(MT7996_PROBE_OCC_SEQ - MT7996_PROBE_OCC_BASE) / 4],
	       !memcmp(&page[(MT7996_PROBE_OCC_REPORT - MT7996_PROBE_OCC_BASE) / 4], core_report, sizeof(core_report)),
	       (unsigned int)cfg);
	return 0;
}
