/* SPDX-License-Identifier: MIT */
/*
 * MT7996 DSP cores: three MediaTek "MRV5" RISC-V cores, one per band
 * (b = 0..2), running the DSP firmware container (MemDPD / DPD work).
 *
 * Core identity [measured]: misa 0x40901125 (RV32 I M A F C + U + X);
 * mvendorid/marchid/mimpid/mhartid all 0; CSRs 0xfc2..0xfc4 read ASCII
 * "MRV5", "5E00", "1". 640.0 MHz measured against the DSP block's 1 MHz
 * counter. No caches observed. Machine mode only; the firmware enables the
 * FPU (mstatus.FS = 3). Single-precision FMA, no D, no fsqrt, no atomics in
 * the image. 214 CSR numbers present, the same set on all three cores.
 *
 * ADDRESS SPACES. Two views are given:
 *  - DSP view (MT7996_DSP_*): the core's own bus. ITCM at 0, DTCM at
 *    0x00200000, its own control block at 0x86c00000, and the PHY/peripheral
 *    range 0x8xxxxxxx.
 *  - MCU view (MT7996_DSP_MCU_*): the WM and WA bus. ITCM/DTCM of core b at
 *    0x86000000/0x86200000 + b * 0x400000; control block of core b at
 *    0x86c00000 + b * 0x100000; trigger/IRQ words at 0x8308xxxx + b * 0x100000.
 *    The host reaches these through REG_ACCESS (see mcu_dic.h).
 *
 * The DSPs are compute islands for LOAD/STORE: they cannot reach SYSRAM,
 * ULM, CRAM or the UMAC pool at any address (a 448-probe scan of every
 * 16 MiB base found no alias). MEM_DMA TX0 -> RX0 can reach DSP0 DTCM
 * at ~0.9 GB/s (see mcu_dma.h and tools/dsp). Other input paths are
 * MCU copy (35 MB/s),
 * the download engine, or the PHY-side DFE capture engine, which addresses
 * DTCM 0x206000..0x20ffff as 0x80206000..0x8020ffff (see the capture
 * headers; unproven as an RX input).
 *
 * Confidence tags: [measured] verified on hardware, [static] read from
 * firmware/ROM code, [inferred].
 */
#ifndef HW_MT7996_DSP_H
#define HW_MT7996_DSP_H

#include "../hw_bits.h"

#define MT7996_DSP_NUM                    3u
#define MT7996_DSP_CLOCK_HZ               640000000u	/* [measured] */

/* ------------------------------------------------------------------------
 * DSP-view memory map
 * ------------------------------------------------------------------------ */
#define MT7996_DSP_ITCM_BASE              0x00000000u	/* 64 KiB */
#define MT7996_DSP_ITCM_SIZE              0x00010000u
#define MT7996_DSP_RESET_VECTOR           0x00000000u	/* li t0,8; jr t0 */
#define MT7996_DSP_ENTRY                  0x00000008u	/* mstatus, then window CSRs */
/* Vendor code image: 58,256 bytes; ITCM 0xe390..0xffff free. [static] */
#define MT7996_DSP_ITCM_IMAGE_END         0x0000e390u
#define MT7996_DSP_DTCM_BASE              0x00200000u	/* 64 KiB; CSR 0x9dd reads this */
#define MT7996_DSP_DTCM_SIZE              0x00010000u
#define MT7996_DSP_DTCM_DATA_END          0x002016d0u	/* initialised data/rodata; BSS above */
/* MemDPD sample areas in DTCM, filled by the WM (CPU copy) or the DFE
 * capture engine: X 20 KiB then Y 20 KiB. [static] */
#define MT7996_DSP_MEMDPD_X               0x00206000u
#define MT7996_DSP_MEMDPD_Y               0x0020b000u
#define MT7996_DSP_MEMDPD_END             0x00210000u
/* Software trigger table: TRIG3..TRIG7, 16-byte entries
 * {handler, 0xffffffff, bit << 24 | 0x010100, name}. Writable DTCM; the
 * IRQ 24 handler walks it. [static][measured] */
