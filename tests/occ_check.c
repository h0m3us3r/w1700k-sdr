// SPDX-License-Identifier: MIT
/* The occupancy program (firmware/dsp/occ.c) on the host, for tests/test_firmware_occ.py: its
 * core's memory is an array here, and the requests the WM would make come from stdin.
 *
 *   occ_check THRESHOLD EVERY < snapshots > reports
 *
 * stdin: snapshots of OCC_SAMPLES little-endian sample words each, one request a snapshot (the
 * first with OCC_FLAG_RESET; a snapshot whose first word is 0xffffffff is no snapshot but stands
 * for "the next one is asked with OCC_FLAG_RESET"). stdout: every report the program writes, as
 * OCC_REPORT_WORDS words. stderr: one line per request, "request N reports R".
 */
#define OCC_NATIVE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

uint8_t occ_dtcm[0x10000];

#include "occ.c"

int main(int argc, char **argv)
{
	static uint32_t words[OCC_SAMPLES];
	float threshold;
	uint32_t every, req = 100, reset = 1, seen = 0, now;

	if (argc != 3)
		return 2;
	threshold = strtof(argv[1], NULL);
	every = (uint32_t)strtoul(argv[2], NULL, 0);
	while (fread(words, sizeof(words), 1, stdin) == 1) {
		if (words[0] == 0xffffffffu) {
			reset = 1;
			continue;
		}
		memcpy(occ_dtcm + OCC_SAMPLE_AT, words, sizeof(words));
		W(OCC_FLAGS) = reset ? OCC_FLAG_RESET : 0;
		F(OCC_THRESHOLD) = threshold;
		W(OCC_EVERY) = every;
		reset = 0;
		occ_serve(++req);
		if (W(OCC_DONE) != req)
			return 3;
		now = W(OCC_REPORTS);
		fprintf(stderr, "request %u reports %u\n", (unsigned int)(req - 100), (unsigned int)now);
		if (now != seen)
			fwrite(occ_dtcm + OCC_REPORT, 4, OCC_REPORT_WORDS, stdout);
		seen = now;
	}
	return 0;
}
