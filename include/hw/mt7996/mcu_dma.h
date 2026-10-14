/* SPDX-License-Identifier: MIT */
/*
 * MT7996 MCU-side DMA: MCU_DMA0 (the WM's DMA engine towards the host; its
 * TX ring 0 feeds host WFDMA0 RX ring 0), the PDA firmware-download engine
 * in the same block, the TX descriptor format, the firmware's ring adapter,
 * and MEM_DMA.
 *
 * ADDRESS SPACE: MCU bus. MCU_DMA0 is at 0x54000000 (host fixed map BAR
 * 0x02000, 4 KiB). Engine n of this family is at 0x54000000 + (n << 26):
 * engine 1 is MEM_DMA at 0x58000000. Host-side WFDMA registers are in the
 * host PCIe headers.
 *
 * Register layout follows the WFDMA family layout (the same one the host
 * WFDMA uses); only fields used or observed are listed. Confidence tags:
 * [measured] verified on hardware, [static] from firmware/ROM code or vendor
 * register descriptions, [inferred].
 */
#ifndef HW_MT7996_MCU_DMA_H
#define HW_MT7996_MCU_DMA_H

#include "../hw_bits.h"

#define MT7996_MCU_DMA0_BASE              0x54000000u
#define MT7996_MEM_DMA_BASE               0x58000000u
#define MT7996_MCU_DMA_ENGINE(n)          (0x54000000u + ((unsigned int)(n) << 26))

/* ========================================================================
 * Address-range checks (armed only when non-zero; live: open) [static]
 * ======================================================================== */
#define MT7996_MCU_DMA_TX_DMAD_RNG_START(b) ((b) + 0x0b0u)
#define MT7996_MCU_DMA_TX_DMAD_RNG_END(b) ((b) + 0x0b8u)
#define MT7996_MCU_DMA_RX_DMAD_RNG_START(b) ((b) + 0x0c0u)
#define MT7996_MCU_DMA_RX_DMAD_RNG_END(b) ((b) + 0x0c8u)
#define MT7996_MCU_DMA_TX_PLD_RNG_START(b) ((b) + 0x0d0u)
#define MT7996_MCU_DMA_TX_PLD_RNG_END(b)  ((b) + 0x0d8u)
#define MT7996_MCU_DMA_RX_PLD_RNG_START(b) ((b) + 0x0e0u)
#define MT7996_MCU_DMA_RX_PLD_RNG_END(b)  ((b) + 0x0e8u)

/* ========================================================================
 * Reset, software interrupts and status
 * ======================================================================== */
#define MT7996_MCU_DMA_CONN_HIF_RST(b)    ((b) + 0x100u)
#define   MT7996_MCU_DMA_HIF_LOGIC_RST_N  HW_BIT(4)
#define   MT7996_MCU_DMA_DMASHDL_RST_N    HW_BIT(5)
#define MT7996_MCU_DMA_HOST2MCU_SW_INT_SET(b) ((b) + 0x108u)	/* [15:0] */
#define MT7996_MCU_DMA_MCU2HOST_SW_INT_SET(b) ((b) + 0x10cu)	/* [15:0] */
/* MCU interrupt status. Reads 0 on every clean run; bits 16/17 seen set
 * after a stranded-descriptor wedge. [measured] */
