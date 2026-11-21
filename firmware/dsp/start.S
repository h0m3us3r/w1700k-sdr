/* SPDX-License-Identifier: MIT */
/* The occupancy program's start on an MT7996 DSP core: the core's memory windows and caches, the
 * stack, the trap record, the program's memory cleared, then occ_main for good. Interrupts stay
 * off.
 *
 * The window and cache CSRs are the ones tools/dsp/start.S writes, in its order and with its
 * values: Bernard's work in that file, who read the assignments out of the start of the public
 * mt7996_dsp.bin of 2026-03-11 (they are inferred from it, not documented anywhere). No vendor
 * instruction or data is in this image. What differs here is what follows them.
 */
#include "occ_layout.h"

#define DTCM	0x00200000		/* hw/mt7996/dsp.h MT7996_DSP_DTCM_BASE */
#define CTRL_TAG 0x86c00014		/* MT7996_DSP_CTRL_BASE + MT7996_DSP_CTRL_TAG; READY is 4 below */

.section .text.entry,"ax"
.global _start
_start:
    csrci mstatus, 8
    csrw mie, zero
    li t0, 0x7807800
    csrs mstatus, t0
    csrw 0x9dc, zero
    li t0, 0x10000
    csrw 0x9de, t0
    li t0, 0x200000
    csrw 0x9dd, t0
    li t0, 0x210000
    csrw 0x9df, t0
    li t0, 0xc0
    csrw 0x9e0, t0
    li t0, 0x10000
    csrw 0x9f0, t0
    li t0, 0x2000c0
    csrw 0x9e1, t0
    li t0, 0x210000
    csrw 0x9f1, t0
    li t0, 0x400000c0
    csrw 0x9e2, t0
    li t0, 0x80000000
    csrw 0x9f2, t0
    li t0, 0x800002c0
    csrw 0x9e3, t0
    li t0, 0xc0000000
    csrw 0x9f3, t0
    li t0, 0xc00003c0
    csrw 0x9e4, t0
    li t0, -1
    csrw 0x9f4, t0
    li t0, 0x1f
    csrw 0x9c0, t0
    li t0, 0x37fc
    csrw 0x7c0, t0
    fence.i
    li sp, DTCM + OCC_STACK_TOP
    la t0, trap
    csrw mtvec, t0
    li t0, 0x1f
    csrs 0xbc0, t0
    csrw fcsr, zero
    /* The working memory: nothing loads it, and the sums start at nothing. */
    la t0, __bss_start
    la t1, __bss_end
1:  bgeu t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b
2:  /* The core's control block: our tag, and up. */
    li t0, CTRL_TAG
    li t1, OCC_MAGIC
    sw t1, 0(t0)
    li t1, 1
    sw t1, -4(t0)
    call occ_main
3:  j 3b

.balign 4
trap:
    li t0, DTCM
    csrr t1, mcause
    sw t1, OCC_BOX_CAUSE(t0)
    csrr t1, mepc
    sw t1, OCC_BOX_EPC(t0)
    csrr t1, mtval
    sw t1, OCC_BOX_TVAL(t0)
    li t1, OCC_STATE_TRAPPED
    sw t1, OCC_BOX_STATE(t0)
4:  j 4b