#define MT7996_DSP_TRIG_TABLE             0x002000ccu
#define MT7996_DSP_TRIG_ENTRY(n)          (MT7996_DSP_TRIG_TABLE + 16u * ((n) - 3u))	/* n = 3..7 */
#define MT7996_DSP_TRIG_HANDLER(n)        MT7996_DSP_TRIG_ENTRY(n)
#define   MT7996_DSP_TRIG7_STOCK_HANDLER  0x00001e7cu	/* vendor image tag 0x26030201, all cores */
/* Exception-name pointer table used by the trap dump. [static] */
#define MT7996_DSP_EXC_NAME_TABLE         0x0020009cu
/* Idle state of the vendor image: PC just past wfi, SP. [measured] */
#define MT7996_DSP_IDLE_PC                0x00001db8u
#define MT7996_DSP_IDLE_SP                0x00205cf0u

/* ------------------------------------------------------------------------
 * Per-core control block (DSP view 0x86c00000 = its own; MCU view below)
 * ------------------------------------------------------------------------ */
#define MT7996_DSP_CTRL_BASE              0x86c00000u
#define MT7996_DSP_MCU_CTRL(b)            (0x86c00000u + ((unsigned int)(b) << 20))
/* Ready flag: 1 once the core's firmware is up; clear it before a reset
 * pulse to see the core come back. RW. [measured] */
#define MT7996_DSP_CTRL_READY             0x10u
/* Image build tag, 0x26030201 for the vendor image (DSP and MCU agree). [measured] */
#define MT7996_DSP_CTRL_TAG               0x14u
#define   MT7996_DSP_CTRL_TAG_VENDOR      0x26030201u
/* Per-event trace tags written by the firmware's handlers: +0x18 OTFIQ
 * (0x20230117, then phase tags 0xbb1000nn), +0x30 MEMDPD (0x80000bb1).
 * A breadcrumb bank, not a message mailbox. [static] */
#define MT7996_DSP_CTRL_TAG_OTFIQ         0x18u
#define MT7996_DSP_CTRL_TAG_MEMDPD        0x30u
/* Command word of the WM's OTFIQ runs (phy_cal.h T2{8}, under the WF_COMM
 * +0xd6c4 DSP lock): per run the WM zeroes +0x18, +0x1c, +0x24, +0x28 and
 * +0x48, writes the command here, starts the band's test-signal generator,
 * polls this word for a done bit ([18] after the single pre-run, [16]
 * otherwise) and stops the generator. [3:0] = 1, [15:4] run count, [31] set;
 * the other bits come from the OTFIQ configuration words. Outside the
 * traced window. [static] */
#define MT7996_DSP_CTRL_CMD               0x20u
/* Timers/counters in the same block. Free-running once started; on a DSP
 * the counters run only after CSR 0xbc0 |= 0x1f. The WM and WA read core
 * 0's block at 0x86c0xxxx. [measured] */
#define MT7996_DSP_CTRL_TIMER_40M         0x8000u	/* 40 MHz timer */
#define MT7996_DSP_CTRL_CNT_32K           0x8034u	/* 32.768 kHz counter */
#define MT7996_DSP_CTRL_CNT_1M            0x8068u	/* 1 MHz counter */

/* ------------------------------------------------------------------------
 * MCU-view windows and control
 * ------------------------------------------------------------------------ */
/* ITCM/DTCM of core b. Each 64 KiB and aliases every 64 KiB within its
 * window. WM/WA reach them at 35 MB/s read or write (0.048 B/cycle).
 * Writes read back exactly. [measured] */
#define MT7996_DSP_MCU_ITCM(b)            (0x86000000u + ((unsigned int)(b) << 22))
#define MT7996_DSP_MCU_DTCM(b)            (0x86200000u + ((unsigned int)(b) << 22))
/* DSP-own address -> MCU address for core b (ITCM and DTCM). */
#define MT7996_DSP_MCU_ADDR(b, own)       (0x86000000u + ((unsigned int)(b) << 22) + (own))

