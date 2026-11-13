/* SPDX-License-Identifier: MIT */
/*
 * MT7996 probe page: this project's own mailbox layout in WM patch SRAM.
 *
 * NOT A HARDWARE BLOCK. Everything here is an interface the project defines
 * between the host (kernel module and tools), code it runs on the WM, WA and
 * DSP cores, and its ROM-patch hooks. The hardware underneath is plain SRAM;
 * see mcu_map.h for the memories themselves.
 *
 * ADDRESS SPACE: WM MCU bus. Patch SRAM (ULM1) is non-cacheable on the WM
 * (Andes PMA), so stores are visible to the host and the WA without cache
 * maintenance. The WA reaches the same words at the same addresses. The host
 * reaches them through its BAR remap windows (see the host PCIe headers) or
 * through the firmware's REG_ACCESS command (behind the REG_ACCESS edit, see
 * mcu_dic.h); a BAR read does not involve the MCU, so it still works while
 * the WM is stalled inside our code.
 *
 * WA- and DSP-side blob slots (the same conventions on the other cores) are
 * at the end of this file.
 *
 * Confidence tags: [measured] verified on hardware, [static] read from
 * firmware/ROM code, [inferred]. For our own interface "[static]" means the
 * layout is fixed by our sources.
 */
#ifndef HW_MT7996_PROBE_PAGE_H
#define HW_MT7996_PROBE_PAGE_H

#include "../hw_bits.h"

/* ------------------------------------------------------------------------
 * Page boundaries
 * ------------------------------------------------------------------------ */

/*
 * The ROM copies the whole DECRYPTED patch section (security footer
 * included) to patch SRAM base. For the stock patch container that is
 * 37,056 bytes, ending at 0x009090c0, not at the 0x00908cc0 the body length
 * (36,032 bytes, footer stripped) suggests. [measured: a blob staged at
 * 0x00909000 lost its first 0xc0 bytes to the copy]
 */
#define MT7996_PROBE_PATCH_COPY_END       0x009090c0u
/*
 * First 4 KiB page clear of the patch copy. A 2 KiB blob staged here before
 * the patch download survives the full patch/WM/DSP/WA load byte-for-byte
 * and executes. [measured]
 */
#define MT7996_PROBE_FLOOR                0x0090a000u
/* End (exclusive) of the window the kernel admits as an IRQ5 slot target:
 * 4-byte aligned addresses in [FLOOR, END). [static] */
#define MT7996_PROBE_END                  0x00910000u
/* Window the host may write through the CTL ioctl and the stream go/stop
 * words: 4-byte aligned, inclusive. [static] */
#define MT7996_PROBE_HOST_FIRST           0x0090b000u
#define MT7996_PROBE_HOST_LAST            0x0090fffcu

/*
 * HAZARD: staging (host POKE through the ROM download path) poisons the NEXT
 * firmware START, which then returns device result 2. Stage only BEFORE the
 * patch download (the patch ends with PATCH_FINISH, not START). After the last
 * START the ROM download state is gone and a POKE fails at WAIT_DOWNLOAD.
 * [measured]
 * HAZARD: anything below MT7996_PROBE_PATCH_COPY_END is overwritten when the
 * patch container lands. [measured]
 */

/* ------------------------------------------------------------------------
 * 0x0090a000: ROM-patch hook evidence block (SYSRAM-layout hook)
 *
 * Written by the profile-5 hook replacement (rom_patch.h) every time the
 * hooked ROM pool initialiser runs: exactly once per WM/DSP/WA download.
 * The PDA-CRC hook writes no evidence block (its effect is the evidence).
 * Uninitialised value of the magic slot after power-on is noise
 * (e.g. 0x2e20ecdf). [measured]
 * ------------------------------------------------------------------------ */
#define MT7996_PROBE_HOOK_BASE            0x0090a000u
#define MT7996_PROBE_HOOK_MAGIC           (MT7996_PROBE_HOOK_BASE + 0x00u)
#define   MT7996_PROBE_HOOK_MAGIC_VAL     0x4b4f4f48u	/* "HOOK" little-endian */
#define MT7996_PROBE_HOOK_CALLS           (MT7996_PROBE_HOOK_BASE + 0x04u)	/* +1 per call */
/* Incoming arguments of the hooked call, before the hook rewrites them:
 * a0 = PLE banks, a1 = 0, a2 = PSE banks, a3 = 1. Profile 4 measured as
 * (0x100, 0, 0x1c0, 1). [measured] */
