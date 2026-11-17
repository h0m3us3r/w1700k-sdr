/* SPDX-License-Identifier: MIT */
/* RF control while streaming: a mailbox in the WM mailbox page that the host fills through
 * W1700K_SDR_CTL (the stream's pump does the writes between sweeps), and a resumable op
 * interpreter the producer runs between chunks.
 *
 * The host writes the ops, then COUNT, CLEANUP and REQ (a new nonzero number). The
 * interpreter runs ops until CTL_BUDGET cycles have passed and never waits inside a call:
 * DELAY, an unmet POLL and the VCO search's settling return and resume on the next call, so a
 * slow sequence costs the stream only the time its register accesses take. When the program
 * ends (all ops, or the first hard failure) it writes STATUS, CYCLES, SOFT and ACK = REQ.
 * CLEANUP = index | guard << 16: a hard failure at an op in [guard, index) jumps to index and
 * runs the rest, still reporting the first failure.
 *
 * Ops are {op, a, b, c}; op = opcode [7:0] | flags [11:8] | arg [31:12]. Flag SOFT: a timeout
 * is counted in SOFT and the program goes on (the firmware ignores its own lock-poll
 * timeouts). arg is the DELAY/POLL time in microseconds.
 *   W32 a=addr b=value          RMW32 a=addr b=mask c=value     R32 a=addr -> res
 *   DELAY                       POLL a=addr b=mask c=value -> res = last read
 *   AW a=reg b=value            AR a=reg -> res                 ARMW a=reg b=mask c=value
 *   APOLL a=reg b=mask c=value -> res = last read
 *   SAR a=die -> res = the VCO band code
 *   RF a=command | band << 8, b, c -> res = its outcome: our WM's builder (radio/rfctl.h, published
 *      at MT7996_PROBE_OWN_WM_RF_BUILD) expands the command into ops of the kinds above, which run
 *      inside the RF op exactly as a posted program's do (resumable, the same budget, lock-poll
 *      timeouts in SOFT); its report goes to the RF block (probe_page.h "RF commands")
 * An A-die reg is die << 16 | k << 12 | index (k: chain 0..4, 15 the die's TOP bank; index is
 * a 12-bit register number). Each A-die access is one SPI transaction under CONN semaphore 12,
 * taken and released around it, as the ROM does. The words, opcodes and codes are
 * MT7996_PROBE_CTL_* (hw/mt7996/probe_page.h).
 *
 * The guard. A raw op reaches any MCU address and any A-die register, and a few of those are
 * not a host's to reach by mistake: what would let the radio transmit, what programs an eFuse,
 * and the accesses the headers mark HAZARD because they wedge a die or the MCU until a power
 * cycle. A posted program's raw ops are judged before they run (ctl_denied) and such an op ends
 * the program with MT7996_PROBE_CTL_DENIED (which ops: guard.h), unless the host has unlocked the WM
 * (MT7996_PROBE_OWN_WM_UNLOCK: for development, until the next load). The firmware's own
 * programs, an RF op's, are not judged: they are ours. This keeps a mistake out; it is no wall
 * against the host, which loads the firmware and can unlock it.
 */
#include "hw/adie/adie.h"
#include "hw/adie/mt7977.h"
#include "hw/adie/sx.h"
#include "hw/mt7996/mcu_periph.h"
#include "hw/mt7996/probe_page.h"

#include "adie_spi.h"
#include "guard.h"
#include "../radio/rfctl.h"

struct ctl {
	uint32_t req, pc, count, started, wait_from, served, calls, cleanup, guard, failed, soft;
	uint32_t sub, sar_i, sar_acc, sar_low;
	const struct rfctl_op *rf;	/* an RF op's program while it runs, else 0 */
	uint32_t rf_pc, rf_count, rf_out, rf_check, rf_unlocked;
	uint32_t chunk, chunk0;		/* the producer keeps chunk = chunks published */
	int active;
};

/* An op's return when it resumes on the next call: no CTL status code (BAD_OP is 1). */
#define CTL_YIELD 0xffffffffu

/* A wait inside an op: 1 while it lasts (the caller yields), 0 once `us` have passed. */
static int ctl_waiting(struct ctl *c, uint32_t us)
{
	if (!c->wait_from)
		c->wait_from = rdcycle() | 1;
	if (rdcycle() - c->wait_from < us * MT7996_MCU_CYCLES_PER_US)
		return 1;
	c->wait_from = 0;
	return 0;
}

