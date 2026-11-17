/* SPDX-License-Identifier: MIT */
/*
 * MT7996 MCU-side memory map: what the WM and WA RISC-V MCUs (and, for
 * their own TCMs, the DSP cores) see, and where each firmware container's
 * regions land.
 *
 * ADDRESS SPACE: the MCU bus. The WM and WA see the same addresses for every
 * shared memory (ROM, ULM1/2, SYSRAM, CRAM, the UMAC pool, each other's local
 * memories, the DSP windows). The DSP cores' own view is in dsp.h; they reach
 * none of the memories below. Peripheral apertures are listed by name only;
 * their registers live in the headers named.
 *
 * Host view: the host reaches MCU memory through BAR remap windows (L1:
 * 64 KiB pages; L2: 4 KiB pages) and a fixed map (MCU SYSRAM 0x00400000 ->
 * BAR 0x80000 and 0x00410000 -> BAR 0x90000; UMAC SYSRAM 0x40000000 -> BAR
 * 0x70000; MCU_DMA0 0x54000000 -> BAR 0x02000). See the host PCIe headers.
 *
 * Firmware-image addresses (data words, tables, free areas) are for the
 * vendor 2026 release/test-mode WM, the 2026 WA and the current DSP image;
 * they move between builds. ROM addresses are for the retail ROM; see
 * rom_patch.h for the per-region relocation against other ROM revisions.
 *
 * Confidence tags: [measured] verified on hardware, [static] from firmware/
 * ROM code or the vendor driver's coredump tables, [inferred].
 */
#ifndef HW_MT7996_MCU_MAP_H
#define HW_MT7996_MCU_MAP_H

#include "../hw_bits.h"

/* ========================================================================
 * WM memories
 * ======================================================================== */

/* ULM0: boot ROM (mask ROM, not writable). The WM's ILM window base
 * (milmb = 0x00800001). ROM image 286,796 bytes, entry 0x0080000c. */
#define MT7996_WM_ROM_BASE                0x00800000u
#define MT7996_WM_ROM_SIZE                0x00050000u	/* 320 KiB */
#define MT7996_WM_ROM_IMAGE_SIZE          0x0004604cu
#define MT7996_WM_ROM_ENTRY               0x0080000cu
/* ROM fixed-address API pointer table, 4 bytes per index (index 735 at
 * 0x0081b77c). Read-only to firmware. [static, live-confirmed slots] */
#define MT7996_WM_ROM_API_TABLE           0x0081ac00u
#define MT7996_WM_ROM_API(idx)            (MT7996_WM_ROM_API_TABLE + 4u * (idx))

/* ULM1: patch SRAM + WM region-0 code. Seen from the host it repeats every
 * 0x40000 (256 KiB period, 224 KiB populated). [static size, measured alias] */
#define MT7996_WM_ULM1_BASE               0x00900000u
#define MT7996_WM_ULM1_SIZE               0x00038000u	/* 224 KiB */
#define MT7996_WM_ULM1_ALIAS_PERIOD       0x00040000u
/* ROM-patch section destination (the decrypted section, security footer
 * included, is copied here; hook table at its start, see rom_patch.h). */
#define MT7996_WM_PATCH_BASE              0x00900000u
/* End of the stock patch copy (37,056 bytes); our appended hook bodies
 * extend it to about 0x00909140. [measured] */
#define MT7996_WM_PATCH_COPY_END          0x009090c0u
/* Probe page: our mailbox/staging window, layout in probe_page.h. Free of
 * the patch copy; survives the full four-container load. [measured] */
#define MT7996_WM_PROBE_BASE              0x0090a000u
#define MT7996_WM_PROBE_SIZE              0x00006000u
/* Patch SRAM below WM region 0, free of the ROM, the patch and the firmware
 * images: it holds power-on noise after boot and a full load. Free only
 * until a stream packs: our WM's packers keep their packed-chunk buffers
 * 1..3 here (0x00910000..0x009153ff, probe_page.h), which MCU_DMA0 TX0 reads
 * directly, and rewrite them for as long as a packed stream runs (half, full,
 * split). A raw stream leaves the region alone. Seen on a unit: code,
 * descriptor rings and a test pattern put at 0x00910000..0x009120ff stayed
 * intact through a 40 Msps raw stream and were overwritten from the start of
 * a 160 Msps full one. [measured] */
