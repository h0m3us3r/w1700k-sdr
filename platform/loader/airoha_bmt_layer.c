// SPDX-License-Identifier: GPL-2.0-only
/* The vendor BMT translation as a U-Boot MTD layer, with the optional
 * replacement engine. Recovery runs before UBI, operations are serialized, and
 * the whole-chip MTD cannot write outside our partitions. Missing or invalid
 * tables fail the probe; there is no untranslated fallback.
 * A writer that cannot start does not: the device then attaches read-only, with
 * translation alone (attach_read_only). */
#include <stdio.h>
#include <malloc.h>
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/string.h>
#include <linux/types.h>
#include <linux/mtd/mtd.h>
#if defined(CONFIG_AIROHA_BMT) && !defined(AIROHA_BMT_HOST)
#include <command.h>
#endif
#include "airoha_bmt_layer.h"
#include "airoha_bmt.h"

#if defined(CONFIG_AIROHA_BMT_WRITE)
#include <u-boot/sha256.h>
#include "airoha_bmt_transaction.h"
#define BMT_WRITE 1
static int bmt_locked;
static int bmt_enter(void) { if(bmt_locked) return -EBUSY; bmt_locked=1; return 0; }
static void bmt_leave(void) { bmt_locked=0; }
static void bmt_sha256(const u8 *data,u32 len,u8 out[32]) { sha256_csum_wd(data,len,out,65536); }
#define BMT_ALLOC(n) calloc(1,n)
#define BMT_FREE(p) free(p)
#else
#define BMT_WRITE 0
#endif


static struct {
	struct mtd_info *mtd;
	struct airoha_bmt t;
	unsigned int blk_shift;
	u32 bbt_block, bmt_block;
#if BMT_WRITE
	struct airoha_bmt_transaction *writer;
	u8 *work, *io_page;
	int read_only;		/* the writer did not start: translation alone, nothing written */
	u32 flags_before;	/* the MTD's flags, to give back at detach */
	struct {
		u64 erase_attempts, erase_completed;
		u64 program_attempts, program_completed;
		u64 markbad_attempts, markbad_completed;
		u64 readback_attempts, readback_completed;
	} counters;
#endif
	int (*_read_oob)(struct mtd_info *mtd, loff_t from, struct mtd_oob_ops *ops);
	int (*_write_oob)(struct mtd_info *mtd, loff_t to, struct mtd_oob_ops *ops);
	int (*_erase)(struct mtd_info *mtd, struct erase_info *instr);
	int (*_block_isbad)(struct mtd_info *mtd, loff_t ofs);
	int (*_block_markbad)(struct mtd_info *mtd, loff_t ofs);
} layer;

/* user block -> final physical block, or a negative errno. */
static int map_block(u32 block)
{
	int mapped = airoha_bmt_translate(&layer.t, block);

	return mapped < 0 ? -EIO : mapped;
}

/* Read both physical marker bytes directly; byte 1 (worn) counts if `worn`.
 * The generic NAND bool/cache interface can conceal read errors and is not a
 * publication oracle. */
static int raw_marker_bad(struct mtd_info *mtd, loff_t physical, int worn)
{
	u8 markers[2];
	struct mtd_oob_ops op={.mode=MTD_OPS_RAW,.ooblen=2,.oobbuf=markers};
	int rc=layer._read_oob(mtd,physical,&op);
	if(rc<0) return rc;
	if(op.oobretlen!=2) return -EIO;
	return markers[0]!=255 || (worn && markers[1]!=255);
}

/* The vendor pool scan's question: is this block's factory marker (OOB byte
 * 0 of its first page) set? Byte 1 (worn) does not count. */
static int factory_marker_bad(void *ctx, u32 block)
{
	return raw_marker_bad(ctx,(loff_t)block<<layer.blk_shift,0);
}

/* Data and OOB advance independently through the same NAND page iterator.
 * Bound BOTH lengths to this logical eraseblock before translating the next. */
