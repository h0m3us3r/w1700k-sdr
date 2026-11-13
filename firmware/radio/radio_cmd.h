/* SPDX-License-Identifier: MIT */
/* Radio commands to our WM (the WM_RADIO_* block of wm/wm.h; host/w1700k/wm.py mirrors them).
 *
 *   SELFTEST                       -> RADIO_SELFTEST_MAGIC: the radio code runs (from CRAM)
 *   IDENTIFY                       -> the A-die chip IDs read (ARG words get them: see radio.c)
 *   BRINGUP                        the cold bring-up: the ROM patch's radio init, the common and
 *                                  per-band PHY init, every die's RF init and calibrations;
 *                                  the EEPROM must be in WM_RADIO_EE first
 *   SETUP band lo lo_hi bwf        the band at an LO and a bandwidth (chanset.h chanset): everything
 *                                  the receive-only channel set writes, from the LO (Hz, [31:0] and
 *                                  [63:32]) and bwf = bandwidth [7:0] (0 20, 1 40, 2 80, 3 160,
 *                                  4 320 MHz: the sample rate) | chanset.h CHANSET_F_* [15:8] |
 *                                  RADIO_CHAN_NO_DCOC; the synthesizer by TUNE's program; then the
 *                                  RX DC-offset calibration. No channel numbers; the analog filter
 *                                  is not part of it (RF command FILTER)
 *   RATE band bw                   the band's bandwidth alone (chanset.h chanset_rate) at its
 *                                  current LO: no synthesizer, no DCOC; LO and filter stay. CAPTURE
 *                                  again afterwards
 *   DCOC band                      the RX DC-offset calibration at the band's current LO, filter
 *                                  and gain (after a SETUP)
 *   CAPTURE band bw                the band's capture path as MediaTek's capture prime leaves it
 *                                  (after its channel set; bw 0..4 as CHANSET)
 *   SELECT band                    ICAP listens to the band (no stream on another band running)
 *   STREAM packing                 a stream with a packing schedule (RADIO_PACKING_*): the WA
 *                                  started if it packs, the route and ICAP0 armed, then the
 *                                  producer until the host's stop word; ACK when it has ended
 *                                  (RES = the producer's status, the result page as before), the
 *                                  route released
 *   STEP step arg                  one bring-up step alone, for development (RADIO_STEP_*)
 */
#ifndef RADIO_RADIO_CMD_H
#define RADIO_RADIO_CMD_H

#define RADIO_OP_SELFTEST	1u
#define RADIO_OP_IDENTIFY	2u
#define RADIO_OP_BRINGUP	3u
#define RADIO_OP_SETUP		4u
#define RADIO_OP_STEP		5u
#define RADIO_OP_CAPTURE	6u
#define RADIO_OP_SELECT		7u
#define RADIO_OP_STREAM		8u
#define RADIO_OP_RATE		9u
#define RADIO_OP_DCOC		10u

#define RADIO_SELFTEST_MAGIC	0x52414449u	/* 'RADI' */

/* Packing schedules (stream/blob.py PACKINGS, in this order): which chunks go out packed to
 * 28 bits per sample pair (lossless, 8 KiB -> 7 KiB). */
#define RADIO_PACKING_RAW	0u	/* none: up to 160 Msps on one 10 GbE link */
#define RADIO_PACKING_HALF	1u	/* every other chunk (WM 4 + WA 4 of 16): 320 Msps on 10 GbE */
#define RADIO_PACKING_FULL	2u	/* every chunk (WM 8 + WA 8 of 16): up to 160 Msps, 7/8 of the bytes */
#define RADIO_PACKINGS		3u
#define RADIO_STREAM_NO_WA	0x100u		/* STREAM result bit: the WA did not start */

#define RADIO_CHAN_BW		0x000000ffu	/* SETUP arg 3 fields */
#define RADIO_CHAN_FLAGS	0x0000ff00u
#define RADIO_CHAN_NO_DCOC	0x00010000u

/* Results */
#define RADIO_OK		0u
#define RADIO_ERR_OP		1u	/* unknown op */
#define RADIO_ERR_ARG		2u	/* an argument out of range */
#define RADIO_ERR_ORDER		3u	/* a prerequisite has not run (RADIO_STATE_*) */
#define RADIO_ERR_HW		4u	/* a hardware access failed: see the failure record */

/* Development steps (RADIO_OP_STEP arg 0) */
#define RADIO_STEP_PATCH_INIT	1u	/* the ROM patch's radio init */
#define RADIO_STEP_COMMON	2u	/* the common PHY init (before the bands) */
#define RADIO_STEP_BAND		3u	/* arg 1 = band: the per-band PHY init */
#define RADIO_STEP_RF_INIT	4u	/* arg 1 = die: RF init, R-cal, RC calibration */
#define RADIO_STEP_DCOC		5u	/* arg 1 = band: RX DC-offset calibration at its LO */
#define RADIO_STEP_POST		6u	/* the common init's tail (after the dies) */
#define RADIO_STEP_POOL		7u	/* the UMAC pool layout (the capture partition) */

/* WM_RADIO_STATE bits */
#define RADIO_STATE_IDENTIFIED	(1u << 0)
#define RADIO_STATE_PATCH_INIT	(1u << 1)
#define RADIO_STATE_COMMON	(1u << 2)
#define RADIO_STATE_BAND(b)	(1u << (4 + (b)))	/* per-band PHY init done */
#define RADIO_STATE_RF(d)	(1u << (8 + (d)))	/* die d RF init and calibrations done */
#define RADIO_STATE_DCOC(d)	(1u << (12 + (d)))
#define RADIO_STATE_POST	(1u << 15)
#define RADIO_STATE_POOL	(1u << 3)
#define RADIO_STATE_UP		(1u << 16)	/* the whole bring-up */
#define RADIO_STATE_CHAN(b)	(1u << (20 + (b)))	/* band b is set up (SETUP) */
#define RADIO_STATE_CAPTURE(b)	(1u << (24 + (b)))	/* band b's capture path is set up */

#endif
