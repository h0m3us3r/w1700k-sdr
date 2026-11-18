/* SPDX-License-Identifier: MIT */
/*
 * MT7996 boot ROM and its patch mechanism: ROM revision relocation, the
 * ROM-patch container and hook table, the hardware instruction-redirect
 * comparators, documented hook points (vendor and ours), and the download
 * gates (PDA CRC, signature, eFuse).
 *
 * ADDRESS SPACE: WM MCU bus. The comparators (0x8904xxxx) are MCU
 * peripherals programmed by the ROM from the patch's table; the patch
 * container lands in patch SRAM (mcu_map.h).
 *
 * ROM REVISIONS. Addresses here are for the RETAIL ROM unless tagged [SDK].
 * Retail text is relocated against the vendor SDK ROM by a different delta
 * per region, so no single offset converts between them: +0x98 (0x83f...
 * rodata: ICAP control-register map table), +0xc8 (0x844... rodata), -0x230
 * (the security slice), +0x96 (download handler pointers), +0x24 (WiFi
 * subsystem power-on / download decrypt), +0x18 (halUmacLib*, scatter and
 * PDA leaves), +0x3b6 (cos_delay_usec). Data-table contents are identical.
 * Read ROM addresses live (or from this part's ROM dump); never reuse an
 * address from another ROM revision. [measured]
 *
 * Confidence tags: [measured] verified on hardware, [static] from ROM/patch
 * code, [inferred].
 */
#ifndef HW_MT7996_ROM_PATCH_H
#define HW_MT7996_ROM_PATCH_H

#include "../hw_bits.h"

/* ========================================================================
 * Patch container (the file the host downloads)
 * ======================================================================== */
/*
 * Container header/section layout: fw_format.h. One section, AES-128-CBC
 * with an all-zero key and IV over the whole section, destination
 * 0x00900000. The last 0x400 decrypted bytes are a security footer
 * {u32 pubkey_off = 0x40, u32 sig_off = 0x300, ..., +0x0a key selector,
 * +0x40 256-byte BE modulus, +0x300 256-byte BE signature} (RSA-2048,
 * e = 65537, RSASSA-PSS, SHA-256, MGF1-SHA-256, salt 32, over bytes
 * 0..footer+0x2ff). The ROM copies the whole decrypted section, footer
 * included, to patch SRAM. Download: SEM_GET (0x10) -> per region INIT
 * (0x05) + scatter -> PATCH_FINISH (0x07) -> SEM_RELEASE; semaphore reply
 * 0 fail, 1 already downloaded, 2 granted, 3 released. [static/measured]
 */
#define MT7996_ROMPATCH_FOOTER_SIZE       0x400u
#define MT7996_ROMPATCH_FOOTER_PUBKEY_OFF 0x40u
#define MT7996_ROMPATCH_FOOTER_SIG_OFF    0x300u
/*
 * HAZARD: authentication of the patch is NOT enforced on parts with
 * unprovisioned security fuses (blank key-hash banks): a self-signed patch
 * and one whose vendor signature no longer verifies both loaded. Do not
 * assume this on a provisioned part. [measured]
 */

/* ========================================================================
 * Hook table: s_hw_entry_table, at the start of the patch SRAM copy
 * ======================================================================== */
#define MT7996_ROMPATCH_TABLE             0x00900000u
#define MT7996_ROMPATCH_TABLE_SIZE        0x208u
#define MT7996_ROMPATCH_BITMAP_LO         0x00900000u	/* enables, entries 0..31 */
#define MT7996_ROMPATCH_BITMAP_HI         0x00900004u	/* enables, entries 32..63 */
#define MT7996_ROMPATCH_FROM(i)           (0x00900008u + 4u * (i))	/* ROM address */
#define MT7996_ROMPATCH_TO(i)             (0x00900108u + 4u * (i))	/* replacement */
#define MT7996_ROMPATCH_ENTRIES           64u
/*
 * The entry count is DERIVED: 32 - clz(highest non-zero bitmap word), and
 * entries 0..count-1 are all programmed whatever their enable bits say.
 * HAZARD: keep entries packed from index 0 (bitmap 2^n - 1); a gap is
 * programmed from whatever FROM/TO hold. [static]
 * Vendor patches use 25 entries (2024) or 26 (2025-02 on); ours go at
 * index 26 and 27. Every vendor FROM is a ROM function entry and every
 * vendor replacement is a whole function returning to the ROM caller. [static]
 */

