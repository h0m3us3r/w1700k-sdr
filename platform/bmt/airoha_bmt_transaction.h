/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef AIROHA_BMT_TRANSACTION_H
#define AIROHA_BMT_TRANSACTION_H

#include "airoha_bmt_write.h"

/* One block's logical image: each page's main bytes followed by its free OOB,
 * 64 times. Its size follows the map's page geometry. */
#define AIROHA_IMAGE_STRIDE(t) ((airoha_u32)(t)->page_bytes + (t)->oob_bytes)
#define AIROHA_IMAGE_BYTES(t) (AIROHA_BMT_BLOCK_PAGES * AIROHA_IMAGE_STRIDE(t))
#define AIROHA_TRANSACTION_WORK_BYTES(t) (2u * AIROHA_IMAGE_BYTES(t))

enum airoha_pool_role {
	AIROHA_POOL_FREE, AIROHA_POOL_BAD, AIROHA_POOL_TABLE,
	AIROHA_POOL_LIVE, AIROHA_POOL_UNKNOWN, AIROHA_POOL_JOURNAL,
	AIROHA_POOL_STAGE,
};

/* The normal page callbacks transfer the physical chip's AUTO OOB bytes.
 * Only the two vendor source-backreference bytes are normalized by the engine.
 * The count outputs on program_data preserve physical partial progress even
 * when the callback fails. The binding must check full reads before returning.
 */
struct airoha_bmt_data_io {
	struct airoha_bmt_flash metadata;
	int (*read_data)(void *, airoha_u32 block, airoha_u32 page,
			 airoha_u8 *main, airoha_u8 *oob);
	int (*program_data)(void *, airoha_u32 block, airoha_u32 page,
			 const airoha_u8 *main, const airoha_u8 *oob,
			 airoha_u32 *written, airoha_u32 *oobwritten);
};

/* One eraseblock's caller changes. OOB is a linear AUTO view of this block,
 * including first-page offsets; the reserved backreference bytes cannot be
 * written through this interface. Bindings reject/convert RAW/PLACE explicitly.
 */
struct airoha_bmt_change {
	airoha_u32 logical;
	airoha_u32 offset, length;
	const airoha_u8 *data;
	airoha_u32 oob_offset, oob_length;
	const airoha_u8 *oob;
	int erase;
};
struct airoha_bmt_progress {
	airoha_u32 data, oob;
};

/* One fixed journal location is a per-unit/profile input, separate from logical
 * partition offsets. Its block is never free pool data. A journal that is no
 * record is erased at the start, unless a publication is under way; a whole
 * record that is not ours to read, and a table found there, are fatal to the
 * writer (airoha_bmt_transaction.c, load_journal).
 * The caller holds one lock over reads and complete transactions/recovery.
 * image/work are ordinary buffers, not DMA allocations; callbacks are sync.
 */
struct airoha_bmt_transaction {
	struct airoha_bmt *map;
	struct airoha_bmt scratch;
	struct airoha_bmt_intent pending;
	struct airoha_bmt_data_io io;
	struct airoha_bmt_flash publisher;
	airoha_u32 first, end, bbt_block, bmt_block, journal_block, cursor;
	airoha_u8 pool[AIROHA_BMT_MAX_ENTRIES];	/* by reserve index, block - system */
	airoha_u8 *image, *verify;
	airoha_u8 expected[AIROHA_BMT_PAGE_MAX], actual[AIROHA_BMT_PAGE_MAX];
	int ready, busy, failed, has_pending;
	airoha_u32 remaps, recoveries, aborted, corrected_reads;
};

/* Verify inventory and recover before registering writable MTD or attaching
 * UBI. No callback is published by the binding on failure. Caller owns storage
 * and the two-image work allocation (AIROHA_TRANSACTION_WORK_BYTES(map)); NULL
 * work is an allocation failure. The map carries the page geometry. */
int airoha_bmt_transaction_init(struct airoha_bmt_transaction *w,
		struct airoha_bmt *map, const struct airoha_bmt_data_io *io,
		airoha_u32 first, airoha_u32 end, airoha_u32 bbt_block,
		airoha_u32 bmt_block, airoha_u32 journal_block, airoha_u8 *work);
int airoha_bmt_transaction_apply(struct airoha_bmt_transaction *w,
		const struct airoha_bmt_change *change, struct airoha_bmt_progress *done);
int airoha_bmt_transaction_recover(struct airoha_bmt_transaction *w);

#endif