#define MT7996_MCU_DMA_MCU_INT_STA(b)     ((b) + 0x110u)
#define   MT7996_MCU_DMA_INT_HOST2MCU_SW  HW_GENMASK(15, 0)
#define   MT7996_MCU_DMA_INT_TX_TIMEOUT   HW_BIT(16)
#define   MT7996_MCU_DMA_INT_RX_TIMEOUT   HW_BIT(17)
#define   MT7996_MCU_DMA_INT_TXFIFO0_OVF  HW_BIT(18)
#define   MT7996_MCU_DMA_INT_TXFIFO1_OVF  HW_BIT(19)
#define   MT7996_MCU_DMA_INT_RXFIFO_OVF   HW_BIT(20)
#define   MT7996_MCU_DMA_INT_TX_DMAD_RNG  HW_BIT(21)
#define   MT7996_MCU_DMA_INT_RX_DMAD_RNG  HW_BIT(22)
#define   MT7996_MCU_DMA_INT_TX_PLD_RNG   HW_BIT(23)
#define   MT7996_MCU_DMA_INT_RX_PLD_RNG   HW_BIT(24)
#define   MT7996_MCU_DMA_INT_RXFIFO1_OVF  HW_BIT(25)
#define MT7996_MCU_DMA_MCU_INT_ENA(b)     ((b) + 0x114u)
/* MEM_DMA's MCU_INT_STA puts the TX/RX timeouts at bits 8/9 instead. [static] */
#define   MT7996_MEM_DMA_INT_TX_TIMEOUT   HW_BIT(8)
#define   MT7996_MEM_DMA_INT_RX_TIMEOUT   HW_BIT(9)
#define MT7996_MEM_DMA_HOST_INT_STA       0x58000200u	/* bit 0 written by the WA */

/* ========================================================================
 * Global configuration [static]
 * ======================================================================== */
#define MT7996_MCU_DMA_GLO_CFG(b)         ((b) + 0x208u)
#define   MT7996_MCU_DMA_GLO_TX_DMA_EN    HW_BIT(0)
#define   MT7996_MCU_DMA_GLO_TX_DMA_BUSY  HW_BIT(1)
#define   MT7996_MCU_DMA_GLO_RX_DMA_EN    HW_BIT(2)
#define   MT7996_MCU_DMA_GLO_RX_DMA_BUSY  HW_BIT(3)
#define   MT7996_MCU_DMA_GLO_BT_SIZE      HW_GENMASK(5, 4)
#define   MT7996_MCU_DMA_GLO_TX_WB_DDONE  HW_BIT(6)
#define   MT7996_MCU_DMA_GLO_DMAD_32B_EN  HW_BIT(8)
#define   MT7996_MCU_DMA_GLO_RX_WB_DDONE  HW_BIT(13)
#define   MT7996_MCU_DMA_GLO_CSR_SW_RST   HW_BIT(24)
#define   MT7996_MCU_DMA_GLO_ADDR_EXT_EN  HW_BIT(26)
#define   MT7996_MCU_DMA_GLO_OMIT_RX_INFO HW_BIT(27)
#define   MT7996_MCU_DMA_GLO_OMIT_TX_INFO HW_BIT(28)
#define   MT7996_MCU_DMA_GLO_CLK_GATE_DIS HW_BIT(30)
#define MT7996_MCU_DMA_RST_DTX_PTR(b)     ((b) + 0x20cu)	/* one bit per TX ring */
#define MT7996_MCU_DMA_PAUSE0(b)          ((b) + 0x220u)	/* [19:0] TX ring pause, [20] RX all */
#define MT7996_MCU_DMA_TIMEOUT_CFG(b)     ((b) + 0x230u)
#define   MT7996_MCU_DMA_TO_TX_TH         HW_GENMASK(7, 0)
#define   MT7996_MCU_DMA_TO_TX_TICK       HW_GENMASK(14, 8)
#define   MT7996_MCU_DMA_TO_TX_ENA        HW_BIT(15)
#define   MT7996_MCU_DMA_TO_RX_TH         HW_GENMASK(23, 16)
#define   MT7996_MCU_DMA_TO_RX_TICK       HW_GENMASK(30, 24)
#define   MT7996_MCU_DMA_TO_RX_ENA        HW_BIT(31)
#define MT7996_MCU_DMA_RST_DRX_PTR(b)     ((b) + 0x280u)
#define MT7996_MCU_DMA_INFO(b)            ((b) + 0x284u)	/* RO */
#define   MT7996_MCU_DMA_INFO_TX_RINGS    HW_GENMASK(7, 0)
#define   MT7996_MCU_DMA_INFO_RX_RINGS    HW_GENMASK(15, 8)
#define   MT7996_MCU_DMA_INFO_PDA_EXIST   HW_BIT(31)
#define MT7996_MCU_DMA_GLO_CFG_EXT0(b)    ((b) + 0x2b0u)
#define   MT7996_MCU_DMA_EXT0_MAX_PREFETCH HW_GENMASK(1, 0)
#define   MT7996_MCU_DMA_EXT0_DMAD_PF_TH  HW_GENMASK(21, 20)	/* start at >= 1/2/4/8 free */
/* Prefetch counts shared across neighbouring DMAs must be programmed before
 * DMA is enabled; do not re-run the ROM init to reconfigure. [static] */