/* Software trigger (WM side). For TRIGn (n = 3..7, table index n - 3): set
 * enable bit n, then write pulse bit (n - 3) and 0. The pulse raises the
 * DSP's IRQ 24, which dispatches through MT7996_DSP_TRIG_TABLE. The vendor
 * WM never fires TRIG7, so it is free for our code. [measured] */
#define MT7996_DSP_MCU_TRIG_EN(b)         (0x8308ef08u + ((unsigned int)(b) << 20))
#define   MT7996_DSP_TRIG_EN_BIT(n)       HW_BIT(n)
#define MT7996_DSP_MCU_TRIG_PULSE(b)      (0x8308ef10u + ((unsigned int)(b) << 20))
#define   MT7996_DSP_TRIG_PULSE_BIT(n)    HW_BIT((n) - 3u)
/* DSP-side interrupt acknowledge/source: one bit per source, written by the
 * handler (1 MEMDPD, 2 OTFIQ, 0x10 another). The DSP firmware writes it at
 * 0x8308ef04 in its own view [static]; the per-core MCU-view stride is
 * [inferred] from the neighbouring trigger words. */
#define MT7996_DSP_MCU_IRQ_ACK(b)         (0x8308ef04u + ((unsigned int)(b) << 20))
/* Live SP / PC mirrors of core b, RO. [measured] */
#define MT7996_DSP_MCU_SP(b)              (0x830a02b4u + ((unsigned int)(b) << 20))
#define MT7996_DSP_MCU_PC(b)              (0x830a02b8u + ((unsigned int)(b) << 20))

/*
 * Per-core enable and reset (bit b each). At rest ENABLE = 7, RESET = 0.
 * The WM's per-core restart pulse is:
 *   ENABLE &= ~bit; RESET |= bit; ENABLE |= bit; RESET &= ~bit.
 * It restarts the core from ITCM with ITCM/DTCM contents (and any trigger
 * hook) intact, and recovers a faulted core. [measured]
 */
#define MT7996_DSP_CORE_ENABLE            0x81020010u
#define MT7996_DSP_CORE_RESET             0x81020014u
#define   MT7996_DSP_CORE_BIT(b)          HW_BIT(b)

/* ------------------------------------------------------------------------
 * DSP interrupt numbers (vendor registration, DSP view) [static]
 * ------------------------------------------------------------------------ */
#define MT7996_DSP_IRQ_GPT                1u	/* "GPT LISR" */
#define MT7996_DSP_IRQ_MEMDPD             8u
#define MT7996_DSP_IRQ_OTFIQ              9u	/* "on-the-fly IQ"; handler has a body */
#define MT7996_DSP_IRQ_DFE                24u	/* "DFEIRQ"; also the software-trigger dispatch */
#define MT7996_DSP_IRQ_25                 25u	/* registered, name unresolved */
#define MT7996_DSP_IRQ_TX                 26u	/* "TXIRQ" */

/* ------------------------------------------------------------------------
 * DSP CSRs (CSR numbers) [measured presence; meaning inferred unless noted]
 * ------------------------------------------------------------------------ */