#define MT7996_WM_ULM1_FREE_BASE          0x00910000u
#define MT7996_WM_ULM1_FREE_SIZE          0x00006000u	/* to 0x00915fff */
/* WM region-0 code (vendor 2026 release and test mode): 0x00916400..0x00937d0f. */

/* ULM2: the WM's DLM window (mdlmb = 0x02200001): ROM/WM shared data,
 * descriptor rings, network buffers. Off-limits for our data. */
#define MT7996_WM_ULM2_BASE               0x02200000u
#define MT7996_WM_ULM2_SIZE               0x00040000u	/* 256 KiB */
/*
 * Inferred from the Andes ILM/DLM size fields: ILM window 2 MiB
 * (0x00800000..0x009fffff, covering ROM and ULM1), DLM window 1 MiB
 * (256 KiB populated). [inferred]
 */
/* Known ULM2 contents (retail ROM data; firmware build as noted): */
#define MT7996_WM_ROM_GP                  0x02212800u	/* ROM global pointer [static/measured] */
#define MT7996_WM_ROM_STACK_LO            0x0220d400u	/* ROM stack 0x0220d400..0x0220e000 [static] */
#define MT7996_WM_ROM_STACK_HI            0x0220e000u
/* The ROM's FIT_TABLES (_fit_tables, ROM gp - 0x4800): the GCI register-field
 * interface and one module interface per hardware block (conn.h "GCI").
 * [static] */
#define MT7996_WM_ROM_FIT_TABLES          0x0220e000u
/* WM region-5 data load address 0x0220e800 (a startup copy moves it up). */
/* .wifi_rom_data from 0x0222b434, .wifi_rom_bss from 0x0222b558. Pool layout
 * globals [measured live]: */
#define MT7996_WM_G_WSICAP_POS            0x0222b434u	/* u8 */
#define MT7996_WM_G_PSE_POS               0x0222b435u	/* u8 */
#define MT7996_WM_G_PSE_BANKS             0x0222b436u	/* u16 */
#define MT7996_WM_G_PLE_BANKS             0x0222b438u	/* u16 */
#define MT7996_WM_G_PLE_POS               0x0222b558u	/* u8 */
#define MT7996_WM_G_ICAP_BANKS            0x0222b55au	/* u16 */
/* WM SYSRAM profile table in the release WM's data: entries
 * {u16 id, u16 rsv, u16 PLE banks, u16 PSE banks}; id 5 at +0x10. (The
 * 2024 test-mode WM has it at 0x0222391c.) [static/measured] */
#define MT7996_WM_PROFILE_TABLE           0x02223f84u
/* MCU_DMA0 TX0 firmware ring state and descriptors: see mcu_dma.h. */

/* SYSRAM (WF_MCU_SYSRAM): non-cacheable memory (PMA entry 0). Seen from the
 * host, 0x00404000..0x0040efff (where WM BSS loads) reads as one repeated
 * 8-byte bus latch value per page, and 0x00468000..0x0047ffff is a latch of
 * the last bus value, not memory. The MCU and host views of 0x00464000 are
 * the same memory word for word with no cache maintenance. MCU access
 * 70-71 MB/s (0.097-0.099 B/cycle, ~41 cycles per 32-bit access). [measured] */
#define MT7996_WM_SYSRAM_BASE             0x00400000u
#define MT7996_WM_SYSRAM_SIZE             0x00068000u	/* 416 KiB */
/* WF_SW_DEF block shared by host, WM and WA (see mcu_irq.h). */
#define MT7996_WM_SYSRAM_SWDEF            0x00401400u
/* WM BSS: region 6 0x00404000..0x004071cf, region 1 0x00407200..0x0040f1cf. */
/* CSI area (bridge registry type 0 id 2), 0x0040f200 + 0x10000: reserved
 * and unused on the test-mode WM (random power-up content). [measured] */