static int bmt_io(struct mtd_info *mtd, loff_t addr,
                     struct mtd_oob_ops *ops, int writing)
{
	struct mtd_oob_ops cur = *ops;
	int status = 0;

	ops->retlen = 0;
	ops->oobretlen = 0;
	while (ops->retlen < ops->len || ops->oobretlen < ops->ooblen) {
		u32 offset = addr & (mtd->erasesize - 1);
		u32 span = mtd->erasesize - offset;
		u32 pages = (span + mtd->writesize - 1) / mtd->writesize;
		u32 per_page = ops->mode == MTD_OPS_AUTO_OOB ?
			mtd->oobavail : mtd->oobsize;
		int mapped = map_block((u32)(addr >> layer.blk_shift));
		loff_t physical;
		int ret;

		if (addr < 0 || (u64)addr >= mtd->size || mapped < 0)
			return -EINVAL;
		cur.len = min_t(size_t, span, ops->len - ops->retlen);
		cur.ooblen = ops->ooblen - ops->oobretlen;
		if (cur.ooblen) {
			if (!per_page || cur.ooboffs >= per_page)
				return -EINVAL;
			cur.ooblen = min_t(size_t, cur.ooblen,
					 (size_t)pages * per_page - cur.ooboffs);
		}
		cur.datbuf = cur.len ? ops->datbuf + ops->retlen : NULL;
		cur.oobbuf = cur.ooblen ? ops->oobbuf + ops->oobretlen : NULL;
		cur.retlen = 0;
		cur.oobretlen = 0;
		physical = ((loff_t)mapped << layer.blk_shift) + offset;
		ret = writing ? layer._write_oob(mtd, physical, &cur) :
				layer._read_oob(mtd, physical, &cur);
		/* Even a failed operation can have delivered/programmed bytes. */
		ops->retlen += cur.retlen;
		ops->oobretlen += cur.oobretlen;
		if (writing && ret)
			return ret;
		if (ret < 0 && !mtd_is_bitflip(ret) && ret != -EBADMSG)
			return ret;
		/* Never return an accepted ECC status with zero progress: MEMREAD
		 * retries those statuses and would otherwise loop indefinitely. */
		if (!cur.retlen && !cur.oobretlen)
			return -EIO;
		if (ret == -EBADMSG || (ret == -EUCLEAN && status != -EBADMSG))
			status = ret;
		else if (ret > status && status >= 0)
			status = ret;
		if (cur.retlen != cur.len || cur.oobretlen != cur.ooblen)
			return ret < 0 ? ret : -EIO;
		/* Any remaining component starts in the next logical block. */
		addr += span;
		cur.ooboffs = 0;
	}
	return status;
}

static int bmt_read_oob(struct mtd_info *mtd, loff_t from, struct mtd_oob_ops *ops)
{
	return bmt_io(mtd, from, ops, 0);
}

static int bmt_write_oob(struct mtd_info *mtd, loff_t to, struct mtd_oob_ops *ops)
{
	return bmt_io(mtd, to, ops, 1);
}

static int bmt_erase(struct mtd_info *mtd, struct erase_info *instr)
{
	u64 start = instr->addr & ~((u64)mtd->erasesize - 1);
	u64 end = instr->addr + instr->len;

	while (start < end) {
		u32 block = (u32)(start >> layer.blk_shift);
		int mapped = map_block(block);
		struct erase_info one = {
			.len = mtd->erasesize,
		};
		int ret;

		if (mapped < 0) {
			instr->fail_addr = start;
			return mapped;
		}
		one.addr = (u64)mapped << layer.blk_shift;
		ret = layer._erase(mtd, &one);
		if (ret) {	/* no remap on erase failure */
			instr->fail_addr = start;
			return ret;
		}
		start += mtd->erasesize;
	}

	return 0;
}

static int bmt_block_isbad(struct mtd_info *mtd, loff_t ofs)
{
	int mapped = map_block((u32)(ofs >> layer.blk_shift));

	if (mapped < 0)
		return mapped;
	return raw_marker_bad(mtd, (loff_t)mapped << layer.blk_shift, 1);
}