/* ========================================================================
 * Hardware redirect comparators (programmed by the ROM at patch finish)
 * ======================================================================== */
/*
 * Two-stage PC transfer per entry i (off = 8 * i, tramp = 0x00850000 + off):
 * a fetch at FROM continues at tramp, and a fetch at tramp continues at TO.
 * Nothing is physically fetched from the tramp window. The redirect
 * transfers the PC; it does not substitute one instruction. RW. [static,
 * behaviour measured]
 */
#define MT7996_ROMPATCH_TRAMP_BASE        0x00850000u
#define MT7996_ROMPATCH_TRAMP(i)          (MT7996_ROMPATCH_TRAMP_BASE + 8u * (i))
#define MT7996_ROMPATCH_CMP1_ADDR(i)      (0x89040720u + 8u * (i))	/* = FROM */
#define MT7996_ROMPATCH_CMP1_DELTA(i)     (0x89040724u + 8u * (i))	/* = tramp - FROM */
#define MT7996_ROMPATCH_CMP2_ADDR(i)      (0x89040920u + 8u * (i))	/* = tramp */
#define MT7996_ROMPATCH_CMP2_DELTA(i)     (0x89040924u + 8u * (i))	/* = TO - tramp */
#define MT7996_ROMPATCH_CMP1_EN_LO        0x89040700u	/* entries 0..31 */
#define MT7996_ROMPATCH_CMP1_EN_HI        0x89040704u	/* entries 32..63 */
#define MT7996_ROMPATCH_CMP2_EN_LO        0x89040708u	/* second stage, 64..95 */
#define MT7996_ROMPATCH_CMP2_EN_HI        0x8904070cu	/* second stage, 96..127 */
/*
 * The second-stage enables are built by read-modify-write per bit, so they
 * ACCUMULATE across patch loads within one boot; only a subsystem reset
 * clears them. [measured]
 * HAZARD: the 0x89040000 aperture is otherwise unswept from the host;
 * treat a host read there as aperture work. [static]
 */
/* Programming routine SYSpatch_enable_hw_entry (retail; [SDK] 0x0080aa68),
 * reached from PATCH_FINISH via the patch-receive copy with interrupts
 * masked. [static] */
#define MT7996_ROM_SYSPATCH_ENABLE_HW_ENTRY 0x0080afc0u

/*
 * WRITING A REPLACEMENT [measured]:
 *  - Shape: our code, then a copy of the instruction at FROM, then an
 *    absolute jump to FROM + len so the rest of the function runs from ROM.
 *    The copy goes LAST when the hook must act before the original (e.g. to
 *    rewrite a register the original stores).
 *  - The copied instruction must be position independent: lui, li, addi,
 *    c.lw, c.mv and the Andes gp-relative forms are; jal, auipc, branches
 *    and CoDense exec.it/ex9.it are not.
 *  - HAZARD: exec.it/ex9.it operands come from the CoDense table (uitb),
 *    which neither ROM ever writes except a deep-sleep save/restore; they
 *    cannot be resolved offline. Hook past a CoDense prologue, not on it.
 *  - ra is free only if the hooked function already saved it; at a function
 *    entry it has not.
 *  - HAZARD: a raw .text extract (objcopy -O binary) applies no relocations;
 *    a surviving relocation becomes a branch-to-self that reads as correct
 *    code and hangs the MCU. Assemble with -mno-relax and refuse any
 *    relocation.
 *  - HAZARD: some single instructions refuse a hook: the WM then never
 *    starts (-110 at START), even with a functional no-op replacement and a
 *    stock WM. Alignment, from/to congruence, scratch register, body growth,
 *    entry count and patch-finish reachability are all ruled out. Test a new
 *    site with a no-op and a stock WM (one boot); if refused, move one
 *    instruction.
 */