#define MT7996_WM_SYSRAM_CSI_BASE         0x0040f200u
#define MT7996_WM_SYSRAM_CSI_SIZE         0x00010000u
/* Radar pulse buffer (the channel set's BW320 background receiver start, phy_chan.h):
 * MediaTek's SYSRAM bridge registry (WM .data 0x022244a0, {type, -, id, address, size, ...}
 * x 14; V:e01311ca) entry type 0 id 3 is the radar area 0x0041f200 + 0x800, right after the
 * CSI area (MT7996_WM_SYSRAM_CSI_BASE + _SIZE); detector 2's buffer
 * is its second 0x200 (V:e00ad570), the AHB window START = buffer, END = buffer + 0x200
 * (V:e00ecfac). MediaTek clears the buffer (memset, V:e00ad60a) before the start; our firmware
 * uses none of this SYSRAM and reads no pulse record. [static][measured] */
#define MT7996_WM_SYSRAM_RDD_BASE         0x0041f200u
#define MT7996_WM_SYSRAM_RDD_SIZE         0x00000800u
#define MT7996_WM_SYSRAM_RDD_BUF_SIZE     0x00000200u
#define MT7996_WM_SYSRAM_RDD2_BUF         (MT7996_WM_SYSRAM_RDD_BASE + MT7996_WM_SYSRAM_RDD_BUF_SIZE)
/* Capture window: 16 KiB, 8 banks x 2 KiB, page/lane interleaved
 * (1024 x 128-bit beats: low 64 bits at +(b >> 9) * 0x2000 + (b & 511) * 8,
 * high 64 bits +0x1000). Host readable. Capture registers: see the ICAP
 * headers. [measured] */
#define MT7996_WM_SYSRAM_CAPWIN_BASE      0x00464000u
#define MT7996_WM_SYSRAM_CAPWIN_SIZE      0x00004000u
/* HAZARD: while capture EN is set, each capture-window page reads as its
 * own last 8 bytes repeated 512 times. Clear EN, then read. [measured] */

/* ========================================================================
 * CRAM (shared by WM and WA; write-back cached by each, PMA entry 10)
 * ======================================================================== */
#define MT7996_MCU_CRAM0_BASE             0xe0000000u
#define MT7996_MCU_CRAM0_SIZE             0x00160000u	/* 1408 KiB */
#define MT7996_MCU_CRAM1_BASE             0xe0160000u
#define MT7996_MCU_CRAM1_SIZE             0x0011c000u	/* 1136 KiB; CRAM ends 0xe027bfff */
/* The WA's slice, then WM code/rodata/data/BSS from 0xe0021800. No WM<->WA
 * coherence mechanism has been observed. [static; coherence inferred]
 * HAZARD: the ROM's download writes its command packets into 2 KiB buffers
 * in CRAM: after a WM download, 32-bit words at 0xe002a000, 0xe002a800,
 * 0xe002f800, 0xe0030000 and 0xe0030800 held packet headers (0x3800002c,
 * 0x4100004c) over the region the download had just written there, and our
 * WM trapped on the corrupted code. MediaTek's test-mode WM loads nothing
 * below MT7996_WM_FW_CRAM_FREE_BASE; ours starts there too. [measured] */
#define MT7996_WM_FW_CRAM_FREE_BASE       0xe0040000u
#define MT7996_WA_CRAM_BASE               0xe0000000u
#define MT7996_WA_CRAM_SIZE               0x00021800u	/* 134 KiB */
/*
 * Note: 0xc0000000 as an "uncached alias of 0xe0000000" is unproven; no PMA
 * entry covers 0xc0000000..0xcfffffff. [inferred, weak]
 */

/* ========================================================================
 * UMAC frame pool (PLE / PSE / ICAP shared SRAM)
 * ======================================================================== */
/*
 * Flat MCU view: pool byte n is at 0xe0400000 + n in every partition.
 * Write-back cacheable on the MCUs (PMA entry 10); DMA and ICAP writes are
 * not snooped. The host's 0x40000000..0x4000ffff (BAR 0x70000) mirrors the
 * first 64 KiB, uncached. [measured]
 */