/* The VCO band search (as the WM firmware does it, mt7977.h): 10 steps, each a measurement
 * (SX_CMD = MEASURE, 10 us, SX_CMD = PREP), comparator SX_CMP.KEEP kept as the next code bit, and
 * the next trial code written to SX_BAND[31:20] (the preset bits kept) and SX_BAND2[31:20].
 * Returns 0 done, CTL_YIELD, else a CTL code. */
static uint32_t sx_sar(struct ctl *c, uint32_t die, uint32_t *res)
{
	uint32_t sx = MT7996_PROBE_CTL_AREG(die, MT7977_K_SX, 0), v, e, code;

	for (;;) {
		switch (c->sub) {
		case 0:
			if ((e = adie_xfer(sx | ADIE_SX_BAND, 1, &v)))
				return e;
			c->sar_low = v & ADIE_SX_BAND_PRESET;
			c->sar_acc = 0;
			c->sar_i = 0;
			c->sub = 1;
			/* fall through */
		case 1:
			if ((e = adie_w(sx | ADIE_SX_CMD, ADIE_SX_CMD_MEASURE)))
				return e;
			c->sub = 2;
			/* fall through */
		default:
			if (ctl_waiting(c, ADIE_SX_SETTLE_US))
				return CTL_YIELD;
			if ((e = adie_w(sx | ADIE_SX_CMD, ADIE_SX_CMD_PREP)) || (e = adie_xfer(sx | ADIE_SX_CMP, 1, &v)))
				return e;
			c->sar_acc = (c->sar_acc +
				      (HW_GET(ADIE_SX_CMP_KEEP, v) << (MT7977_SAR_STEPS - 1 - c->sar_i))) & 0xffff;
			code = MT7977_SAR_CODE(c->sar_acc, c->sar_i);
			if ((e = adie_w(sx | ADIE_SX_BAND, HW_PREP(ADIE_SX_BAND_WORD, code) | c->sar_low)) ||
			    (e = adie_w(sx | ADIE_SX_BAND2, HW_PREP(ADIE_SX_BAND2_WORD, code))))
				return e;
			if (++c->sar_i < MT7977_SAR_STEPS) {
				c->sub = 1;
				continue;
			}
			*res = code;
			c->sub = 0;
			return 0;
		}
	}
}

/* One op {op, a, b, cc} other than RF: 0 done, CTL_YIELD (resume this op next call), else a hard
 * failure code. */
static uint32_t ctl_exec(struct ctl *c, uint32_t op, uint32_t a, uint32_t b, uint32_t cc, uint32_t *res)
{
	uint32_t arg = HW_GET(MT7996_PROBE_CTL_OP_ARG, op), v, e;

	switch (HW_GET(MT7996_PROBE_CTL_OP_CODE, op)) {
	case MT7996_PROBE_CTL_OP_END:
		return 0;
	case MT7996_PROBE_CTL_OP_W32:
		R32(a) = b;
		return 0;
	case MT7996_PROBE_CTL_OP_RMW32:
		R32(a) = (R32(a) & ~b) | cc;
		return 0;
	case MT7996_PROBE_CTL_OP_R32:
		*res = R32(a);
		return 0;
	case MT7996_PROBE_CTL_OP_DELAY:
		return ctl_waiting(c, arg) ? CTL_YIELD : 0;
	case MT7996_PROBE_CTL_OP_POLL:
		*res = v = R32(a);
		if ((v & b) == cc) {
			c->wait_from = 0;
			return 0;
		}
		return ctl_waiting(c, arg) ? CTL_YIELD : MT7996_PROBE_CTL_TIMEOUT;
	case MT7996_PROBE_CTL_OP_AW:
		return adie_w(a, b);
	case MT7996_PROBE_CTL_OP_AR:
		return adie_xfer(a, 1, res);
	case MT7996_PROBE_CTL_OP_ARMW:
		if ((e = adie_xfer(a, 1, &v)))
			return e;
		*res = (v & ~b) | cc;
		return adie_w(a, *res);
	case MT7996_PROBE_CTL_OP_APOLL:
		if ((e = adie_xfer(a, 1, &v)))
			return e;
		*res = v;
		if ((v & b) == cc) {
			c->wait_from = 0;
			return 0;
		}
		return ctl_waiting(c, arg) ? CTL_YIELD : MT7996_PROBE_CTL_TIMEOUT;
	case MT7996_PROBE_CTL_OP_SAR:
		return sx_sar(c, a, res);
	}
	return MT7996_PROBE_CTL_BAD_OP;
}