#define MT7996_PROBE_HOOK_ARG(n)          (MT7996_PROBE_HOOK_BASE + 0x08u + 4u * (n))
/* Return of the layout setter / the ICAP bank-count setter. Pre-seeded to
 * 0xffffffff ("not reached"); 0 = accepted. The layout setter returns 1 and
 * stores nothing if two of its three positions are equal. [measured] */
#define MT7996_PROBE_HOOK_LAYOUT_RESULT   (MT7996_PROBE_HOOK_BASE + 0x18u)
#define MT7996_PROBE_HOOK_BANKS_RESULT    (MT7996_PROBE_HOOK_BASE + 0x1cu)
#define   MT7996_PROBE_HOOK_RESULT_NOT_REACHED 0xffffffffu

/* ------------------------------------------------------------------------
 * 0x0090f000: the stream producer's done word and results
 * ------------------------------------------------------------------------ */
/* Done word, 16 bytes below the results: {magic, count or status}. */
#define MT7996_PROBE_WM_DONE              0x0090f000u
#define   MT7996_PROBE_DONE_MAGIC         0x50524f42u	/* "PROB" */
#define MT7996_PROBE_WM_DONE_ARG          0x0090f004u
/* Go/stop word: the host's stream pump writes GO just before it polls;
 * the producer consumes it (writes 0) and later finds STOP written there. */
#define MT7996_PROBE_WM_GO                0x0090f008u
#define   MT7996_PROBE_GO_MAGIC           0x474f474fu	/* "GOGO" */
#define   MT7996_PROBE_STOP_MAGIC         0x504f5453u	/* "STOP" */
#define MT7996_PROBE_WM_RES               0x0090f010u
/*
 * HAZARD: a generic blob may use up to 500 result words from 0x0090f010,
 * which overlaps the CTL mailbox at 0x0090f400. Never run a generic probe
 * blob while a stream program uses CTL.
 */

/* Stream producer results at MT7996_PROBE_WM_RES (u32 then u64 fields). */
#define MT7996_PROBE_RES_STATUS           0x00u
#define   MT7996_PROBE_RES_ST_NOT_REACHED 0u
#define   MT7996_PROBE_RES_ST_DURATION    1u	/* ran the full duration */
#define   MT7996_PROBE_RES_ST_NO_GO       2u	/* go word never came */
#define   MT7996_PROBE_RES_ST_OVERRUN     3u	/* capture within SAFETY of an unpublished chunk */
#define   MT7996_PROBE_RES_ST_RING_BUSY   4u	/* TX0 CPU != DMA index at start */
#define   MT7996_PROBE_RES_ST_ADAPTER     5u	/* firmware ring adapter out of step */
#define   MT7996_PROBE_RES_ST_NO_DRAIN    6u	/* TX0 did not drain */
#define   MT7996_PROBE_RES_ST_HOST_STOP   7u	/* stopped by the host */
#define   MT7996_PROBE_RES_ST_WA_NOSTART  8u
#define   MT7996_PROBE_RES_ST_WA_NOEXIT   9u
#define   MT7996_PROBE_RES_ST_WA_BUSY     10u	/* WA soft-interrupt dispatch not at rest */
#define MT7996_PROBE_RES_CPU_END          0x04u
#define MT7996_PROBE_RES_TX_DMA_END       0x08u
#define MT7996_PROBE_RES_CR9_END          0x0cu
#define MT7996_PROBE_RES_CR0_END          0x10u
#define MT7996_PROBE_RES_SENT_AFTER       0x14u
#define MT7996_PROBE_RES_DONE_AFTER       0x18u
#define MT7996_PROBE_RES_ERR_STATUS       0x1cu	/* MCU 0x54000110 after reclaim */
#define MT7996_PROBE_RES_GO_WAIT_CYCLES   0x20u
#define MT7996_PROBE_RES_FIRST_CR9        0x24u
#define MT7996_PROBE_RES_WA_STATUS        0x28u	/* WA status | exited << 8 */
#define MT7996_PROBE_RES_PACK_MAX_CYCLES  0x2cu
#define MT7996_PROBE_RES_MAX_LAG          0x30u
#define MT7996_PROBE_RES_WA_PACK_MAX      0x34u
#define MT7996_PROBE_RES_WA_SP            0x38u
#define MT7996_PROBE_RES_WA_RA            0x3cu
#define MT7996_PROBE_RES_WA_EXIT_CYCLES   0x40u
#define MT7996_PROBE_RES_CTL_SERVED       0x44u
#define MT7996_PROBE_RES_CHUNKS           0x48u	/* u64 from here on */
#define MT7996_PROBE_RES_CYCLES           0x50u
#define MT7996_PROBE_RES_WRITTEN          0x58u
#define MT7996_PROBE_RES_CREDIT_WAITS     0x60u
#define MT7996_PROBE_RES_WA_WAITS         0x68u
#define MT7996_PROBE_RES_WM_WAITS         0x70u
#define MT7996_PROBE_RES_WM_PACKS         0x78u
#define MT7996_PROBE_RES_WM_PACK_CYCLES   0x80u
#define MT7996_PROBE_RES_WA_PACKS         0x88u
#define MT7996_PROBE_RES_WA_PACK_CYCLES   0x90u
#define MT7996_PROBE_RES_WA_IDLE          0x98u
#define MT7996_PROBE_RES_SIZE             0xa0u