/* ========================================================================
 * Rings: TX ring n (0..6) at +0x300 + 0x10 n, RX ring n (0..11) at
 * +0x500 + 0x10 n.
 * ======================================================================== */
#define MT7996_MCU_DMA_TX_BASE(b, n)      ((b) + 0x300u + 0x10u * (n))
#define MT7996_MCU_DMA_TX_CNT(b, n)       ((b) + 0x304u + 0x10u * (n))
#define MT7996_MCU_DMA_TX_CIDX(b, n)      ((b) + 0x308u + 0x10u * (n))
#define MT7996_MCU_DMA_TX_DIDX(b, n)      ((b) + 0x30cu + 0x10u * (n))	/* RO */
#define MT7996_MCU_DMA_RX_BASE(b, n)      ((b) + 0x500u + 0x10u * (n))
#define MT7996_MCU_DMA_RX_CNT(b, n)       ((b) + 0x504u + 0x10u * (n))
#define MT7996_MCU_DMA_RX_CIDX(b, n)      ((b) + 0x508u + 0x10u * (n))
#define MT7996_MCU_DMA_RX_DIDX(b, n)      ((b) + 0x50cu + 0x10u * (n))
#define   MT7996_MCU_DMA_RING_MAX_CNT     HW_GENMASK(11, 0)	/* in CNT */
#define   MT7996_MCU_DMA_RING_BASE_EXT    HW_GENMASK(19, 16)	/* in CNT */
#define   MT7996_MCU_DMA_RING_IDX         HW_GENMASK(11, 0)	/* CIDX / DIDX */
/* Prefetch control: [31:16] base in prefetch SRAM (<= 0x3ff), [7:0] depth. */
#define MT7996_MCU_DMA_TX_EXT_CTRL(b, n)  ((b) + 0x600u + 4u * (n))
#define MT7996_MCU_DMA_RX_EXT_CTRL(b, n)  ((b) + 0x680u + 4u * (n))
#define   MT7996_MCU_DMA_EXT_CTRL_BASE    HW_GENMASK(31, 16)
#define   MT7996_MCU_DMA_EXT_CTRL_DEPTH   HW_GENMASK(7, 0)

/* MCU_DMA0 TX ring 0: the WM -> host event ring [measured] */
#define MT7996_MCU_DMA0_TX0_BASE          0x54000300u
#define MT7996_MCU_DMA0_TX0_CNT           0x54000304u
#define MT7996_MCU_DMA0_TX0_CIDX          0x54000308u
#define MT7996_MCU_DMA0_TX0_DIDX          0x5400030cu
#define MT7996_MCU_DMA0_INT_STA           0x54000110u
/* Live configuration: 8 descriptors at 0x0223fd40 (ULM2, non-cacheable).
 * Hardware delivers each TX0 buffer into host WFDMA0 RX ring 0 (MCU view
 * 0x7c024500). [measured] */
#define MT7996_MCU_DMA0_TX0_DESC          0x0223fd40u
#define MT7996_MCU_DMA0_TX0_ENTRIES       8u

/* ========================================================================
 * TX descriptor (16 bytes) [measured]
 * ======================================================================== */
