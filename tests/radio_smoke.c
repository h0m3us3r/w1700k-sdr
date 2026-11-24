// SPDX-License-Identifier: MIT
/* The radio code of our WM on the host (tests/test_firmware_radio.py): every module of
 * firmware/radio/ but hal_wm.c, linked with the host mock of hal.h (firmware/radio/tests/mock.c)
 * running free, and driven the way our WM runs a load.
 *
 *   radio_smoke EEPROM steps     the bring-up step by step in radio.c's order (RADIO_OP_STEP:
 *                                patch init, pool, common, band 0..2, RF init 0..2, post), then per
 *                                band SETUP without its DCOC, the DCOC step and CAPTURE, then band
 *                                0's RATE to 40 MHz and back, each with CAPTURE, a DCOC, then
 *                                SELECT band 1
 *   radio_smoke EEPROM commands  the same as the host sends it: BRINGUP, per band SETUP (with its
 *                                DCOC) and CAPTURE, the same RATEs and DCOC, then SELECT band 1
 *   radio_smoke rfctl A B C ...  the RF command builder's program of each RF op {A, B, C}, with die
 *                                0's RC code set as a bring-up leaves it (RFCTL_RC_CODE)
 *   radio_smoke rfctl-rc=N ...   the same on a die whose RC code is N
 *   radio_smoke rfctl-uncal ...  the same before any bring-up (no RC code)
 *
 * Each band at its default (host/w1700k/radio.py DEFAULT_BANDS): band 0 at an LO of 2442 MHz and
 * 160 MHz, band 1 at 5250 MHz and 160 MHz, band 2 at 6105 MHz and 320 MHz.
 *
 * Output of the radio modes: stdout is the mock's access log, with "# step NAME ARG" before each
 * command; stderr one line per command,
 *     result NAME ARG res R fail CODE ARG COUNT state S
 * then the mock's "OK N accesses". The rfctl mode prints one JSON line per op:
 *     {"res": R, "count": N, "out": O, "check": C, "lo": L, "lo_hi": H, "ops": [[op, a, b, c], ...]}
 *
 * The free-running mock answers a read with the register's last written value, 0 if never
 * written, unless preset. The presets stand in for what the hardware reports: the A-die chip IDs
 * of this board, unit A's D-die revision and strap, the XO ready, and the done flags of the
 * synthesizer and DCOC engines (their polls would otherwise time out: nothing writes them).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hw/adie/adie.h"
#include "hw/adie/sx.h"
#include "hw/mt7996/conn.h"

#include "hw/mt7996/phy_chan.h"

#include "radio.h"
#include "radio_cmd.h"
#include "rfctl.h"
#include "tests/mock.h"

uint32_t radio_command(uint32_t op, const uint32_t *arg, uint32_t *state);

static const struct {
	uint64_t lo_hz;
	uint32_t bw;
} stock[RADIO_BANDS] = { { 2442000000ull, 3 }, { 5250000000ull, 3 }, { 6105000000ull, 4 } };

#define RFCTL_RC_CODE	17u	/* die 0's RC-calibration code on unit A */

static uint32_t state;

static void presets(void)
{
	static const uint32_t chip_id[RADIO_DIES] = { ADIE_TOP_CHIP_ID_MT7976, ADIE_TOP_CHIP_ID_MT7977,
						      ADIE_TOP_CHIP_ID_MT7977 };

	for (uint32_t die = 0; die < RADIO_DIES; die++) {
		mock_apreset(die, ADIE_K_TOP, ADIE_TOP_CHIP_ID, chip_id[die]);
		for (uint32_t k = 0; k < ADIE_NUM_K_WF; k++) {
			mock_apreset(die, k, ADIE_SX_DONE, ADIE_SX_DONE_FLAG);
			mock_apreset(die, k, ADIE_DCOC_DONE, ADIE_DCOC_DONE_FLAG);
		}
	}
	mock_preset(MT7996_CBTOP_HW_REV, CBTOP_HW_REV_E2);
	mock_preset(MT7996_CBTOP_PAD_GPIO, HW_PREP(CBTOP_PAD_GPIO_ADIE_COMB, CBTOP_PAD_GPIO_ADIE_COMB_677));
	mock_preset(MT7996_CBTOP_XO_STA, HW_PREP(CBTOP_XO_STA_READY, CBTOP_XO_STA_READY_VAL));
	/* As reset leaves it: transmit and receive both disabled. A mock that starts the word at 0
	 * shows a read-modify-write that keeps "transmit enabled" as readily as one that keeps it
	 * disabled. RADIO_SMOKE_ARB_SCR gives the word another start, for the test that something
	 * else had enabled transmit. */
	const char *start = getenv("RADIO_SMOKE_ARB_SCR");

	for (uint32_t band = 0; band < RADIO_BANDS; band++)
		mock_preset(MT7996_LMAC_ARB_SCR(band), start ? (uint32_t)strtoul(start, NULL, 0) : MT7996_LMAC_ARB_SCR_CAL);
}