/*
 * WM packed-chunk buffers (stream, 7168 bytes each = one 8 KiB chunk packed
 * to 28-bit samples). In WM-local patch SRAM: written at local speed and read
 * by MCU_DMA0 TX0 directly. All hold power-on noise; nothing else writes
 * them. [measured]
 */
#define MT7996_PROBE_WM_PACKBUF(n)        ((n) == 0 ? 0x0090d000u : 0x00910000u + 0x1c00u * ((n) - 1u))
#define MT7996_PROBE_WM_PACKBUF_COUNT     4u
#define MT7996_PROBE_PACKED_BYTES         7168u
#define MT7996_PROBE_CHUNK_BYTES          8192u

/* ------------------------------------------------------------------------
 * 0x0090f300: our WM's status block (firmware/wm/wm.h)
 *
 * Written by our own WM, when it runs instead of the vendor WM. Words: magic
 * 'OWWM', version, heartbeat (main-loop passes / 1024), state (1 running,
 * 2 trapped), the core as the ROM handed it over (sp, ra, gp, mstatus, mtvec,
 * mie, a0, a1), the last trap (mcause, mepc, mtval) and the trap count; then
 * the call slot: the host writes args 0..3 and a function address, our WM
 * calls it and publishes the return value and the call count.     [measured]
 * Handed over by the retail ROM: sp 0x02201ec0, gp 0x02212800, mstatus 0x88
 * (interrupts on), mtvec 0x00808f00, mie 0x808.                    [measured]
 * ------------------------------------------------------------------------ */
#define MT7996_PROBE_OWN_WM_STATUS        0x0090f300u
#define   MT7996_PROBE_OWN_WM_MAGIC       0x4d57574fu	/* 'OWWM' */
#define MT7996_PROBE_OWN_WM_CALL          (MT7996_PROBE_OWN_WM_STATUS + 0x40u)
#define MT7996_PROBE_OWN_WM_ARG(n)        (MT7996_PROBE_OWN_WM_STATUS + 0x48u + 4u * (n))
#define MT7996_PROBE_OWN_WM_RET           (MT7996_PROBE_OWN_WM_STATUS + 0x58u)
/* Host writes 1 once the radio is configured; our WM starts the WA and writes 2. */
#define MT7996_PROBE_OWN_WM_WA_START      (MT7996_PROBE_OWN_WM_STATUS + 0x5cu)
/* Radio commands (firmware/wm/wm.h WM_RADIO_*, firmware/radio/radio_cmd.h): EEPROM buffer
 * address (WM), REQ (host) and ACK (WM), op and four arguments (host), result, failure code,
 * argument and count, microseconds taken, state bits (WM). */