/* An RF op. On its first execution our WM's builder makes the command's program and the RF block
 * gets its header; then the program's ops run here as a posted program's do, from op `out` on each
 * result into the RF block, yielding when an op waits or the slice's budget (from t0) is spent.
 * Returns as an op does; res: OK, MT7996_PROBE_CTL_RF_UNLOCKED when the check op read an unlocked
 * VCO, or the failure | the failing op of the program << 8. */
static uint32_t ctl_rf(struct ctl *c, uint32_t t0, uint32_t a, uint32_t b, uint32_t cc, uint32_t *res)
{
	const struct rfctl_op *o;
	struct rfctl_prog p;
	rfctl_build_fn build;
	uint32_t e, v, i;

	if (!c->rf) {
		build = (rfctl_build_fn)(uintptr_t)R32(MT7996_PROBE_OWN_WM_RF_BUILD);
		e = R32(MT7996_PROBE_OWN_WM_STATUS) == MT7996_PROBE_OWN_WM_MAGIC && build ? build(a, b, cc, &p) :
		    MT7996_PROBE_CTL_BAD_OP;
		if (e) {
			*res = e;
			return e;
		}
		R32(MT7996_PROBE_CTL_RF_LO) = p.lo;
		R32(MT7996_PROBE_CTL_RF_LO_HI) = p.lo_hi;
		R32(MT7996_PROBE_CTL_RF_OPS) = p.count;
		R32(MT7996_PROBE_CTL_RF_OUTS) = p.count - p.out;
		for (i = 0; i < MT7996_PROBE_CTL_RF_OUT_MAX; i++)
			R32(MT7996_PROBE_CTL_RF_OUT(i)) = 0;
		c->rf = p.ops;
		c->rf_count = p.count;
		c->rf_out = p.out;
		c->rf_check = p.check;
		c->rf_pc = 0;
		c->rf_unlocked = 0;
	}
	while (c->rf_pc < c->rf_count) {
		o = &c->rf[c->rf_pc];
		v = 0;
		e = ctl_exec(c, o->op, o->a, o->b, o->c, &v);
		if (e == CTL_YIELD)
			return CTL_YIELD;
		if (e) {
			c->wait_from = 0;
			c->sub = 0;
			if (e != MT7996_PROBE_CTL_TIMEOUT || !(o->op & MT7996_PROBE_CTL_OP_SOFT)) {
				*res = e | HW_PREP(MT7996_PROBE_CTL_STATUS_OP, c->rf_pc);
				c->rf = 0;
				return e;
			}
			c->soft++;
		}
		if (c->rf_pc >= c->rf_out && c->rf_pc - c->rf_out < MT7996_PROBE_CTL_RF_OUT_MAX)
			R32(MT7996_PROBE_CTL_RF_OUT(c->rf_pc - c->rf_out)) = v;
		if (c->rf_pc == c->rf_check && HW_GET(ADIE_SX_STATUS_VCO, v) != ADIE_SX_STATUS_VCO_LOCKED)
			c->rf_unlocked = 1;
		if (++c->rf_pc < c->rf_count && rdcycle() - t0 > MT7996_PROBE_CTL_BUDGET)
			return CTL_YIELD;
	}
	c->rf = 0;
	*res = c->rf_unlocked ? MT7996_PROBE_CTL_RF_UNLOCKED : MT7996_PROBE_CTL_OK;
	return 0;
}

/* ---- The guard (see the head of this file) ---- */

static int ctl_unlocked(void)
{
	return R32(MT7996_PROBE_OWN_WM_UNLOCK) == MT7996_PROBE_OWN_WM_UNLOCK_MAGIC;
}