#define MT7996_DSP_CSR_INTC_FIRST         0x5c0u	/* ..0x5f7: interrupt controller */
#define MT7996_DSP_CSR_INTC_LAST          0x5f7u
#define MT7996_DSP_CSR_INTC_CLAIM         0x5c4u	/* read has side effects */
#define MT7996_DSP_CSR_INTC_COMPLETE      0x5c5u	/* read has side effects */
#define MT7996_DSP_CSR_CUST_FIRST         0x7c0u	/* ..0x7e6 */
#define MT7996_DSP_CSR_ACC_FIRST          0x7e0u	/* ..0x7e6 saved on every trap: accumulators? */
#define MT7996_DSP_CSR_ACC_LAST           0x7e6u
#define MT7996_DSP_CSR_USER_FIRST         0x8f0u	/* ..0x8fb and 0x8ff: user-level */
#define MT7996_DSP_CSR_WIN_FIRST          0x9c0u	/* ..0x9ff: memory windows/attributes */
#define MT7996_DSP_CSR_WIN_LAST           0x9ffu
#define MT7996_DSP_CSR_DTCM_BASE          0x9ddu	/* reads 0x00200000 */
#define MT7996_DSP_CSR_CNT_EN             0xbc0u	/* |= 0x1f starts the counters */
#define   MT7996_DSP_CNT_EN_ALL           0x1fu
#define MT7996_DSP_CSR_ID_A               0xfc1u	/* differs per core (0xe5cc / 0xe5d0) */
#define MT7996_DSP_CSR_ID_NAME            0xfc2u	/* "MRV5" */
#define MT7996_DSP_CSR_ID_VER             0xfc3u	/* "5E00" */
#define MT7996_DSP_CSR_ID_REV             0xfc4u	/* "1" */
/* Also present: 0xbc1, 0xbcf..0xbd4, 0xbe0..0xbe4, 0xbe8 (undecoded). */

/* ------------------------------------------------------------------------
 * Vendor ISA extensions (major opcodes) [measured for the MAC; others static]
 * ------------------------------------------------------------------------ */
#define MT7996_DSP_OPC_CUSTOM0            0x0bu	/* post-increment loads/stores */
#define MT7996_DSP_OPC_CUSTOM1            0x2bu	/* 64/128-bit register-group ld/st */
#define MT7996_DSP_OPC_CUSTOM2            0x5bu	/* clip / saturate */
#define MT7996_DSP_OPC_CUSTOM3            0x7bu	/* two-accumulator MAC, acc chosen by rd */
/* custom-3 MAC (full 32-bit signed operands, accumulators >= 36 bits): */
#define   MT7996_DSP_MAC_SET_F7           0x60u	/* funct3 0: acc = rs1 * rs2 */
#define   MT7996_DSP_MAC_ACC_F7           0x40u	/* funct3 1: acc += ; funct3 3: acc -= */
#define   MT7996_DSP_MAC_F3_SET           0u
#define   MT7996_DSP_MAC_F3_ADD           1u
#define   MT7996_DSP_MAC_F3_SUB           3u
#define   MT7996_DSP_MAC_F3_READ          6u	/* rd = round(acc[rs1] >> reg[rs2]), half up, low 32 */
/* Zero-overhead hardware loops use BRANCH funct3 2 and 3. */

/*
 * Measured throughput (per core):
 *   DTCM read 32-bit 7.97 B/cycle (5.1 GB/s, two loads per cycle);
 *   DTCM read/write 128-bit 15.9 B/cycle (10.2 GB/s);
 *   DTCM copy 32-bit / 128-bit 3.55 / 6.39 B/cycle (2.3 / 4.1 GB/s);
 *   single-float FMA 1/cycle (1.28 GFLOPS); int32 mul 2/cycle;
 *   custom MAC 2/cycle with both accumulators (1.28 G MAC/s).
 *
 * HAZARD: a DSP load/store to ULM2 (0x02200000) or SYSRAM (0x00400000)
 * faults the core (PC 0x14e0); 0x10000000..0x3fffffff faults. Recover with
 * the per-core reset pulse. Pool/CRAM reads return 0 and pool writes do not
 * land. [measured]
 * HAZARD: the vendor DSP interrupt wrapper allows nesting. Our own trap
 * handler on a live core took an interrupt, treated it as its own trap,
 * never acknowledged it and re-entered forever, walking its result pointer
 * through DTCM: cores 0 and 2 wedged and needed a firmware reload (no power
 * cycle). Clear mstatus.MIE for the whole of any code run on a DSP and
 * restore it on exit. [measured]
 * HAZARD: never read CSRs 0x5c4/0x5c5 (interrupt claim/complete) or
 * 0x345/0x348/0x349 (CLIC nxti/scratchcsw) in a sweep: reads have side
 * effects. [static]
 */

#endif /* HW_MT7996_DSP_H */