#define MT7996_PROBE_OWN_WM_RADIO_EE      (MT7996_PROBE_OWN_WM_STATUS + 0x60u)
#define MT7996_PROBE_OWN_WM_RADIO_REQ     (MT7996_PROBE_OWN_WM_STATUS + 0x64u)
#define MT7996_PROBE_OWN_WM_RADIO_ACK     (MT7996_PROBE_OWN_WM_STATUS + 0x68u)
#define MT7996_PROBE_OWN_WM_RADIO_OP      (MT7996_PROBE_OWN_WM_STATUS + 0x6cu)
#define MT7996_PROBE_OWN_WM_RADIO_ARG(n)  (MT7996_PROBE_OWN_WM_STATUS + 0x70u + 4u * (n))
#define MT7996_PROBE_OWN_WM_RADIO_RES     (MT7996_PROBE_OWN_WM_STATUS + 0x80u)
#define MT7996_PROBE_OWN_WM_RADIO_FAIL    (MT7996_PROBE_OWN_WM_STATUS + 0x84u)	/* code, arg, count */
#define MT7996_PROBE_OWN_WM_RADIO_US      (MT7996_PROBE_OWN_WM_STATUS + 0x90u)
#define MT7996_PROBE_OWN_WM_RADIO_STATE   (MT7996_PROBE_OWN_WM_STATUS + 0x94u)
/* The address of our WM's RF command builder (firmware/radio/rfctl.h rfctl_build), which the
 * control interpreter calls for MT7996_PROBE_CTL_OP_RF: our WM's main loop and the stream
 * producer, which is linked separately, reach it through this word. Written at start. */
#define MT7996_PROBE_OWN_WM_RF_BUILD      (MT7996_PROBE_OWN_WM_STATUS + 0x98u)
/* Our WA's status block (firmware/wa/wa.h), after the WM's and up to the CTL mailbox: 8 words.
 * (It was at +0x80, on top of the WM's radio words.) */
#define MT7996_PROBE_OWN_WA_STATUS        0x0090f3e0u

/* ------------------------------------------------------------------------
 * 0x0090f400: RF control mailbox (CTL)
 *
 * The host fills ops, then COUNT, CLEANUP and finally REQ (a new nonzero
 * value). The stream producer runs a slice of the program between chunks
 * (at most MT7996_PROBE_CTL_BUDGET cycles per call, never waiting inside a
 * call). When the program ends (all ops, or the first hard failure) it
 * writes STATUS, CYCLES, SOFT, CHUNK0/1, SERVED and, last and fenced,
 * ACK = REQ. Whatever REQ holds when the stream starts is taken as already
 * served, so a stale request cannot replay.
 * ------------------------------------------------------------------------ */
#define MT7996_PROBE_CTL_BASE             0x0090f400u
#define MT7996_PROBE_CTL_REQ              (MT7996_PROBE_CTL_BASE + 0x00u)	/* host: new nonzero = run */
#define MT7996_PROBE_CTL_ACK              (MT7996_PROBE_CTL_BASE + 0x04u)	/* = REQ when finished */
#define MT7996_PROBE_CTL_STATUS           (MT7996_PROBE_CTL_BASE + 0x08u)
#define   MT7996_PROBE_CTL_STATUS_CODE    HW_GENMASK(7, 0)
#define   MT7996_PROBE_CTL_STATUS_OP      HW_GENMASK(31, 8)	/* failing op index */
#define MT7996_PROBE_CTL_COUNT            (MT7996_PROBE_CTL_BASE + 0x0cu)	/* ops, <= CTL_MAX */
#define MT7996_PROBE_CTL_CYCLES           (MT7996_PROBE_CTL_BASE + 0x10u)	/* start to end, 720 MHz cycles */
#define MT7996_PROBE_CTL_SERVED           (MT7996_PROBE_CTL_BASE + 0x14u)	/* programs completed */
#define MT7996_PROBE_CTL_CALLS            (MT7996_PROBE_CTL_BASE + 0x18u)	/* idle calls, every 1024 */
#define MT7996_PROBE_CTL_SEEN             (MT7996_PROBE_CTL_BASE + 0x1cu)	/* REQ as last read while idle */
#define MT7996_PROBE_CTL_CLEANUP          (MT7996_PROBE_CTL_BASE + 0x20u)
/* A hard failure at an op in [GUARD, INDEX) jumps to INDEX and runs the
 * rest, still reporting the first failure. 0 = no cleanup. */