/* One op of the posted program, at `at`. */
static uint32_t ctl_op(struct ctl *c, uint32_t t0, uint32_t at, uint32_t *res)
{
	uint32_t op = R32(at), a = R32(at + 4), b = R32(at + 8), cc = R32(at + 12);

	if (HW_GET(MT7996_PROBE_CTL_OP_CODE, op) == MT7996_PROBE_CTL_OP_RF)
		return ctl_rf(c, t0, a, b, cc, res);
	if (ctl_denied(op, a, b, cc) && !ctl_unlocked())
		return MT7996_PROBE_CTL_DENIED;
	return ctl_exec(c, op, a, b, cc, res);
}

static void ctl_finish(struct ctl *c, uint32_t status)
{
	if (c->failed)
		status = c->failed;
	R32(MT7996_PROBE_CTL_STATUS) = status;
	R32(MT7996_PROBE_CTL_SOFT) = c->soft;
	R32(MT7996_PROBE_CTL_CHUNK0) = c->chunk0;
	R32(MT7996_PROBE_CTL_CHUNK1) = c->chunk;
	R32(MT7996_PROBE_CTL_CYCLES) = rdcycle() - c->started;
	R32(MT7996_PROBE_CTL_SERVED) = ++c->served;
	asm volatile("fence" ::: "memory");
	R32(MT7996_PROBE_CTL_ACK) = c->req;
	c->active = 0;
}

/* One slice of work: start a new program if one is posted, then run ops until the budget is
 * spent or an op yields. */
static void ctl_step(struct ctl *c)
{
	uint32_t t0 = rdcycle(), v, e, at, res;

	if (!c->active) {
		v = R32(MT7996_PROBE_CTL_REQ);
		if (!(++c->calls & 1023)) {
			R32(MT7996_PROBE_CTL_CALLS) = c->calls;
			R32(MT7996_PROBE_CTL_SEEN) = v;
		}
		if (v == c->req || !v)
			return;
		c->req = v;
		c->count = R32(MT7996_PROBE_CTL_COUNT);
		v = R32(MT7996_PROBE_CTL_CLEANUP);
		c->cleanup = HW_GET(MT7996_PROBE_CTL_CLEANUP_INDEX, v);
		c->guard = HW_GET(MT7996_PROBE_CTL_CLEANUP_GUARD, v);
		c->failed = 0;
		c->soft = 0;
		c->pc = 0;
		c->sub = 0;
		c->rf = 0;
		c->wait_from = 0;
		c->started = t0;
		c->chunk0 = c->chunk;
		c->active = 1;
		if (c->count > MT7996_PROBE_CTL_MAX) {
			ctl_finish(c, MT7996_PROBE_CTL_TOO_MANY);
			return;
		}
	}
	while (c->pc < c->count) {
		at = MT7996_PROBE_CTL_OP(c->pc);
		res = R32(MT7996_PROBE_CTL_RES(c->pc));
		e = ctl_op(c, t0, at, &res);
		R32(MT7996_PROBE_CTL_RES(c->pc)) = res;
		if (e == CTL_YIELD)
			return;
		if (e) {
			c->wait_from = 0;
			c->sub = 0;
			if (e == MT7996_PROBE_CTL_TIMEOUT && (R32(at) & MT7996_PROBE_CTL_OP_SOFT)) {
				c->soft++;
			} else if (!c->failed && c->pc >= c->guard && c->pc < c->cleanup &&
				   c->cleanup < c->count) {
				c->failed = e | HW_PREP(MT7996_PROBE_CTL_STATUS_OP, c->pc);
				c->pc = c->cleanup;
				continue;
			} else {
				ctl_finish(c, e | HW_PREP(MT7996_PROBE_CTL_STATUS_OP, c->pc));
				return;
			}
		}
		c->pc++;
		if (rdcycle() - t0 > MT7996_PROBE_CTL_BUDGET)
			return;
	}
	ctl_finish(c, MT7996_PROBE_CTL_OK);
}

/* Programs posted before the stream are not run: whatever REQ holds at the start is taken as
 * already served, so a stale request from an earlier run cannot replay. */
static void ctl_init(struct ctl *c)
{
	c->req = R32(MT7996_PROBE_CTL_REQ);
	c->active = 0;
	c->rf = 0;
	c->served = 0;
	c->calls = 0;
	R32(MT7996_PROBE_CTL_ACK) = c->req;
}