#define MT7996_MCU_TXD_W0_BUF0            0x0u	/* raw 32-bit MCU address of the payload */
#define MT7996_MCU_TXD_W1_CTRL            0x4u
#define   MT7996_MCU_TXD_DDONE            HW_BIT(31)
#define   MT7996_MCU_TXD_LS0              HW_BIT(30)
#define   MT7996_MCU_TXD_SDL0             HW_GENMASK(29, 16)	/* length, max 16,383 bytes */
/* w1[31:16] alone, the halfword the publisher stores (below). [measured] */
#define MT7996_MCU_TXD_W1_HI              0x6u
#define MT7996_MCU_TXD_W2_BUF1            0x8u
#define MT7996_MCU_TXD_W3_INFO            0xcu
#define MT7996_MCU_TXD_SIZE               16u
/*
 * Publishing (what works): write w0, then the halfword w1[31:16] =
 * LS0 | len with DDONE clear, fence w,w, then advance CIDX. At most
 * entries - 1 in flight: full when (cidx + 1) % entries == DIDX; completion
 * is tracked by DIDX, not DDONE. Largest power-of-two chunk that fits SDL0
 * is 8 KiB. No address-high bits are needed for MCU addresses.
 * Payload sources TX0 reads directly: the UMAC pool (CPU-written cacheable
 * data needs a D-cache write-back first), WM ULM2, WM patch SRAM
 * (0x0090d000, 0x00910000..0x00915fff), WA DLM (0x1025e100..0x102637ff).
 * Host RX0 ring: 1536 descriptors work; 4096 were accepted by the size
 * write but never delivered. [measured]
 */

/* ========================================================================
 * Firmware ring adapter (WM data in ULM2, retail): one 0x50-byte record per
 * (engine, queue) at gp + 0x19e08 + (engine * 10 + queue) * 0x50.
 * ======================================================================== */
#define MT7996_MCU_DMA0_TX0_ADAPTER       0x0222c608u
#define   MT7996_MCU_ADAPTER_BASE         0x00u	/* descriptor base */
#define   MT7996_MCU_ADAPTER_SLOTS        0x08u	/* buffer pointer per descriptor (NULL = none to free) */
#define   MT7996_MCU_ADAPTER_COUNT        0x10u
#define   MT7996_MCU_ADAPTER_CPU          0x2cu	/* firmware's CPU index */
#define   MT7996_MCU_ADAPTER_SENT         0x34u
#define   MT7996_MCU_ADAPTER_DONE         0x38u
/* ROM reclaim(engine, queue): walks done..sent one entry at a time; call with
 * gp = 0x02212800. Firmware event buffers start at 0x0222ec00. [measured] */
#define MT7996_MCU_ROM_DMA_RECLAIM        0x00825010u	/* retail ROM */
/*
 * Throughput [measured]: 2 KiB descriptors, 7 in flight: publish 1.49 GB/s,
 * TX drained 1.45 GB/s, host landed 1.446 GB/s (Gen3 x2, MPS 128 B; ~87% of
 * the ~1.66 GB/s ceiling). With PCIe ASPM on: 0.32 GB/s. 8 KiB descriptors
 * sustain the 1.28 GB/s (320 Msps) capture stream gap-free for 10 min.
 *
 * HAZARD: stopping the capture producer before TX0 has drained strands the
 * last descriptor, sets TX timeout bits 16/17 in MCU_INT_STA and wedges the
 * WM (REG_ACCESS dead) until a firmware reload. Always drain first. [measured]
 * HAZARD: ROM reclaim hangs the WM on a 72-byte (not 16-aligned) frame;
 * skipping reclaim kills the command path instead; reclaim over ~9.4 M
 * entries never returned. Mark all but the last 8 done before calling it,
 * and publish with NULL slots so it frees nothing. [measured]
 * HAZARD: a redirect of a descriptor to a bus address PCIe cannot reach left
 * the MCU command path dead until RESET_MT + reload. [measured]
 * HAZARD: PCIe ASPM L0s/L1 cuts TX0 throughput to ~0.32 GB/s; turn it off. [measured]
 */

/* ========================================================================
 * PDA: firmware-download engine (MCU_DMA0 +0x800; host BAR 0x02800)
 * ======================================================================== */
/* Destination address for the current region; after a failure it holds the
 * last region processed. RW. [static/measured] */