#define   MT7996_PROBE_CTL_CLEANUP_INDEX  HW_GENMASK(15, 0)
#define   MT7996_PROBE_CTL_CLEANUP_GUARD  HW_GENMASK(31, 16)
#define MT7996_PROBE_CTL_SOFT             (MT7996_PROBE_CTL_BASE + 0x24u)	/* soft timeouts counted */
#define MT7996_PROBE_CTL_CHUNK0           (MT7996_PROBE_CTL_BASE + 0x28u)	/* chunks published at start */
#define MT7996_PROBE_CTL_CHUNK1           (MT7996_PROBE_CTL_BASE + 0x2cu)	/* ... and at end */
#define MT7996_PROBE_CTL_OP(i)            (MT7996_PROBE_CTL_BASE + 0x40u + 16u * (i))	/* {op, a, b, c} */
#define MT7996_PROBE_CTL_RES(i)           (MT7996_PROBE_CTL_BASE + 0x840u + 4u * (i))
#define MT7996_PROBE_CTL_MAX              128u	/* ops and results end at 0x0090fe40 */
#define MT7996_PROBE_CTL_END              0x0090fe40u
#define MT7996_PROBE_CTL_BUDGET           2000u	/* cycles per call: 2.8 us at 720 MHz */
#define MT7996_PROBE_CTL_LAG_CHUNKS       32u	/* control runs only while < 32 chunks behind */

/* Op word: opcode [7:0] | flags [11:8] | arg [31:12] (DELAY/POLL time, us). */
#define   MT7996_PROBE_CTL_OP_CODE        HW_GENMASK(7, 0)
#define   MT7996_PROBE_CTL_OP_SOFT        HW_BIT(8)	/* timeout counted in SOFT, program goes on */
#define   MT7996_PROBE_CTL_OP_ARG         HW_GENMASK(31, 12)
#define   MT7996_PROBE_CTL_OP_END         0u
#define   MT7996_PROBE_CTL_OP_W32         1u	/* a = addr, b = value */
#define   MT7996_PROBE_CTL_OP_RMW32       2u	/* a = addr, b = mask, c = value: ([a] & ~b) | c */
#define   MT7996_PROBE_CTL_OP_R32         3u	/* a = addr -> res */
#define   MT7996_PROBE_CTL_OP_DELAY       4u	/* arg us */
#define   MT7996_PROBE_CTL_OP_POLL        5u	/* until ([a] & b) == c, arg us; res = last read */
#define   MT7996_PROBE_CTL_OP_AW          6u	/* A-die write: a = reg, b = value */
#define   MT7996_PROBE_CTL_OP_AR          7u	/* A-die read -> res */
#define   MT7996_PROBE_CTL_OP_ARMW        8u	/* A-die RMW: b = mask, c = value */
#define   MT7996_PROBE_CTL_OP_APOLL       9u	/* A-die poll: b = mask, c = value */
#define   MT7996_PROBE_CTL_OP_SAR         10u	/* a = die -> res = VCO band code (10-step search) */
#define   MT7996_PROBE_CTL_OP_RF          11u	/* an RF command (below) -> res = its outcome */
/* A-die register argument: die << 16 | k << 12 | index. k 0..4 chain,
 * 8..14 raw chip-select 0..6, 15 the die's TOP bank; index 12 bits. Each
 * access is one SPI transaction under the CONN semaphore (see the A-die
 * header for the SPI masters and register meanings). */
#define   MT7996_PROBE_CTL_AREG_DIE       HW_GENMASK(23, 16)
#define   MT7996_PROBE_CTL_AREG_K         HW_GENMASK(15, 12)
#define   MT7996_PROBE_CTL_AREG_INDEX     HW_GENMASK(11, 0)
#define   MT7996_PROBE_CTL_AREG(die, k, index) \
	(((unsigned int)(die) << 16) | ((unsigned int)(k) << 12) | (unsigned int)(index))
#define   MT7996_PROBE_CTL_AREG_K_RAW_CS  8u	/* k 8..14: chip-select k - 8 */
/* Status codes */
#define   MT7996_PROBE_CTL_OK             0u
#define   MT7996_PROBE_CTL_BAD_OP         1u
#define   MT7996_PROBE_CTL_TIMEOUT        2u
#define   MT7996_PROBE_CTL_TOO_MANY       3u	/* COUNT > CTL_MAX */
#define   MT7996_PROBE_CTL_SEMA           4u	/* semaphore not granted in 1 ms */
#define   MT7996_PROBE_CTL_BUSY           5u	/* SPI master busy before the transfer */
#define   MT7996_PROBE_CTL_SPI            6u	/* SPI transfer did not finish in 1 ms */
#define   MT7996_PROBE_CTL_BAD_ARG        7u	/* an RF op's command or arguments refused */
/* An RF op's res only (the program goes on): a tune whose VCO reports no
 * lock afterwards (adie/sx.h ADIE_SX_STATUS_VCO != 0). */
