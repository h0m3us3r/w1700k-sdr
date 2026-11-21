// SPDX-License-Identifier: MIT
/* Our WM: after the ROM starts it, it sets both CPUs to 720 MHz, publishes its status block and
 * runs the control mailbox (ctl.h) as its main loop, so the host can run MMIO and A-die op
 * programs on it with W1700K_SDR_CTL at any time (development and register discovery), and
 * serves radio commands (WM_RADIO_*, radio/radio_cmd.h): the cold bring-up, channel sets and
 * capture set-up from the radio code in CRAM, and streams with the producer linked in ULM1.
 * It also calls code the host names (WM_CALL) and starts the WA on request (WM_WA_START), for
 * development.
 */
#include "hw/mt7996/conn.h"
#include "hw/mt7996/mcu_cmd.h"
#include "hw/mt7996/mcu_map.h"
#include "hw/mt7996/mcu_periph.h"
#include "hw/mt7996/rom_patch.h"

#include "../stream/stream.h"
#include "../stream/ctl.h"
#include "../radio/hal.h"
#include "../radio/radio.h"
#include "../radio/radio_cmd.h"
#include "../radio/stream_arm.h"
#include "../wa/wa.h"
#include "wm.h"

#define ST(off) R32(WM_STATUS + (off))

void wm_main(void);
uint32_t radio_command(uint32_t op, const uint32_t *arg, uint32_t *state);

/* The WA out of reset, once (only after the radio is configured: wm.h WM_WA_START), and its main
 * loop running within a second: 0, or RADIO_STREAM_NO_WA. */
static uint32_t wa_start(void)
{
	uint32_t t0 = rdcycle(), beats;

	if (ST(WM_WA_START) != WM_WA_STARTED) {
		R32(MT7996_MCU_CFG_WM_WA) = MT7996_MCU_CFG_WM_WA_WA_CPU_RSTB;
		ST(WM_WA_START) = WM_WA_STARTED;
	}
	while (R32(WA_STATUS + WA_MAGIC_AT) != WA_MAGIC)
		if (rdcycle() - t0 > MT7996_MCU_CLOCK_HZ)
			return RADIO_STREAM_NO_WA;
	beats = R32(WA_STATUS + WA_BEATS_AT);
	while (R32(WA_STATUS + WA_BEATS_AT) == beats)
		if (rdcycle() - t0 > MT7996_MCU_CLOCK_HZ)
			return RADIO_STREAM_NO_WA;
	return 0;
}

_Static_assert(SCHEDULE_COUNT == RADIO_PACKINGS, "one schedule per RADIO_PACKING_* code");

/* STREAM: the WA started if the packing schedule has it pack, the route and ICAP0 armed, the
 * result page and the go word cleared, then the producer (linked at WM_PRODUCER) with that
 * schedule until the host's stop word; afterwards the route released, unless the WA never left
 * the packer (a route change under a core that still reads the pool hangs it). Returns the
 * producer's status. */
static uint32_t stream(uint32_t packing, uint32_t state)
{
	void (*producer)(uint32_t) = (void (*)(uint32_t))(uintptr_t)WM_PRODUCER;
	uint32_t res, wa, band;

	if (packing >= SCHEDULE_COUNT)
		return RADIO_STREAM_BAD_PACKING;
	/* Not before the radio is up and the band ICAP listens to has its capture path: the WA
	 * started on an unconfigured radio leaves it without antenna signal for good
	 * (mcu_periph.h HAZARD), and a stream nothing is captured into has no end, since the pump
	 * waits for a first buffer before it counts idle time. */
	for (band = 0; band < RADIO_BANDS && !(state & RADIO_STATE_SELECTED(band)); band++)
		;
	if (!(state & RADIO_STATE_UP) || band == RADIO_BANDS || !(state & RADIO_STATE_CAPTURE(band)))
		return RADIO_STREAM_NOT_READY;
	wa = schedules[packing].wa_mask != 0;
	if (wa && (res = wa_start()))
		return res;
	stream_arm();
	for (uint32_t i = 0; i < 4; i++)
		R32(DONE_AT + 4 * i) = 0;
	/* The fourth of those words is the shift the packers cut radio-reduced chunks at, which
	 * the pump writes: before this, if it is quick, and then only when the value changes. Left
	 * at 0 it would cut every chunk at shift 0, which wraps where the largest shift only loses
	 * low bits; so the word starts at the largest. */
	_Static_assert(MT7996_PROBE_WM_SHIFT == DONE_AT + 12, "the shift word is the result page's fourth");
	R32(MT7996_PROBE_WM_SHIFT) = MT7996_IQ_RADIO14_SHIFT_MAX;
	R32(GO_ADDR) = 0;
	asm volatile("fence" ::: "memory");
	producer(packing);
	asm volatile("csrci mstatus, %0" : : "n"(MT7996_MCU_MSTATUS_MIE) : "memory");
	res = RES->status;
	if (!wa || (RES->wa_status & RES_WA_EXITED))
		stream_disarm();
	return res;
}