static void command(const char *name, uint32_t op, uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3)
{
	const uint32_t arg[4] = { a0, a1, a2, a3 };
	uint32_t res, shown = op == RADIO_OP_STEP ? a1 : a0;

	printf("# step %s %u\n", name, shown);
	res = radio_command(op, arg, &state);
	fprintf(stderr, "result %s %u res %u fail %u %#x %u state %#010x\n", name, shown, res, hal_fail_rec.code,
		hal_fail_rec.arg, hal_fail_rec.count, state);
}

static void step(const char *name, uint32_t step, uint32_t arg)
{
	command(name, RADIO_OP_STEP, step, arg, 0, 0);
}

static int radio(const char *eeprom, int steps)
{
	uint32_t b;

	mock_open("-");
	mock_eeprom(eeprom);
	presets();
	if (steps) {
		step("patch_init", RADIO_STEP_PATCH_INIT, 0);
		step("pool", RADIO_STEP_POOL, 0);
		step("common", RADIO_STEP_COMMON, 0);
		for (b = 0; b < RADIO_BANDS; b++)
			step("band", RADIO_STEP_BAND, b);
		for (b = 0; b < RADIO_DIES; b++)
			step("rf_init", RADIO_STEP_RF_INIT, b);
		step("post", RADIO_STEP_POST, 0);
		state |= RADIO_STATE_UP;	/* what BRINGUP adds after its steps */
	} else {
		command("bringup", RADIO_OP_BRINGUP, 0, 0, 0, 0);
	}
	for (b = 0; b < RADIO_BANDS; b++) {
		command("setup", RADIO_OP_SETUP, b, (uint32_t)stock[b].lo_hz, (uint32_t)(stock[b].lo_hz >> 32),
			stock[b].bw | (steps ? RADIO_CHAN_NO_DCOC : 0));
		if (steps)
			step("dcoc", RADIO_STEP_DCOC, b);
		command("capture", RADIO_OP_CAPTURE, b, stock[b].bw, 0, 0);
	}
	command("rate", RADIO_OP_RATE, 0, MT7996_CHSET_BW40, 0, 0);
	command("capture", RADIO_OP_CAPTURE, 0, MT7996_CHSET_BW40, 0, 0);
	command("rate", RADIO_OP_RATE, 0, stock[0].bw, 0, 0);
	command("capture", RADIO_OP_CAPTURE, 0, stock[0].bw, 0, 0);
	command("dcoc", RADIO_OP_DCOC, 0, 0, 0, 0);
	command("select", RADIO_OP_SELECT, 1, 0, 0, 0);
	return mock_finish();
}

static int rfctl(int argc, char **argv, int rc_code)
{
	if (rc_code >= 0) {
		radio_cal.rc_code[0] = (uint32_t)rc_code;
		radio_cal.rc_valid = 1u << 0;
	}
	for (int i = 0; i + 2 < argc; i += 3) {
		struct rfctl_prog p = { 0 };
		uint32_t res = rfctl_build(strtoul(argv[i], NULL, 0), strtoul(argv[i + 1], NULL, 0),
					   strtoul(argv[i + 2], NULL, 0), &p);

		printf("{\"res\": %u, \"count\": %u, \"out\": %u, \"check\": %u, \"lo\": %u, \"lo_hi\": %u, \"ops\": [",
		       res, p.count, p.out, p.check, p.lo, p.lo_hi);
		for (uint32_t k = 0; res == MT7996_PROBE_CTL_OK && k < p.count; k++)
			printf("%s[%u, %u, %u, %u]", k ? ", " : "", p.ops[k].op, p.ops[k].a, p.ops[k].b, p.ops[k].c);
		printf("]}\n");
	}
	return 0;
}

int main(int argc, char **argv)
{
	if (argc >= 2 && !strcmp(argv[1], "rfctl"))
		return rfctl(argc - 2, argv + 2, RFCTL_RC_CODE);
	if (argc >= 2 && !strncmp(argv[1], "rfctl-rc=", 9))
		return rfctl(argc - 2, argv + 2, atoi(argv[1] + 9));
	if (argc >= 2 && !strcmp(argv[1], "rfctl-uncal"))
		return rfctl(argc - 2, argv + 2, -1);
	if (argc != 3 || (strcmp(argv[2], "steps") && strcmp(argv[2], "commands"))) {
		fprintf(stderr, "usage: radio_smoke EEPROM steps|commands, or radio_smoke rfctl A B C ...\n");
		return 2;
	}
	return radio(argv[1], !strcmp(argv[2], "steps"));
}