#define MT7996_MCU_POOL_BASE              0xe0400000u
#define MT7996_MCU_POOL_SIZE              0x00160000u	/* 1408 KiB */
#define MT7996_MCU_POOL_BANK_SIZE         0x800u	/* 2 KiB in every page-size mode */
#define MT7996_MCU_POOL_BANKS             704u
#define MT7996_MCU_POOL_BANK(n)           (MT7996_MCU_POOL_BASE + MT7996_MCU_POOL_BANK_SIZE * (n))
/*
 * Partitions are laid out in ascending "position" order; a region's base
 * bank is the sum of the bank counts at lower positions. The UMAC
 * PBUF_OFFSET registers count in 2 KiB banks.
 * Profile 4 (stock boot): PLE 256 banks @0 (64 B pages), ICAP 0,
 *   PSE 448 @256 (256 B pages).
 * Profile 5 (the release WM's unused arm): ICAP 256 @0
 *   (0xe0400000..0xe047ffff, 512 KiB, no other writer), PLE 192 @256
 *   (0xe0480000..0xe04dffff), PSE 256 @448 (0xe04e0000..0xe055ffff).
 * Our ROM patch's layout hook (firmware/rom_patch: banks 512/64/128, which
 *   must total 704): ICAP gets 1 MiB (0xe0400000..0xe04fffff), the most its
 *   18-bit address (4-byte units) can reach; PLE at 0xe0500000, PSE at
 *   0xe0520000. The 1 MiB capture ring streams in it. [measured]
 * Profiles 0 and 6 = 256/448 PLE/PSE. The stock test-mode WM never selects
 * profile 5. [measured unless tagged]
 */
#define MT7996_MCU_POOL_P5_ICAP_BANKS     256u
#define MT7996_MCU_POOL_P5_PLE_BANKS      192u
#define MT7996_MCU_POOL_P5_PSE_BANKS      256u
#define MT7996_MCU_POOL_P4_PLE_BANKS      256u
#define MT7996_MCU_POOL_P4_PSE_BANKS      448u
/* Frame ports (partition-relative, 13-bit frame id): frame 0 is the region
 * base; frames >= 1 are NOT at flat +0x800 * fid. The two ports are not
 * aliases. [measured] */
#define MT7996_MCU_PLE_FRAME(fid)         (0xa0000000u | ((unsigned int)(fid) << 15))
#define MT7996_MCU_PSE_FRAME(fid)         (0xb0000000u | ((unsigned int)(fid) << 15))
/*
 * Throughput (720 MHz): WM cold read 455 MB/s, hot 2.87 GB/s, with the
 * D-cache prefetcher 1.45 GB/s (WA 1.47); write 440 MB/s, with prefetch +
 * write-around 1.36 GB/s; copy 223 MB/s (595 MB/s with both). WM + WA core
 * reads saturate together at ~0.6 GB/s (the cores' path, not the pool). The
 * pool sustains an ICAP write of 1.28 GB/s + MCU_DMA0 reads + ~0.6 GB/s core
 * reads at once. [measured]
 * HAZARD: the WM and WA both writing packed output into the pool lapped the
 * capture within 1-5 ms and twice wedged the PCIe fabric (completion
 * timeouts, power cycle). Write outputs to local memory. [measured]
 * HAZARD: a host read of the PSE register block (0x820c8000) hard-locked the
 * host and wedged the MT7996 past FLR, secondary bus reset and PERST#; only
 * a power cycle cleared it. A host 256-byte write to 0x40000000 also
 * hard-locked the host. [measured]
 * HAZARD: a host pool read right after ICAP ring tests that pulsed its
 * SWITCH control hit a PCIe completion timeout; reset and reload failed,
 * power cycle. [measured]
 * HAZARD: the MCU reads 0xe0400000+ through its D-cache: invalidate between
 * reads that straddle a DMA/ICAP write (or evict: read 4 KiB at
 * 0xe0400000 + k * 0x10000, k = 1..8). [measured]
 */

/* ========================================================================
 * WA memories (the WA's own view; the WM reaches them at the same addresses)
 * ======================================================================== */
#define MT7996_WA_ILM_BASE                0x10000000u	/* milmb = 0x10000001 */
#define MT7996_WA_ILM_SIZE                0x0001c000u	/* 112 KiB */
#define MT7996_WA_DLM_BASE                0x10200000u	/* mdlmb = 0x10200001 */
#define MT7996_WA_DLM_SIZE                0x00064000u	/* 400 KiB */
/* Above the DLM, from 0x10264000, the space mirrors with a 16 KiB period. */
#define MT7996_WA_DLM_MIRROR_BASE         0x10264000u
#define MT7996_WA_DLM_MIRROR_PERIOD       0x00004000u
/* Vendor WA image (2026): ILM code 0x10000000..0x1001a0cf (entry
 * 0x10000000), DLM data 0x10200000..0x1025e08f. [static] */