/* A radio command: the op and its arguments from the status block, the result, the failure
 * record and the time taken back, then the ACK of the request that was served (one posted
 * meanwhile is the next).
 *
 * A load or store that faults is skipped by the trap handler (start.S), and the code goes on
 * with whatever the register held: a read "returns" its own address, and a read-modify-write
 * then writes something made of that. A command during which that happened has not done what it
 * says, so it fails, with the faulting address in the failure record. (A stream's result is the
 * producer's; its traps are for whoever reads the status block.)
 *
 * The time is the core's cycle counter, which wraps every 5.9 s: right for a command, and for
 * a stream, which lasts as long as it lasts, it is 0. */
static void radio_serve(void)
{
	uint32_t arg[4], state = ST(WM_RADIO_STATE), t0, res, op = ST(WM_RADIO_OP), req = ST(WM_RADIO_REQ);
	uint32_t traps = ST(WM_TRAPS);

	for (int i = 0; i < 4; i++)
		arg[i] = ST(WM_RADIO_ARG(i));
	hal_fail_rec.code = hal_fail_rec.arg = hal_fail_rec.count = 0;
	t0 = hal_now_us();
	res = op == RADIO_OP_STREAM ? stream(arg[0], state) : radio_command(op, arg, &state);
	ST(WM_RADIO_US) = op == RADIO_OP_STREAM ? 0 : hal_now_us() - t0;
	if (op != RADIO_OP_STREAM && ST(WM_TRAPS) != traps) {
		hal_fail(HAL_FAIL_TRAP, ST(WM_TRAP_TVAL));
		res = RADIO_ERR_HW;
	}
	ST(WM_RADIO_RES) = res;
	ST(WM_RADIO_FAIL_CODE) = hal_fail_rec.code;
	ST(WM_RADIO_FAIL_ARG) = hal_fail_rec.arg;
	ST(WM_RADIO_FAILS) = hal_fail_rec.count;
	ST(WM_RADIO_STATE) = state;
	asm volatile("fence" ::: "memory");
	ST(WM_RADIO_ACK) = req;
}

/* A ROM routine, called with the ROM's gp (wm_main puts it in gp for good). ROM routines restore
 * interrupts on their way out, the ROM's way, which turns them on: they go off again here, since
 * nothing of ours takes an interrupt. */
static void rom_call2(uint32_t fn, uint32_t a0, uint32_t a1)
{
	register uint32_t r0 asm("a0") = a0, r1 asm("a1") = a1;

	asm volatile("jalr ra, 0(%[f])\n\tcsrci mstatus, %[mie]"
		     : "+r"(r0), "+r"(r1)
		     : [f] "r"(fn), [mie] "n"(MT7996_MCU_MSTATUS_MIE)
		     : "ra", "a2", "a3", "a4", "a5", "a6", "a7", "t0", "t1", "t2", "t3", "t4", "t5", "t6",
		       "memory");
}

/* The ROM leaves cache lines valid over CRAM, which the firmware download then fills behind the
 * caches: without this, the radio code's tables and data read as the stale lines (zeros), while
 * its instructions happened to fetch right. MediaTek's WM does the same first thing. Nothing of
 * ours is dirty yet (CRAM is our image, the rest of our memory is non-cacheable), so the D-cache
 * is dropped, not written back. */
static void caches_reset(void)
{
	asm volatile("csrw %0, %1" : : "n"(MT7996_MCU_CSR_MCCTLCOMMAND), "r"(MT7996_MCU_CCTL_L1D_INVAL_ALL)
		     : "memory");
	for (uint32_t i = 0; i < MT7996_MCU_WM_ICACHE_BYTES; i += MT7996_MCU_CACHE_LINE) {
		asm volatile("csrw %0, %1" : : "n"(MT7996_MCU_CSR_MCCTLBEGINADDR), "r"(i));
		asm volatile("csrw %0, %1" : : "n"(MT7996_MCU_CSR_MCCTLCOMMAND), "r"(MT7996_MCU_CCTL_L1I_IX_INVAL));
	}
	asm volatile("fence; fence.i" ::: "memory");
}

