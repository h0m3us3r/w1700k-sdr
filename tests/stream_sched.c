// SPDX-License-Identifier: MIT
/* stream.h's schedule helpers on the host (tests/test_firmware_stream.py): for every packing
 * schedule and the chunks of four periods, one line "schedule chunk role index address": the
 * chunk's role (2 the WA packs it, 1 the WM, 0 raw), its index among its packer's chunks, and
 * where TX0 reads it from (the packer's buffer, or the capture ring).
 */
#include <stdio.h>

#include "stream.h"

int main(void)
{
	for (unsigned int p = 0; p < SCHEDULE_COUNT; p++) {
		const struct sched *s = &schedules[p];

		for (uint32_t k = 0; k < 4 * PERIOD; k++) {
			int r = role(s, k);
			uint32_t index = r == 2 ? packer_index(s, k, s->wa_n) : r == 1 ? packer_index(s, k, s->wm_n) : 0;
			uint32_t address = r == 2 ? wa_buffer(s, k) : r == 1 ? wm_buffer(s, k) : ring_at(k);

			printf("%u %u %d %u %#x\n", p, (unsigned int)k, r, (unsigned int)index, (unsigned int)address);
		}
		/* The first chunk of each packer, as the producer and the packer find it. */
		printf("first %u %u %u\n", p, s->wa_mask ? (unsigned int)next_of(~0u, s->wa_mask) : 0u,
		       s->wm_mask ? (unsigned int)next_of(~0u, s->wm_mask) : 0u);
	}
	return 0;
}
