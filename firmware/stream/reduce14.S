/* SPDX-License-Identifier: MIT */
/* reduce14_words(src, dst, bytes, shift): `bytes` (a multiple of 64) of raw beats to 7/16 of that
 * as a radio-reduced chunk's data (include/hw/mt7996/iq_format.h; the model is iq_radio14_encode
 * in iq_reduce.h, and tests/test_firmware_kernels.py runs this code against it). Of each 16
 * samples: every sample is cut to the 7 bits of I and of Q from bit `shift` up, two samples fold
 * into one word shaped like a sample word,
 *   f = ((w0 >> shift) & 0x007f007f) | ((w1 << (7 - shift)) & 0x3f803f80)
 * (the second sample goes straight to its place 7 bits up), and the eight folds are packed to
 * seven words as pack_words does: fold 7's 28 bits ride in the spare bits of the others,
 *   o_k = f_k | ((f7 << (14 - 2k)) & 0xc000c000).
 * 87 instructions a pass against pack_words' 75, two folds woven so that neighbours do not depend
 * on each other. Caller-saved registers only.
 */
	.text
	.globl reduce14_words
reduce14_words:
	li a6, 0x007f007f	/* the first sample's fields */
	slli t6, a6, 7		/* the second's */
	li a5, 0xc000c000
	li a7, 7
	sub a7, a7, a3
	add t5, a0, a2		/* a2 is free after this */
1:
	/* folds 7 and 0 */
	lw a4, 56(a0)
	lw t0, 0(a0)
	lw a2, 60(a0)
	lw t1, 4(a0)
	srl a4, a4, a3
	srl t0, t0, a3
	sll a2, a2, a7
	sll t1, t1, a7
	and a4, a4, a6
	and t0, t0, a6
	and a2, a2, t6
	and t1, t1, t6
	or a4, a4, a2
	or t0, t0, t1
	/* word 0 out; fold 1 */
	slli t1, a4, 14
	lw t2, 8(a0)
	and t1, t1, a5
	lw t3, 12(a0)
	or t0, t0, t1
	srl t2, t2, a3
	sw t0, 0(a1)
	sll t3, t3, a7
	and t2, t2, a6
	and t3, t3, t6
	or t2, t2, t3
	/* word 1 out; fold 2 */
	slli t3, a4, 12
	lw t0, 16(a0)
	and t3, t3, a5
	lw t1, 20(a0)
	or t2, t2, t3
	srl t0, t0, a3
	sw t2, 4(a1)
	sll t1, t1, a7
	and t0, t0, a6
	and t1, t1, t6
	or t0, t0, t1
	/* word 2 out; fold 3 */
	slli t1, a4, 10
	lw t2, 24(a0)
	and t1, t1, a5
	lw t3, 28(a0)
	or t0, t0, t1
	srl t2, t2, a3
	sw t0, 8(a1)
	sll t3, t3, a7
	and t2, t2, a6
	and t3, t3, t6
	or t2, t2, t3
	/* word 3 out; fold 4 */
	slli t3, a4, 8
	lw t0, 32(a0)
	and t3, t3, a5
	lw t1, 36(a0)
	or t2, t2, t3
	srl t0, t0, a3
	sw t2, 12(a1)
	sll t1, t1, a7
	and t0, t0, a6
	and t1, t1, t6
	or t0, t0, t1
	/* word 4 out; fold 5 */
	slli t1, a4, 6
	lw t2, 40(a0)
	and t1, t1, a5
	lw t3, 44(a0)
	or t0, t0, t1
	srl t2, t2, a3
	sw t0, 16(a1)
	sll t3, t3, a7
	and t2, t2, a6
	and t3, t3, t6
	or t2, t2, t3
	/* word 5 out; fold 6 */
	slli t3, a4, 4
	lw t0, 48(a0)
	and t3, t3, a5
	lw t1, 52(a0)
	or t2, t2, t3
	srl t0, t0, a3
	sw t2, 20(a1)
	sll t1, t1, a7
	and t0, t0, a6
	and t1, t1, t6
	or t0, t0, t1
	/* word 6 out */
	slli t1, a4, 2
	and t1, t1, a5
	or t0, t0, t1
	sw t0, 24(a1)
	addi a0, a0, 64
	addi a1, a1, 28
	bltu a0, t5, 1b
	ret