#define MT7996_ROMPATCH_SITE_OK_LAYOUT    0x00822b94u	/* shgp a0, word: works */
#define MT7996_ROMPATCH_SITE_REFUSED_1    0x008264e2u	/* c.sub a0,a1, half: refused */
#define MT7996_ROMPATCH_SITE_OK_PDACRC    0x008264e4u	/* sltiu a0,a0,1, word: works */
#define MT7996_ROMPATCH_SITE_REFUSED_2    0x00833814u	/* c.mv a0,s0, word: refused */
#define MT7996_ROMPATCH_SITE_OK_SCATTER   0x00838caau	/* c.mv s2,a0, half: works */

/* ========================================================================
 * Documented hook points (ours) [measured]
 * ======================================================================== */
/*
 * PDA-CRC hook: inside halWfdmaPdaCheckCRC (retail 0x008264d0), at the
 * final `sltiu a0, a0, 1`. Body: c.li a0, 0; the copied sltiu; lui/jalr back
 * to 0x008264e8 (the ROM's own ret). Defeats ONLY the CRC comparison:
 * wait-idle, length and the CRC-valid-bit tests keep refusing a download
 * that did not land. With it, a modified WM boots to FW_STATE 7 while
 * PDA_CRC_VAL (0x54000814, host BAR 0x02814) still reads nonzero, which is
 * itself the proof the hook ran (no evidence block).
 *
 * Where our hooks sit in the table and in patch SRAM is the build's to say
 * (firmware/rom_patch/patch.py): our patch is an empty table to which the
 * PDA-CRC, SYSRAM-layout and start-mask hooks are added in that order, so they
 * are entries 0, 1 and 2, each replacement appended after the one before. (An
 * earlier scheme added two of them to MediaTek's patch as entries 26 and 27,
 * at 0x00908cc0 and 0x00908cd0; nothing builds that any more.)
 */
#define MT7996_ROMPATCH_HOOK_PDACRC_FROM  0x008264e4u
#define MT7996_ROMPATCH_HOOK_PDACRC_RET   0x008264e8u
/*
 * SYSRAM-layout hook: one instruction into halUmacLibPlePseSysramInit
 * (entry 0x00822b92, ROM API index 746), at `shgp a0, 0x18c38` (the store of
 * the PLE bank count). Calls halUmacLibSetSysramLayout(PLE 1, PSE 2,
 * ICAP 0) (0x00822a2e, API 735; returns 1 and stores nothing if two
 * positions are equal) and halUmacLibSetWsIcapBankSysramNum(ICAP banks)
 * (0x00822a50, API 736), rewrites the PLE/PSE bank arguments so that the
 * three total the 704-bank pool, then runs the copied shgp. The counts are
 * the build's (patch.py): ICAP 512 (the 1 MiB capture ring), PLE 64, PSE
 * 128. (Earlier builds used 256 / 192 / 256.) The only partition where ICAP
 * owns pool banks. Writes the evidence block at 0x0090a000 (probe_page.h).
 * Its place in the table: see the PDA-CRC hook above.
 */
#define MT7996_ROMPATCH_HOOK_LAYOUT_FROM  0x00822b94u
#define MT7996_ROM_UMAC_SET_LAYOUT        0x00822a2eu	/* API 735 */
#define MT7996_ROM_UMAC_SET_ICAP_BANKS    0x00822a50u	/* API 736 */
#define MT7996_ROM_UMAC_PLE_PSE_INIT      0x00822b92u	/* API 746 */
/* Download protocol (fw_format.h): the event routine (seq, status) that answers
 * a FW_START, and the sequence of the completing FW_START, whose reply the ROM
 * leaves to the started firmware. Retail ROM. [measured] */
#define MT7996_ROM_FWDL_EVENT             0x0083891cu
#define MT7996_ROM_FWDL_START_SEQ         0x0222b588u	/* u8, g_wifi_start_cmd_seq_num */
/* WifiCPStart / WifiCPStop: MT7996_MCU_CFG_WM_WA = 1 / 0 (mcu_periph.h). [static] */
#define MT7996_ROM_WIFI_CP_START          0x0083988eu
#define MT7996_ROM_WIFI_CP_STOP           0x00839898u