static int bmt_block_markbad(struct mtd_info *mtd, loff_t ofs)
{
	/* Marking a block bad is half of a remap and would leave the vendor
	 * tables out of step; replacement goes through the transaction engine. */
	(void)mtd;
	(void)ofs;
	return -EROFS;
}

/* Scan the reserve for one table, trying each block's first page with the
 * driver's own (unwrapped) read. Returns 0 and leaves the parsed table in
 * layer.t on success. `up` searches ascending from system_blocks (the BBT), else
 * descending from the last block (the BMT), matching where the vendor writes. */
static int find_table(struct mtd_info *mtd, u8 *page, int is_bbt)
{
	u32 total = layer.t.total_blocks;
	u32 system = layer.t.system_blocks;
	int i;

	for (i = 0; i < (int)(total - system); i++) {
		u32 block = is_bbt ? (system + i) : (total - 1 - i);
		struct mtd_oob_ops ops = {
			.mode = MTD_OPS_PLACE_OOB,
			.len = mtd->writesize,
			.datbuf = page,
		};
		int ret, parsed;

		/* The unwrapped driver checks physical factory/worn markers. Never
		 * accept a retired but readable table, or an unreadable marker. */
		ret = raw_marker_bad(mtd, (loff_t)block << layer.blk_shift, 1);
		if (ret < 0)
			return ret;
		if (ret)
			continue;
		ret = layer._read_oob(mtd, (loff_t)block << layer.blk_shift, &ops);
		if (ret < 0 && !mtd_is_bitflip(ret)) {
			if (ret == -EBADMSG) continue;
			return ret;
		}
		if (ops.retlen != mtd->writesize)
			continue;
		parsed = is_bbt ?
			airoha_bmt_parse_bbt(&layer.t, page, mtd->writesize) :
			airoha_bmt_parse_bmt(&layer.t, page, mtd->writesize);
		if (parsed == AIROHA_BMT_OK) {
			if (is_bbt) layer.bbt_block=block; else layer.bmt_block=block;
			return 0;
		}
	}
	return -ENOENT;
}

#if BMT_WRITE
#define BMT_ENTER() bmt_enter()
#define BMT_LEAVE() bmt_leave()
#define BMT_SHA256 bmt_sha256
#define BMT_READ bmt_read_oob
#include "airoha_bmt_mtd_write.h"
#endif

#if BMT_WRITE && !defined(AIROHA_BMT_HOST)
static int do_bmtstats(struct cmd_tbl *cmdtp, int flag, int argc,
			char *const argv[])
{
	if (argc != 1 || !layer.writer)
		return CMD_RET_USAGE;
	printf("bmtstats erase=%llu/%llu program=%llu/%llu markbad=%llu/%llu "
	       "readback=%llu/%llu remap_publications=%u recoveries=%u "
	       "aborted=%u corrected_reads=%u\n",
	       layer.counters.erase_completed, layer.counters.erase_attempts,
	       layer.counters.program_completed, layer.counters.program_attempts,
	       layer.counters.markbad_completed, layer.counters.markbad_attempts,
	       layer.counters.readback_completed, layer.counters.readback_attempts,
	       layer.writer->remaps, layer.writer->recoveries,
	       layer.writer->aborted, layer.writer->corrected_reads);
	return CMD_RET_SUCCESS;
}

U_BOOT_CMD(bmtstats, 1, 1, do_bmtstats,
	"show reset-volatile physical NAND/BMT operation counters", "");
#endif


/* Both tables, from the flash: the pool the stock boot computes from the same markers (fixed at
 * 250 on the W25N04KV, scanned on the W25N04LV), then the BBT and the BMT it selects. */
