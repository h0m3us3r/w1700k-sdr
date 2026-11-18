/* SPDX-License-Identifier: MIT */
/* Our WM: where it lives and what it publishes. The status block sits in the mailbox page
 * (inside the host's CTL window), above the stream results; host/w1700k/wm.py mirrors it.
 *
 * Shared by C and assembly (U32 from layout.h); C checks the addresses against the hw/ headers.
 */
#ifndef WM_WM_H
#define WM_WM_H

#include "../stream/layout.h"

#define WM_ENTRY	U32(0x00916400)		/* ULM1, the vendor WM's region-0 address */
#define WM_LIMIT	U32(0x00938000)		/* end of ULM1 */
/* The radio code, its tables, data and BSS: CRAM where the vendor test-mode WM's regions start,
 * above the ROM's download buffers (mcu_map.h HAZARD). */
#define WM_CRAM_BASE	U32(0xe0040000)
#define WM_CRAM_LIMIT	U32(0xe027c000)

/* Stack: ULM2 above the ROM's own stack (0x0220d400..0x0220e000) and below its data
 * (0x0222b434); the vendor WM loads its data here, the ROM does not use it. */
#define WM_STACK_TOP	U32(0x0222a000)

#define WM_STATUS	U32(0x0090f300)
#define WM_MAGIC_AT	0x00			/* WM_MAGIC once the main loop runs */
#define WM_VERSION_AT	0x04
#define WM_BEATS_AT	0x08			/* main-loop passes / 1024 */
#define WM_STATE_AT	0x0c			/* WM_STATE_* */
/* The core as the ROM handed it over at FW_START. */
#define WM_ENTRY_SP	0x10
#define WM_ENTRY_RA	0x14
#define WM_ENTRY_GP	0x18
#define WM_ENTRY_MSTATUS 0x1c
#define WM_ENTRY_MTVEC	0x20
#define WM_ENTRY_MIE	0x24
#define WM_ENTRY_A0	0x28
#define WM_ENTRY_A1	0x2c
/* The last trap: a load or store fault is skipped (the access reads as nothing), anything else
 * parks the core in WM_STATE_TRAPPED. */
#define WM_TRAP_CAUSE	0x30
#define WM_TRAP_EPC	0x34
#define WM_TRAP_TVAL	0x38
#define WM_TRAPS	0x3c
/* The host writes WM_ARG0..3, then an address to WM_CALL; the main loop clears it, calls that
 * address with those arguments, the ROM's gp and interrupts off (the stream producer is entered
 * this way, and ROM routines are called so), stores the return value and counts the call. */
#define WM_CALL		0x40
#define WM_CALLS	0x44
#define WM_ARG(n)	(0x48 + 4 * (n))
#define WM_RET		0x58
/* The WA is started on request, once the radio is configured: started before, it leaves the radio
 * without antenna signal, and no later configuration brings it back. The host writes
 * WM_WA_START_REQ; the main loop releases the WA and answers WM_WA_STARTED. */
#define WM_WA_START	0x5c
/* Radio commands (radio/radio_cmd.h): the WM publishes WM_RADIO_EE, the address of the EEPROM
 * buffer the host fills before a bring-up. The host writes WM_RADIO_OP and WM_RADIO_ARG(0..3),
 * then a new WM_RADIO_REQ; the main loop runs the op and writes the result, the failure record
 * (hal.h) and the time taken, then WM_RADIO_ACK = REQ. */
#define WM_RADIO_EE	0x60
#define WM_RADIO_REQ	0x64
#define WM_RADIO_ACK	0x68
#define WM_RADIO_OP	0x6c
#define WM_RADIO_ARG(n)	(0x70 + 4 * (n))	/* n 0..3 */
#define WM_RADIO_RES	0x80
#define WM_RADIO_FAIL_CODE 0x84
#define WM_RADIO_FAIL_ARG 0x88
#define WM_RADIO_FAILS	0x8c
#define WM_RADIO_US	0x90
#define WM_RADIO_STATE	0x94			/* RADIO_STATE_* bits: what is up */
/* The address of the RF command builder (radio/rfctl.h rfctl_build), written at start: the control
 * interpreter (stream/ctl.h) calls it for an RF op, in our main loop and in the separately linked
 * stream producer alike. */
#define WM_RF_BUILD	0x98
/* The guard (stream/ctl.h): WM_UNLOCK holds WM_UNLOCK_MAGIC while a host has lifted it, and is
 * cleared at every start; WM_DENIED counts the calls refused (WM_CALL to an address that is not
 * in our own image, on a locked WM). */
#define WM_UNLOCK	0x9c
#define WM_DENIED	0xa0
#define WM_STATUS_WORDS	41

#define WM_MAGIC	U32(0x4d57574f)		/* 'OWWM' */
#define WM_VERSION	U32(7)
#define WM_UNLOCK_MAGIC	U32(0x4b4c4e55)		/* 'UNLK' */
#define WM_WA_START_REQ	U32(1)
#define WM_WA_STARTED	U32(2)
#define WM_STATE_RUNNING	U32(1)
#define WM_STATE_TRAPPED	U32(2)

#ifndef __ASSEMBLER__
#include "hw/mt7996/mcu_map.h"
#include "hw/mt7996/probe_page.h"

_Static_assert(WM_ENTRY == MT7996_WM_FW_R0_BASE && WM_LIMIT == MT7996_WM_ULM1_BASE + MT7996_WM_ULM1_SIZE,
	       "wm.h and mcu_map.h disagree");
_Static_assert(WM_CRAM_BASE == MT7996_WM_FW_CRAM_FREE_BASE &&
	       WM_CRAM_LIMIT == MT7996_MCU_CRAM1_BASE + MT7996_MCU_CRAM1_SIZE, "wm.h and mcu_map.h disagree");
_Static_assert(WM_STATUS == MT7996_PROBE_OWN_WM_STATUS && WM_MAGIC == MT7996_PROBE_OWN_WM_MAGIC &&
	       WM_STATUS + WM_CALL == MT7996_PROBE_OWN_WM_CALL &&
	       WM_STATUS + WM_ARG(0) == MT7996_PROBE_OWN_WM_ARG(0) &&
	       WM_STATUS + WM_RET == MT7996_PROBE_OWN_WM_RET &&
	       WM_STATUS + WM_WA_START == MT7996_PROBE_OWN_WM_WA_START &&
	       WM_STATUS + WM_RADIO_EE == MT7996_PROBE_OWN_WM_RADIO_EE &&
	       WM_STATUS + WM_RADIO_REQ == MT7996_PROBE_OWN_WM_RADIO_REQ &&
	       WM_STATUS + WM_RADIO_STATE == MT7996_PROBE_OWN_WM_RADIO_STATE &&
	       WM_STATUS + WM_RF_BUILD == MT7996_PROBE_OWN_WM_RF_BUILD &&
	       WM_STATUS + WM_UNLOCK == MT7996_PROBE_OWN_WM_UNLOCK &&
	       WM_UNLOCK_MAGIC == MT7996_PROBE_OWN_WM_UNLOCK_MAGIC &&
	       WM_STATUS + WM_DENIED == MT7996_PROBE_OWN_WM_DENIED &&
	       WM_STATUS + 4 * WM_STATUS_WORDS <= MT7996_PROBE_OWN_WA_STATUS, "wm.h and probe_page.h disagree");
#endif

#endif
