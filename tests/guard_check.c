/* SPDX-License-Identifier: MIT */
/* guard_check OP A B C [OP A B C ...]: for each raw op of a host's program, whether the guard
 * keeps it from a locked WM (firmware/stream/guard.h, as it is): a line "denied" or "run".
 * OP is the op's name (W32, RMW32, R32, POLL, AW, ARMW, AR, APOLL, SAR, DELAY); A, B, C its
 * words in hex. Built for the host by tests/test_firmware_guard.py.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "guard.h"

int main(int argc, char **argv)
{
	static const struct { const char *name; uint32_t code; } ops[] = {
		{ "W32", MT7996_PROBE_CTL_OP_W32 }, { "RMW32", MT7996_PROBE_CTL_OP_RMW32 },
		{ "R32", MT7996_PROBE_CTL_OP_R32 }, { "POLL", MT7996_PROBE_CTL_OP_POLL },
		{ "AW", MT7996_PROBE_CTL_OP_AW }, { "ARMW", MT7996_PROBE_CTL_OP_ARMW },
		{ "AR", MT7996_PROBE_CTL_OP_AR }, { "APOLL", MT7996_PROBE_CTL_OP_APOLL },
		{ "SAR", MT7996_PROBE_CTL_OP_SAR }, { "DELAY", MT7996_PROBE_CTL_OP_DELAY },
	};

	if (argc < 5 || (argc - 1) % 4)
		return 2;
	for (int i = 1; i + 3 < argc; i += 4) {
		unsigned int k;

		for (k = 0; k < sizeof(ops) / sizeof(ops[0]) && strcmp(argv[i], ops[k].name); k++)
			;
		if (k == sizeof(ops) / sizeof(ops[0]))
			return 2;
		/* the op word as a program holds it: the code, with flags and an argument above it */
		puts(ctl_denied(ops[k].code | 0x12345u << 12, (uint32_t)strtoul(argv[i + 1], NULL, 16),
				(uint32_t)strtoul(argv[i + 2], NULL, 16), (uint32_t)strtoul(argv[i + 3], NULL, 16))
		     ? "denied" : "run");
	}
	return 0;
}
