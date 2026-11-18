/* SPDX-License-Identifier: GPL-2.0-only */
/* Table encoding and publication for the stages that may write NAND. The
 * dumper never links this. */
#ifndef AIROHA_BMT_WRITE_H
#define AIROHA_BMT_WRITE_H

#include "airoha_bmt.h"

/* Pages, blocks and free OOB come from the map (airoha_bmt_set_pages): 2048 +
 * 56 per page and 128 KiB blocks on the W25N04KV, 4096 + 112 and 256 KiB on
 * the W25N04LV. Page buffers are AIROHA_BMT_PAGE_MAX bytes. */
#define AIROHA_INTENT_BYTES 240u
#define AIROHA_RETIRE_ATTEMPTS 3u

typedef void (*airoha_sha256_fn)(const airoha_u8 *, airoha_u32, airoha_u8 out[32]);

enum airoha_write_operation {
	AIROHA_INTENT_PROGRAM = 1,
	AIROHA_INTENT_ERASE = 2,
};

/* Immutable page-zero record in a separate, manager-owned reserve block.
 * Hashes bind the canonical pre-operation BMT and factory BBT, and the complete
 * intended data image (ECC-corrected main plus AUTO OOB, with the two vendor
 * backreference bytes normalized). No generation or stock selection extension.
 * Pages 1..3 are append-only retirement-attempt tickets. Program a ticket and
 * verify it BEFORE attempting the old table's RAW worn marker, so reset cannot
 * silently exceed the NAND's four-program limit (one table + at most 3 marks).
 */
struct airoha_bmt_intent {
	airoha_u16 operation;
	airoha_u16 journal_block;
	airoha_u16 candidate_block;
	airoha_u16 logical_block;
	airoha_u16 system_key;
	airoha_u16 old_physical;
	airoha_u16 old_table_block;
	airoha_u16 bbt_block;
	airoha_u16 bbt_size;
	airoha_u16 bmt_size;
	airoha_u8 old_mapping_present;
	airoha_u8 bmt_digest[32];
	airoha_u8 bbt_digest[32];
	airoha_u8 data_digest[32];
	/* Reserve-index bitmap captured after a full erased-state inventory.
	 * It identifies blocks this one serialized transaction may have dirtied
	 * during interrupted metadata retries. Existing data/tables have zero
	 * bits; candidate and journal were free and have one bits. Bits from the
	 * pool size to 255 are zero. It does not authorize ordinary master writes
	 * to the pool. */
	airoha_u8 free_pool[32];
	/* Complete canonical preimage for PROGRAM. Allows safe abort when reset
	 * interrupts the stage before original I/O; ERASE does not need old data. */
	airoha_u8 preimage_digest[32];
};

/* These validate writable ownership, which is stricter than stock parsing.
 * Public writes are restricted by bindings to [first,end); they never expose
 * the physical reserve or vendor/loader windows as writable master aliases. */
int airoha_bmt_write_validate(const struct airoha_bmt *t,
			    airoha_u32 first, airoha_u32 end,
			    airoha_u32 bbt_block, airoha_u32 bmt_block);

/* Canonical vendor-compatible bytes: version 1, FF reserved header, zero unused
 * entries as in the matched vendor's initial in-memory table, the BMT checksum
 * over the pool's entries. Caller supplies a full page (t->page_bytes), which
 * remains untouched when validation fails. */
int airoha_bmt_encode(const struct airoha_bmt *t, airoha_u8 *page, airoha_u32 len);
int airoha_bbt_encode(const struct airoha_bmt *t, airoha_u8 *page, airoha_u32 len);

/* Update the original SYSTEM key, including when its old replacement failed.
 * An old reserve destination gains an ignored self-entry excluding it from
 * both allocators; this is not a reader chain. The candidate must already
 * have been allocated and independently verified by the I/O engine. */
int airoha_bmt_replace(struct airoha_bmt *t, airoha_u32 logical,
		       airoha_u32 candidate);

/* The byte contract is explicit, never memcpy(struct) or native-endian fields.
 * The record carries the block, page, free-OOB and chip geometry of `t`, the
 * map it was written against; decode validates hash, that geometry, ranges,
 * distinct metadata/data storage and record padding before publishing any
 * output fields. */
int airoha_bmt_intent_encode(const struct airoha_bmt_intent *intent,
		const struct airoha_bmt *t, airoha_u8 *page, airoha_u32 len,
		airoha_sha256_fn hash);
int airoha_bmt_intent_decode(struct airoha_bmt_intent *intent,
		const struct airoha_bmt *t, const airoha_u8 *page, airoha_u32 len,
		airoha_sha256_fn hash);

/* Check a recovered journal against the loaded tables. Accept only the exact
 * old mapping or the exact one-entry committed replacement derived from it.
 * scratch is a caller-owned map; page is a full-page workspace. */
int airoha_bmt_intent_match(const struct airoha_bmt_intent *intent,
		const struct airoha_bmt *t, struct airoha_bmt *scratch,
		airoha_u8 *page, airoha_sha256_fn hash);

/* Synchronous, unwrapped physical callbacks. No recursive logical translation.
 * The binding holds its outer serialization guard over this entire sequence.
 * read_page returns corrected main bytes (nonnegative bitflip count is okay).
 * program_page regenerates ECC and leaves free OOB FF. It writes one FULL page.
 * erased returns 1 only for a full RAW main+OOB FF page, 0 for used, or -errno.
 * retire programs ONLY first-page RAW OOB byte 1, without erasing or ECC writes.
 * verify_image reads/hashes the complete staged logical image, including valid
 * free OOB, and returns 0 only if it equals the record's intended-image digest.
 */
struct airoha_bmt_flash {
	void *ctx;
	int (*read_page)(void *, airoha_u32 block, airoha_u32 page, airoha_u8 *main);
	int (*program_page)(void *, airoha_u32 block, airoha_u32 page, const airoha_u8 *main);
	int (*erase)(void *, airoha_u32 block);
	int (*erased)(void *, airoha_u32 block, airoha_u32 page);
	int (*markers)(void *, airoha_u32 block, airoha_u8 out[2]);
	int (*retire)(void *, airoha_u32 block);
	int (*verify_image)(void *, const struct airoha_bmt_intent *);
	airoha_sha256_fn hash;
};

/* The tickets the record in this journal has used (0..AIROHA_RETIRE_ATTEMPTS), or an error. A
 * used ticket means a retirement of the old table was at least begun. */
int airoha_bmt_tickets_used(const struct airoha_bmt_flash *io, airoha_u32 journal_block);

/* Publish/recover one already-durable intent. Checks the journal and image
 * again before any metadata I/O. Uses only initially-free blocks named by that
 * immutable journal. Metadata retry candidates are erased/verified/retired
 * before descending past them. Never erase the old BMT or modify the BBT.
 * Returns success only after a fresh vendor-order scan selects the new mapping.
 * Buffers are caller-owned full pages; scratch is separate from t. On any
 * failure retain journal/data, stop further writes and re-inventory at recovery.
 */
int airoha_bmt_publish(struct airoha_bmt *t, struct airoha_bmt *scratch,
		const struct airoha_bmt_intent *r, const struct airoha_bmt_flash *io,
		airoha_u32 first, airoha_u32 end, airoha_u8 *expected, airoha_u8 *actual,
		airoha_u32 *selected_table);

#endif