#define   MT7996_PROBE_CTL_RF_UNLOCKED    8u
/* Measured cost: a full LO retune is about 1.5 ms of SPI; with packing the
 * WM has about 18% of its time to spare for control. [measured] */

/* ------------------------------------------------------------------------
 * RF commands: MT7996_PROBE_CTL_OP_RF                              [static]
 *
 * In regular operation the host names no radio, PHY or A-die register: it
 * posts RF ops, and our WM expands each into its register program
 * (firmware/radio/rfctl.h; the builder rfctl_build is published at
 * MT7996_PROBE_OWN_WM_RF_BUILD) and runs that inside the op, resumably and
 * within the same budget as posted ops: DELAY, an unmet APOLL and the VCO
 * search's settling return and resume on the next call, so a 1.5 ms retune
 * never stalls a stream. A hard failure of an op of the expansion is the RF
 * op's failure (STATUS as for any op); its lock-poll timeouts count in
 * SOFT. The raw ops stay, for development and register discovery.
 *   a = RF_A_CMD [7:0] | RF_A_BAND [15:8]; b, c: the command's arguments.
 *   TUNE    b = LO Hz [31:0], c = LO Hz [63:32]; band 0 2.08..2.74 GHz,
 *           bands 1 and 2 4.65..7.41 GHz
 *   GAIN    b = LNA index 0..4, c = LPF index 0..9, forced on every chain
 *           (phy.h MT7996_PHY_RXGAIN_FORCE_AB); b = RF_GAIN_AGC hands the
 *           gain back to the AGC
 *   FILTER  b = analog RX bandwidth in MHz: 20, 40, 80, 160 or 320; on band 0
 *           (MT7976) 320 is code 3 widened (firmware/radio/rfctl.h), which
 *           also sets the RC trim; refused there before the bring-up
 *   TRIM    b = RC trim code 0..31 (adie.h ADIE_RCTRIM), every chain
 *   STATUS  the band's synthesizer, filter and gain words
 *   LOCKS   every band's synthesizer status (RF_A_BAND ignored)
 *   BAND    the captured band: ICAPCR8 of the three ICAPs = RF_A_BAND, on
 *           a running ring
 * res: MT7996_PROBE_CTL_OK; RF_UNLOCKED (TUNE); or a failure code | the
 * failing op of the expansion << 8 (MT7996_PROBE_CTL_STATUS_*), with BAD_OP
 * when our WM has not published its builder and BAD_ARG when it refuses
 * the command or its arguments.
 * ------------------------------------------------------------------------ */
#define   MT7996_PROBE_CTL_RF_A_CMD       HW_GENMASK(7, 0)
#define   MT7996_PROBE_CTL_RF_A_BAND      HW_GENMASK(15, 8)
#define   MT7996_PROBE_CTL_RF_TUNE        1u
#define   MT7996_PROBE_CTL_RF_GAIN        2u
#define   MT7996_PROBE_CTL_RF_FILTER      3u
#define   MT7996_PROBE_CTL_RF_TRIM        4u
#define   MT7996_PROBE_CTL_RF_STATUS      5u
#define   MT7996_PROBE_CTL_RF_LOCKS       6u
#define   MT7996_PROBE_CTL_RF_BAND        7u
#define   MT7996_PROBE_CTL_RF_GAIN_AGC    0xffffffffu
/*
 * The RF block, after the CTL mailbox: the report of the last RF op. Every
 * RF op rewrites it when its expansion is built; OUT(i) then fill in as its
 * ops complete.
 *   LO     TUNE: the LO the frequency words give, Hz (rounded to the
 *          nearest, ties to even); 0 for the other commands
 *   OPS    ops in the expansion
 *   OUTS   result words in OUT(0..OUTS-1): the results of its last OUTS ops
 *     TUNE    SX status (0xca4), Vtune (0xcd1), comparator (0xca5), then
 *             the done word (0xca7, MT7977) or band word (0xa01, MT7976),
 *             and 0 (the firmware's frequency note, a write)
 *     GAIN    every chain's RXGAIN read back
 *     FILTER  every chain's ADIE_RX_BW_RB
 *     TRIM    every chain's ADIE_RCTRIM as written
 *     STATUS  SX INT, FRAC, CFG and status, the frequency note, every
 *             chain's ADIE_RX_BW, then every chain's RXGAIN
 *     LOCKS   the SX status of bands 0, 1 and 2
 *     BAND    ICAP0 CR9 before, 0 three times (the CR8 writes), CR9 after
 */