#define MT7996_WA_ILM_FREE_BASE           0x1001a0d0u	/* to the ILM end; probe_page.h */
#define MT7996_WA_DLM_FREE_BASE           0x1025e100u	/* power-on noise survives boot+load */
#define MT7996_WA_DLM_FREE_END            0x10263800u
/* Vendor WA stack, guard word at its base. Off-limits. [measured] */
#define MT7996_WA_STACK_BASE              0x10263800u
#define MT7996_WA_STACK_TOP               0x10264000u
/* WA soft-interrupt {bit, fn} table in DLM (see mcu_irq.h). */
#define MT7996_WA_SWI_TABLE               0x102003b0u
/* From the WM, 0x10000004 reads 0x00028463 (the WA image). [measured] */

/* ========================================================================
 * DSP windows (MCU view): see dsp.h for MT7996_DSP_MCU_ITCM(b),
 * MT7996_DSP_MCU_DTCM(b) (0x86000000 / 0x86200000 + b * 0x400000, 64 KiB
 * each, aliasing every 64 KiB, 35 MB/s) and the control block
 * 0x86c00000 + b * 0x100000.
 * ======================================================================== */
#define MT7996_DSP_WIN_BASE               0x86000000u
#define MT7996_DSP_WIN_STRIDE             0x00400000u
#define MT7996_DSP_TCM_SIZE               0x00010000u

/* Unpopulated MCU-bus addresses: 0x00100000..0x0015ffff, 0x00200000,
 * 0x00300000, 0x00500000..0x00700000 and 0x00a00000..0x01000000 read zero
 * from the host. 0x00100000 reading stable zero is a health check: after a
 * wedge or FLR every read returns fresh noise. [measured] */
#define MT7996_MCU_HEALTH_ZERO_ADDR       0x00100000u

/* ========================================================================
 * Peripheral apertures used by the MCUs (names only; see their headers)
 * ======================================================================== */
#define MT7996_MCU_AP_UMAC_SYSRAM         0x40000000u	/* device class; host pool mirror */
#define MT7996_MCU_AP_MCU_DMA0            0x54000000u	/* mcu_dma.h */
#define MT7996_MCU_AP_MCU_DMA1            0x55000000u
#define MT7996_MCU_AP_WFDMA_WRAP          0x57000000u
#define MT7996_MCU_AP_MEM_DMA             0x58000000u	/* mcu_dma.h */
#define MT7996_MCU_AP_PCIE1_DMA1          0x59000000u
#define MT7996_MCU_AP_SEC_EFUSE           0x74140000u
#define MT7996_MCU_AP_CONN_INFRA          0x7c000000u	/* DIC mailbox, semaphores, SPI masters */
#define MT7996_MCU_AP_WF_TOP_MISC_OFF     0x80020000u	/* clocks, ICAP blocks */
#define MT7996_MCU_AP_WF_TOP_MISC_ON      0x81020000u	/* DSP enable/reset, oscillators */
#define MT7996_MCU_AP_WFSYS_ON            0x820b0000u
#define MT7996_MCU_AP_UMAC                0x820c0000u	/* PLE/PSE/PP, mac.h */
#define MT7996_MCU_AP_PHY                 0x83000000u	/* + band << 20 */
#define MT7996_MCU_AP_CMDBT               0x87000000u	/* register-table sequencer, not a CPU */
#define MT7996_MCU_AP_MCU_CFG_LS          0x88000000u	/* WA reset (mcu_periph.h), capture route +0x9000 */
#define MT7996_MCU_AP_MCU_PERIPH          0x89000000u	/* CIRQ, ROM-patch comparators, EXCP */
/*
 * HAZARD: an unqualified host read or write of a WFSYS-domain address can
 * hard-lock the host and wedge the MT7996 past FLR / secondary bus reset /
 * PERST#; only a physical power cycle clears the device. First touch of any
 * unrecorded address must be a guarded read. [measured]
 * HAZARD: 0xdead2117 reads split by host translation path: the WiFi-die
 * fixed-map windows and the whole L2 window poison while the L1 window stays
 * healthy and the firmware keeps running; REG_ACCESS still works. [measured]
 */

