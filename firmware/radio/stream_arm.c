// SPDX-License-Identifier: MIT
/* The capture route and ICAP0 around a stream (hw/mt7996/cap_route.h "Route configurations",
 * hw/mt7996/icap.h): before it, ICAP0 routed into the UMAC pool and configured stopped over the
 * capture ring, which the stream producer then starts; after it, ICAP0 stopped and the route
 * released. ICAPCR8 (the band) and the band's capture mux stay as CAPTURE / SELECT set them.
 */
#include "hw/mt7996/cap_route.h"
#include "hw/mt7996/icap.h"
#include "hw/mt7996/iq_format.h"

#include "hal.h"
#include "stream_arm.h"

/* The pool route's words in address order: the firmware's type-0 values (without +0x00 and
 * +0x04 [23:0], which hang the MCU when driven: cap_route.h HAZARD), every other word 0. */
static const uint32_t pool_route[][2] = {
	{ MT7996_CAP_ROUTE_GRANT_A, 0 },
	{ MT7996_CAP_ROUTE_GRANT_B, MT7996_CAP_ROUTE_GRANT_B_ALL },
	{ MT7996_CAP_ROUTE_GRANT_C, 0 },
	{ MT7996_CAP_ROUTE_OWNER, MT7996_CAP_ROUTE_OWNER_TYPE0 },
	{ MT7996_CAP_ROUTE_10, 0 },
	{ MT7996_CAP_ROUTE_14, 0 },
	{ MT7996_CAP_ROUTE_18, MT7996_CAP_ROUTE_18_TYPE0 },
	{ MT7996_CAP_ROUTE_1C, MT7996_CAP_ROUTE_1C_TYPE0 },
	{ MT7996_CAP_ROUTE_20, MT7996_CAP_ROUTE_20_TYPE0 },
	{ MT7996_CAP_ROUTE_WINDOW, MT7996_CAP_ROUTE_WINDOW_UPPER },
	{ MT7996_CAP_ROUTE_WPATH, MT7996_CAP_ROUTE_WPATH_TYPE0 },
	{ MT7996_CAP_ROUTE_WPATH2, MT7996_CAP_ROUTE_WPATH2_TYPE0 },
};

void stream_arm(int two_paths)
{
	for (uint32_t i = 0; i < sizeof(pool_route) / sizeof(pool_route[0]); i++)
		hal_wr(pool_route[i][0], pool_route[i][1]);
	hal_wr(MT7996_ICAP_CR0(0), MT7996_ICAP_CR0_IDLE);
	hal_wr(MT7996_ICAP_CR2(0), MT7996_ICAP_ADDR_OF_BYTES(0));
	hal_wr(MT7996_ICAP_CR3(0), MT7996_ICAP_ADDR_END_OF(MT7996_IQ_POOL_MAX));
	/* The 64 -> 128 packer makes a beat four sample times of path 0; without it a beat is two
	 * sample times of path 0 and the same two of path 1 (iq_format.h). */
	hal_wr(MT7996_ICAP_CR10(0), two_paths ? 0 : MT7996_ICAP_CR10_64B_128B_EN);
}

void stream_disarm(void)
{
	hal_wr(MT7996_ICAP_CR0(0), MT7996_ICAP_CR0_IDLE);
	for (uint32_t i = 0; i < sizeof(pool_route) / sizeof(pool_route[0]); i++)
		hal_wr(pool_route[i][0], 0);
	hal_wr(MT7996_CAP_ROUTE_WINDOW, MT7996_CAP_ROUTE_WINDOW_UPPER);
	hal_wr(MT7996_ICAP_CR2(0), MT7996_ICAP_WINDOW_START);
	hal_wr(MT7996_ICAP_CR3(0), MT7996_ICAP_WINDOW_END);
	hal_wr(MT7996_ICAP_CR10(0), 0);
}