void wm_main(void)
{
	const uint32_t clk_720 = WF_CPU_CLK_DIV_EN | HW_PREP(WF_CPU_CLK_DIV_D, WF_CPU_CLK_DIV_D_720MHZ) |
				 HW_PREP(WF_CPU_CLK_DIV_SRC, WF_CPU_CLK_DIV_SRC_PLL);
	struct ctl c;
	uint32_t n = 0;

	/* Our code does not use gp (small-data limit 0, no relaxation); ROM routines need theirs. */
	asm volatile("mv gp, %0" : : "r"(MT7996_WM_ROM_GP));
	caches_reset();
	ST(WM_VERSION_AT) = WM_VERSION;
	ST(WM_BEATS_AT) = 0;
	ST(WM_CALL) = 0;
	ST(WM_CALLS) = 0;
	ST(WM_WA_START) = 0;
	ST(WM_RADIO_EE) = (uint32_t)(uintptr_t)radio_ee;
	ST(WM_RF_BUILD) = (uint32_t)(uintptr_t)rfctl_build;
	ST(WM_UNLOCK) = 0;	/* locked at every start: the guard is lifted by the host, for a load at a time */
	/* Occupancy (stream/occ.h) is off until a host turns it on: the page holds whatever it held. */
	R32(MT7996_PROBE_OCC_PERIOD) = 0;
	R32(MT7996_PROBE_OCC_STATE) = MT7996_PROBE_OCC_ST_OFF;
	R32(MT7996_PROBE_OCC_SNAPSHOTS) = 0;
	R32(MT7996_PROBE_OCC_SEQ) = 0;
	ST(WM_DENIED) = 0;
	ST(WM_RADIO_STATE) = 0;
	ST(WM_RADIO_ACK) = ST(WM_RADIO_REQ);
	R32(MT7996_WF_WM_CLK_DIV) = clk_720;
	R32(MT7996_WF_WA_CLK_DIV) = clk_720;
	/* Complete the host's last FW_START, which the ROM left to us. */
	rom_call2(MT7996_ROM_FWDL_EVENT, *(volatile uint8_t *)MT7996_ROM_FWDL_START_SEQ,
		  MT7996_MCU_ROM_REPLY_STATUS_OK);
	/* No interrupt source is ours: mask them all, so a ROM routine that turns interrupts back
	 * on (the TX0 reclaim the producer calls does) cannot take one. */
	asm volatile("csrw mie, zero" ::: "memory");
	ctl_init(&c);
	c.chunk = 0;
	ST(WM_STATE_AT) = WM_STATE_RUNNING;
	asm volatile("fence" ::: "memory");
	ST(WM_MAGIC_AT) = WM_MAGIC;
	for (;;) {
		ctl_step(&c);
		if (!(++n & 1023))
			ST(WM_BEATS_AT) = n >> 10;
		/* The ROM starts only us; the WA image is in its ILM (the ROM's download put it there). */
		if (ST(WM_WA_START) == WM_WA_START_REQ) {
			R32(MT7996_MCU_CFG_WM_WA) = MT7996_MCU_CFG_WM_WA_WA_CPU_RSTB;
			ST(WM_WA_START) = WM_WA_STARTED;
		}
		if (!c.active && ST(WM_RADIO_REQ) != ST(WM_RADIO_ACK)) {
			radio_serve();
			/* A stream's producer served the mailbox meanwhile. */
			ctl_init(&c);
		}
		if (!c.active && ST(WM_CALL)) {
			uint32_t at = ST(WM_CALL);
			uint32_t (*fn)(uint32_t, uint32_t, uint32_t, uint32_t) = (void *)(uintptr_t)at;

			ST(WM_CALL) = 0;
			/* Locked, a call goes into our own image and nowhere else: the ROM is callable
			 * code too, an eFuse writer among it, and so is whatever a host wrote somewhere.
			 * A refused call is counted, returns nothing, and counts as served. */
			if (ctl_unlocked() || (at >= WM_ENTRY && at < WM_LIMIT) || (at >= WM_CRAM_BASE && at < WM_CRAM_LIMIT)) {
				ST(WM_RET) = fn(ST(WM_ARG(0)), ST(WM_ARG(1)), ST(WM_ARG(2)), ST(WM_ARG(3)));
				asm volatile("csrci mstatus, %0" : : "n"(MT7996_MCU_MSTATUS_MIE) : "memory");
			} else {
				ST(WM_RET) = 0;
				ST(WM_DENIED) = ST(WM_DENIED) + 1;
			}
			ST(WM_CALLS) = ST(WM_CALLS) + 1;
			/* What the callee served stays served. */
			ctl_init(&c);
		}
	}
}