/*
 * Vendor hook set (retail FROM addresses, all function entries; frozen
 * 2024-2026 apart from one 2025 addition) [static]:
 *   0x809de0 cos_set_reset_handler (patch main init; 2025+ adds the eFuse
 *            recovery below), 0x80fe3e GPT_EINT_Handler, 0x80fde4 GPT_Stop,
 *   eFuse: 0x810a36, 0x8109d2, 0x80ea90, 0x80e7ec, 0x811170, 0x810e40,
 *            0x810de0, (2025+) 0x8113b2 SYSefuse_Write_efuse,
 *   0x8116ba sys_pwm_set_blink_mode, 0x838c0e fwDownloadAESDecrypt,
 *   0x813348 pvPortMalloc, 0x839bcc wsysMboxHandleMsg, 0x826756
 *            halWfPktDequeue,
 *   WTBL: 0x82c314, 0x82c386, 0x82c1c8, 0x82c26e, 0x82e960, 0x82e880,
 *            0x82e83a, 0x82e130, 0x82c0e0, 0x82ddd0.
 * Every vendor patch also hooks the WDT external-polarity setter.
 */

/* ========================================================================
 * ROM download gates at START
 * ======================================================================== */
/*
 * fwDownloadWifiStart: scatter completion (PDA wait-idle 5 / length 1 /
 * CRC 2, see mcu_dma.h) is computed first, then mtk_sec_boot; 0x6b
 * (secure boot disabled) and 0x64 (success) accept, anything else forces
 * result 6. Result 6 is NOT reported: the ROM prepares the completion event
 * and then spins forever, so the host times out (-110). Hence: -110 at
 * START = signature failure; status 2 = signature passed, CRC failed.
 * [static/measured]
 */
#define MT7996_ROM_SECBOOT_OK             0x64u
#define MT7996_ROM_SECBOOT_PUBKEY_FAIL    0x65u
#define MT7996_ROM_SECBOOT_RSA_EXP_FAIL   0x66u
#define MT7996_ROM_SECBOOT_SERIALIZE_FAIL 0x67u
#define MT7996_ROM_SECBOOT_PSS_FAIL       0x68u
#define MT7996_ROM_SECBOOT_LEN_ALIGN      0x69u	/* length not 64-aligned */
#define MT7996_ROM_SECBOOT_SHA_FAIL       0x6au
#define MT7996_ROM_SECBOOT_DISABLED       0x6bu	/* accepted */
#define MT7996_ROM_SECBOOT_SHA_MISMATCH   0x6cu
#define MT7996_ROM_START_SPIN             0x00838dc4u	/* where a signature failure spins */
/* Firmware state word in host BAR (bits [2:0] = 7 when ready): host header. */
#define MT7996_ROM_FW_STATE_RDY           7u

/* Security state words and the SHA engine: secure.h. On parts with
 * unprovisioned fuses the key-hash banks read zero and the pre-auth
 * register-write gate (0x74140024 bit 6) is open. [measured] */

/* ========================================================================
 * eFuse gate bit burned by post-2025 vendor patches
 * ======================================================================== */
/*
 * Vendor patches from 2025 run a D-die eFuse "FT mapping recovery"
 * unconditionally at every boot (from the patch's reset handler). Gate:
 * physical D-die block 0x2b0 (logical 0x2a0) word 0: exit if bit 14 or 15 is
 * set, or if the low byte (FT version) is not 1 or 2. Otherwise it programs
 * rows 0x3e0/0x3d0/0x3c0 (all-zero data on a healthy part) and then burns
 * bit 14 (healthy) or bit 15 (recovered) of that word, closing the gate for
 * every later boot. An A-die variant runs only if strap 0x700056f0 bit 15 is
 * set. [static]
 * HAZARD: loading a 2025+ vendor ROM patch PERMANENTLY burns an eFuse bit.
 * Read the gate word only via the firmware's eFuse command (UNI 0x2d,
 * 16-byte block 0x2a0). [static]
 * HAZARD: never drive the eFuse controller at 0x70021000 from the host:
 * read and program differ by one mode bit. [static]
 */
/* Gate word fields: fw_format.h / secure.h (MT7996_EFUSE_FT_GATE_*). */

#endif /* HW_MT7996_ROM_PATCH_H */
