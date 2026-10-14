/* SPDX-License-Identifier: MIT */
/* Idle DSP test image. No FFT or memory probes. Interrupts remain disabled throughout.
 * Window register assignments are inferred from the public 2026-03-11
 * mt7996_dsp.bin startup, not established architectural documentation.
 * No vendor instructions or payload are included in the build. */
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
    li sp, 0x210000
    la t0, trap
    csrw mtvec, t0
    li t0, 0x1f
    csrs 0xbc0, t0
    csrw fcsr, zero
    /* Publish ready, then leave DTCM untouched for the DMA test. */
    li t0, 0x200000
    li t1, 1
    sw t1, 40(t0)
    li t1, 0x44535031
    sw t1, 0(t0)
    li t0, 0x86c00014
    sw t1, 0(t0)
    li t1, 1
    sw t1, -4(t0)
1:  j 1b

.balign 4
trap:
    li t0, 0x200000
    csrr t1, mcause
    sw t1, 24(t0)
    csrr t1, mtval
    sw t1, 32(t0)
    csrr t1, mepc
    sw t1, 28(t0)
    li t1, 0xbad00001
    sw t1, 40(t0)
2:  j 2b