static int load_tables(struct mtd_info *mtd, u32 total)
{
	u8 *page;
	int ret, pool;

	memset(&layer.t, 0, sizeof(layer.t));
	pool = airoha_bmt_vendor_pool(total, factory_marker_bad, mtd);
	if (pool == AIROHA_BMT_E_MARKER)
		return -EIO;
	if (pool < 0 || airoha_bmt_set_geometry(&layer.t, total, (u32)pool))
		return -EINVAL;

	page = malloc(mtd->writesize);
	if (!page)
		return -ENOMEM;
	ret = find_table(mtd, page, 1);
	if (!ret)
		ret = find_table(mtd, page, 0);
	free(page);
	if (ret)
		return ret;
	return layer.t.bbt_ascending ? 0 : -EINVAL;
}

#if BMT_WRITE
/* What a read-only attachment answers to a write. The operations stay in place, and are not
 * cleared: the MTD core registers no device that has no erase operation (add_mtd_device, unless
 * the device is flagged MTD_NO_ERASE), so a device attached read-only with its erase cleared was
 * refused there and the flash had no reader after all. The core calls none of these on a device
 * that is not writeable; a caller that goes round the core gets the same answer. */
static int read_only_write_oob(struct mtd_info *mtd, loff_t to, struct mtd_oob_ops *ops)
{ (void)mtd; (void)to; ops->retlen = ops->oobretlen = 0; return -EROFS; }
static int read_only_erase(struct mtd_info *mtd, struct erase_info *instr)
{ (void)mtd; instr->fail_addr = MTD_FAIL_ADDR_UNKNOWN; return -EROFS; }
static int read_only_markbad(struct mtd_info *mtd, loff_t ofs)
{ (void)mtd; (void)ofs; return -EROFS; }

/* The writer did not start (airoha_bmt_transaction_init): its journal's block is bad, an
 * interrupted replacement could not be finished, or the pool is not the one stock computes.
 * That used to fail the probe, and then the flash had no reader at all: no UBI, no system, and
 * nothing short of the serial console to look at it with. The tables themselves were found and
 * they translate; so the device attaches with translation alone and writes nothing. What needs
 * a write (an update, a saved setting) fails and says so; the system starts.
 *
 * The map is read from the flash again first: a recovery that stopped part-way may have left
 * the one in memory ahead of what the flash holds. Not for a transport that failed or memory
 * that ran out: those are no state of the flash. */
static int attach_read_only(struct mtd_info *mtd, u32 total, int why)
{
	int ret;

	if (why == -ENOMEM || bmt_transport_failed)
		return why;
	ret = load_tables(mtd, total);
	if (ret)
		return ret;
	printf("airoha-bmt: " "the replacement writer did not start (%d): attached READ-ONLY. The flash reads as before; "
	       "nothing can be written to it until this is looked at\n", why);
	return 0;
}
#endif