/* ========================================================================
 * Firmware region destinations
 * ======================================================================== */
/*
 * Four containers, all RISC-V: ROM patch (1 section), WM (9 regions),
 * DSP (6), WA (3). Load order: patch -> WM + START(override) -> DSP +
 * START(| PDA_DSP) -> WA + START(| PDA_CR4). Region feature byte 0x01
 * (encrypted) except WM region 0 = 0x21 (encrypted + override address = the
 * WM start address). Region AES-128-CBC, zero key and IV. Sizes include
 * each region's 16-byte trailer. [static]
 */
#define MT7996_WM_FW_REGIONS              9u
#define MT7996_WA_FW_REGIONS              3u
#define MT7996_DSP_FW_REGIONS             6u

/* WM, vendor 2026 release image [static] */
#define MT7996_WM_FW_R0_BASE              0x00916400u	/* ULM1 code, entry */
#define MT7996_WM_FW_R0_SIZE              0x00021910u
#define MT7996_WM_FW_R1_BASE              0x00407200u	/* SYSRAM BSS */
#define MT7996_WM_FW_R1_SIZE              0x00007fd0u
#define MT7996_WM_FW_R2_BASE              0xe0088000u	/* CRAM main code */
#define MT7996_WM_FW_R2_SIZE              0x000b3bd0u
#define MT7996_WM_FW_R3_BASE              0xe013bc00u	/* CRAM rodata */
#define MT7996_WM_FW_R3_SIZE              0x000c2fd0u
#define MT7996_WM_FW_R4_BASE              0xe01fec00u	/* CRAM BSS */
#define MT7996_WM_FW_R4_SIZE              0x0007cfd0u
#define MT7996_WM_FW_R5_BASE              0x0220e800u	/* ULM2 data (load address) */
#define MT7996_WM_FW_R5_SIZE              0x0001c3d0u
#define MT7996_WM_FW_R6_BASE              0x00404000u	/* SYSRAM BSS */
#define MT7996_WM_FW_R6_SIZE              0x000031d0u
#define MT7996_WM_FW_R7_BASE              0xe005c400u	/* CRAM data */
#define MT7996_WM_FW_R7_SIZE              0x00023bd0u
#define MT7996_WM_FW_R8_BASE              0xe0031800u	/* CRAM RF/calibration code */
#define MT7996_WM_FW_R8_SIZE              0x0002a6d0u
/* The 2026 test-mode WM differs in CRAM only: r2 0xe0095c00 + 0xaf3d0,
 * r3 0xe0145000 + 0x833d0, r4 0xe01c8400 + 0x42fd0, r7 0xe006f800 +
 * 0x263d0, r8 0xe0040000 + 0x2a610. */

/* WA, vendor 2026 image [static] */
#define MT7996_WA_FW_R0_BASE              0x10000000u	/* ILM code, entry */
#define MT7996_WA_FW_R0_SIZE              0x0001a0d0u
#define MT7996_WA_FW_R1_BASE              0x10200000u	/* DLM data */
#define MT7996_WA_FW_R1_SIZE              0x0005e090u
#define MT7996_WA_FW_R2_BASE              0xe0000000u	/* CRAM code */
#define MT7996_WA_FW_R2_SIZE              0x00004650u

/* DSP: per band b, code to the ITCM window and data to the DTCM window;
 * the three pairs are byte-identical. Loader-side windows only; the image
 * is linked at 0 (reset vector jumps to 0x8). [static] */
#define MT7996_DSP_FW_CODE_BASE(b)        (0x86000000u + ((unsigned int)(b) << 22))
#define MT7996_DSP_FW_CODE_SIZE           0x0000e390u	/* 58,256 bytes */
#define MT7996_DSP_FW_DATA_BASE(b)        (0x86200000u + ((unsigned int)(b) << 22))
#define MT7996_DSP_FW_DATA_SIZE           0x000016d0u	/* 5,840 bytes */

/* ROM patch: one section, type 0x30002, to MT7996_WM_PATCH_BASE; a
 * download to 0x00900000 is what selects the patch path. See rom_patch.h. */

#endif /* HW_MT7996_MCU_MAP_H */