#define MT7996_PDA_TAR_ADDR               0x54000800u
#define MT7996_PDA_TAR_LEN                0x54000804u	/* [19:0] */
#define   MT7996_PDA_TAR_LEN_MASK         HW_GENMASK(19, 0)
/* Download state. Reads 0x25 after a good load. [measured] */
#define MT7996_PDA_DWLD_STATE             0x54000808u
#define   MT7996_PDA_STATE_FINISH         HW_BIT(0)
/* [3:2] == 1 means idle: the ROM's wait-idle polls (v & 0xc) == 4, 1024
 * times with a ~1 ms delay. [static] */
#define   MT7996_PDA_STATE_IDLE_FIELD     HW_GENMASK(3, 2)
#define   MT7996_PDA_STATE_IDLE           1u
#define   MT7996_PDA_STATE_LEN_OK         HW_BIT(2)	/* length check requires it set */
#define   MT7996_PDA_STATE_LEN_ERR        0x07ffff10u	/* length check requires all clear */
#define   MT7996_PDA_STATE_CRC_VALID      HW_BIT(5)	/* CRC accumulation complete */
#define   MT7996_PDA_STATE_RESET          HW_BIT(31)	/* ROM writes 0x80000000 to reset */
/* Decrypt mode: mode 0 -> |= 3; mode 1 -> (v & ~2) | 1. [static] */
#define MT7996_PDA_CONFG                  0x5400080cu
#define   MT7996_PDA_CONFG_DECRYPT        HW_GENMASK(1, 0)
/* Accumulated CRC residue over the region's ciphertext (the check runs
 * before decryption). Must read 0 for START to accept. RO. Not a standard
 * CRC-32 syndrome; there is no polynomial, seed or expected-value register.
 * Example: one flipped ciphertext bit -> 0x664e8dcd and START status 2;
 * a stock reload in the same boot restores 0. [measured] */
#define MT7996_PDA_CRC_VAL                0x54000814u
/*
 * ROM checks at START (scatter completion): wait-idle (else 5), length
 * (else 1), CRC: CRC_VALID set and CRC_VAL == 0 (else 2). A rejected WM
 * costs only the attempt; the next load is clean. The PDA-CRC ROM-patch
 * hook (rom_patch.h) defeats only the CRC comparison. [static/measured]
 */

/* ========================================================================
 * MEM_DMA (engine 1): local TX0 -> RX0 payload transfer
 * ======================================================================== */
/* WM: engine-1 TX0 adapter 0x0222c928 (descriptors 0x0223fec0..0x0223ff40,
 * 8), RX1 adapter 0x0222c978 (descriptors 0x0223fcc0..0x0223fd40, 8). WA:
 * TX1 at 0x58000310 (4 entries), RX0 at 0x58000500 (8). The ROM sets RX0
 * prefetch 0x58000680 to base 0x220 / depth 2; the WA's table says base
 * 0x2c0 (unresolved). */
#define MT7996_MEM_DMA_WM_TX0_ADAPTER     0x0222c928u
#define MT7996_MEM_DMA_WM_RX1_ADAPTER     0x0222c978u

/* Measured on W1700K2 / MT7996 (2026-10-14), own firmware, capture stopped:
 * TX0 -> RX0 moves 8 KiB from ICAP pool 0xe0400000 to DSP0 DTCM 0x8620d000
 * in ~8.7 us at WM 720 MHz, without a CPU payload copy. Two stopped window
 * pages at 0x00464000 / 0x00466000 also work (2 x 4 KiB, ~9.5 us total).
 * tools/dsp/wm-dma.c contains the transfer implementation and its README
 * documents setup, ownership, validation and limits.
 *
 * Completion requires matching TX/RX DIDX AND RX descriptor DDONE:
 * DIDX was observed advancing before DDONE. Preserve the ROM prefetch
 * allocation (TX0 0x00800002, RX0 0x02200002), fence descriptor publication,
 * and stop/drain before reusing memory. DSP must be running via normal PDA
 * load; accessing TCM while DSP reset is held can hang the radio bus.
 * These measurements do not establish continuous capture or concurrent
 * host streaming. The default receiver does not use this test helper.
 */

#endif /* HW_MT7996_MCU_DMA_H */