static int attach_unlocked(struct mtd_info *mtd)
{
	unsigned int shift = 0;
	u32 erasesize = mtd->erasesize;
	u32 total;
	int ret;
#if BMT_WRITE
	int read_only = 0;
#endif

	if (layer.mtd)
		return -ENOSPC;
#if BMT_WRITE
	if (bmt_transport_failed) return -EIO;
#endif
	if (!mtd->_read_oob || !mtd->_block_isbad || !erasesize ||
	    (erasesize & (erasesize - 1)) || !mtd->writesize ||
	    erasesize % mtd->writesize || mtd->writesize < AIROHA_BMT_BYTES ||
	    mtd->size % erasesize || mtd->size / erasesize > 0xffff)
		return -EINVAL;
	while (erasesize >>= 1)
		shift++;
	total = (u32)(mtd->size >> shift);

	memset(&layer, 0, sizeof(layer));
	layer.blk_shift = shift;
	/* Keep the driver's ops reachable for the reserve scan below. */
	layer._read_oob = mtd->_read_oob;
	layer._write_oob = mtd->_write_oob;
	layer._erase = mtd->_erase;
	layer._block_isbad = mtd->_block_isbad;
	layer._block_markbad = mtd->_block_markbad;

	/* Probe must fail before registration; raw fallback changes addresses. */
	ret = load_tables(mtd, total);
	if (ret)
		return ret;

#if BMT_WRITE
	ret=managed_prepare(mtd);
	if(ret) {
		ret=attach_read_only(mtd,total,ret);
		if(ret) return ret;
		read_only=1;
	}
#endif
	layer.mtd = mtd;
	mtd->_read_oob = bmt_read_oob;
	mtd->_write_oob = layer._write_oob ? bmt_write_oob : NULL;
	mtd->_erase = layer._erase ? bmt_erase : NULL;
	mtd->_block_isbad = bmt_block_isbad;
	mtd->_block_markbad = layer._block_markbad ? bmt_block_markbad : NULL;
#if BMT_WRITE
	if(read_only) {
		/* Translation alone, as a build without the writer has it, and nothing
		 * that writes: UBI attaches such a device read-only. */
		layer.read_only=1;
		layer.flags_before=mtd->flags;
		mtd->flags&=~MTD_WRITEABLE;
		mtd->_write_oob=read_only_write_oob;
		mtd->_erase=read_only_erase;
		mtd->_block_markbad=read_only_markbad;
	} else {
		mtd->_read_oob=managed_read;
		mtd->_write_oob=managed_write;
		mtd->_erase=managed_erase;
		mtd->_block_isbad=managed_isbad;
		mtd->_block_markbad=managed_markbad;
	}
#endif
	printf("airoha-bmt: attached (system_blocks=%u, pool=%u, bbt=%u, bmt=%u); "
	       "markbad refused\n", layer.t.system_blocks, airoha_bmt_pool_blocks(&layer.t),
	       layer.t.bbt_size, layer.t.bmt_size);
	return 0;
}

static void detach_unlocked(struct mtd_info *mtd)
{
	if (layer.mtd != mtd)
		return;
#if BMT_WRITE
	managed_free();
#endif
	mtd->_read_oob = layer._read_oob;
	mtd->_write_oob = layer._write_oob;
	mtd->_erase = layer._erase;
	mtd->_block_isbad = layer._block_isbad;
	mtd->_block_markbad = layer._block_markbad;
#if BMT_WRITE
	if (layer.read_only)
		mtd->flags = layer.flags_before;
#endif
	memset(&layer, 0, sizeof(layer));
}

int airoha_bmt_attach(struct mtd_info *mtd)
{
	int rc;
#if BMT_WRITE
	rc=bmt_enter(); if(rc) return rc;
#endif
	rc=attach_unlocked(mtd);
#if BMT_WRITE
	bmt_leave();
#endif
	return rc;
}
int airoha_bmt_attached(const struct mtd_info *mtd)
{
	return mtd && layer.mtd == mtd;
}

void airoha_bmt_detach(struct mtd_info *mtd)
{
#if BMT_WRITE
	if(bmt_enter()) return;
#endif
	detach_unlocked(mtd);
#if BMT_WRITE
	bmt_leave();
#endif
}

#if defined(CONFIG_AIROHA_BMT) && !BMT_WRITE && !defined(AIROHA_BMT_HOST)
/* Read-only stages (the dumper) only: drop the translation so the chip can be
 * read as it is, every block and page in physical order, for a raw dump. A
 * stage that can write never has this: an untranslated write would land on
 * other blocks than stock reads. */
static int do_bmtdetach(struct cmd_tbl *cmdtp, int flag, int argc,
			char *const argv[])
{
	struct mtd_info *mtd = layer.mtd;

	if (argc != 1)
		return CMD_RET_USAGE;
	if (!mtd) {
		printf("airoha-bmt: not attached; the chip is already addressed physically\n");
		return CMD_RET_SUCCESS;
	}
	detach_unlocked(mtd);
	printf("airoha-bmt: detached; %s is addressed physically\n", mtd->name);
	return CMD_RET_SUCCESS;
}

U_BOOT_CMD(bmtdetach, 1, 0, do_bmtdetach,
	"address the NAND physically, without the vendor tables (read-only stages)", "");
#endif