#define MT7996_PROBE_CTL_RF_BASE          0x0090fe40u
#define MT7996_PROBE_CTL_RF_LO            (MT7996_PROBE_CTL_RF_BASE + 0x00u)	/* Hz [31:0] */
#define MT7996_PROBE_CTL_RF_LO_HI         (MT7996_PROBE_CTL_RF_BASE + 0x04u)	/* Hz [63:32] */
#define MT7996_PROBE_CTL_RF_OPS           (MT7996_PROBE_CTL_RF_BASE + 0x08u)
#define MT7996_PROBE_CTL_RF_OUTS          (MT7996_PROBE_CTL_RF_BASE + 0x0cu)
#define MT7996_PROBE_CTL_RF_OUT(i)        (MT7996_PROBE_CTL_RF_BASE + 0x10u + 4u * (i))
#define MT7996_PROBE_CTL_RF_OUT_MAX       16u	/* the RF block ends at 0x0090fe90 */

/* Free in the probe page: 0x0090fe90..0x0090ffff (nothing of ours uses it). */

/* ------------------------------------------------------------------------
 * WA blob slots (WA ILM tail; MCU and WA see the same addresses)
 * ------------------------------------------------------------------------ */
#define MT7996_PROBE_WA_FIRST             0x1001a400u	/* vendor WA code ends 0x1001a0d0 */
#define MT7996_PROBE_WA_LAST              0x1001bffcu
#define MT7996_PROBE_WA_CODE              0x1001a400u
#define MT7996_PROBE_WA_DONE              0x1001b400u	/* {DONE_MAGIC, count} */
#define MT7996_PROBE_WA_RES               0x1001b410u
/* The stream packer runs on its own stack (1 KiB below the done word); its
 * entry stub leaves the caller's sp at TOP-16 and ra at TOP-12. */
#define MT7996_PROBE_WA_STACK_TOP         0x1001b400u
/*
 * WM<->WA shared block during a stream, at MT7996_PROBE_WA_RES (WA ILM,
 * uncached for both cores via PMA entry 0).
 */
#define MT7996_PROBE_SH_STARTED           0x00u	/* WA: entered */
#define MT7996_PROBE_SH_GO                0x04u	/* WM: start */
#define MT7996_PROBE_SH_STOP              0x08u	/* WM: stop */
#define MT7996_PROBE_SH_EXITED            0x0cu	/* WA: returned */
#define MT7996_PROBE_SH_DONE              0x10u	/* every WA chunk below this is packed */
#define MT7996_PROBE_SH_PUBLISHED         0x14u	/* WM publish point */
#define MT7996_PROBE_SH_SENT              0x18u	/* chunks below this have left TX0 */
#define MT7996_PROBE_SH_PACKING           0x1cu	/* WM: the stream's packing schedule, before the WA is raised */
#define MT7996_PROBE_SH_STATUS            0x20u
#define   MT7996_PROBE_SH_ST_STOPPED      1u
#define   MT7996_PROBE_SH_ST_NO_GO        2u	/* go never came within 1 s */
#define   MT7996_PROBE_SH_ST_WM_STALLED   3u	/* publish point still for 1 s */
#define   MT7996_PROBE_SH_ST_NO_SCHEDULE  4u	/* the schedule has nothing for the WA to pack */
#define MT7996_PROBE_SH_PACK_MAX          0x24u
#define MT7996_PROBE_SH_PACKS             0x28u	/* u64 */
#define MT7996_PROBE_SH_PACK_CYCLES       0x30u	/* u64 */
#define MT7996_PROBE_SH_WAITS             0x38u	/* u64 */
/*
 * WA packed-chunk buffers, in WA DLM between the vendor image's data (ends
 * 0x1025e090) and its stack guard word (0x10263800). Power-on noise only.
 */
#define MT7996_PROBE_WA_PACKBUF(n)        (0x1025e100u + 7168u * (n))
#define MT7996_PROBE_WA_PACKBUF_COUNT     3u
/*
 * HAZARD: after the WA has been raised into our code (however it was raised
 * or left), the firmware's command path is dead until the next firmware load,
 * every time, and once the firmware resumes the host's read window breaks.
 * Read results through the BAR while the WM is still held in our code (the
 * producer holds 3 s for that). [measured]
 */

#endif /* HW_MT7996_PROBE_PAGE_H */
