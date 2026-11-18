/* SPDX-License-Identifier: GPL-2.0-only */

#ifndef TEST_MTD_H
#define TEST_MTD_H
#include <linux/types.h>
#include <linux/errno.h>
#include <stddef.h>
#define MTD_OPS_PLACE_OOB 0
#define MTD_OPS_RAW 2
#define MTD_OPS_AUTO_OOB 1
#define MTD_WRITEABLE 0x400
struct mtd_req_stats { unsigned uncorrectable_errors, corrected_bitflips, max_bitflips; };
struct mtd_oob_ops { struct mtd_req_stats *stats; unsigned mode; size_t len,retlen,ooblen,oobretlen; unsigned ooboffs; u8 *datbuf,*oobbuf; };
#define MTD_ERASE_DONE 8
#define MTD_ERASE_FAILED 16
#define MTD_FAIL_ADDR_UNKNOWN -1LL
struct erase_info { u64 addr,len,fail_addr; struct mtd_info *mtd; unsigned long priv; unsigned state; };
struct mtd_info { u64 size; u32 erasesize,writesize,oobsize,oobavail,flags;
struct { void *parent; } dev;
void *_write_user_prot_reg, *_lock_user_prot_reg, *_erase_user_prot_reg;
int (*_read_oob)(struct mtd_info *,loff_t,struct mtd_oob_ops *);
int (*_write_oob)(struct mtd_info *,loff_t,struct mtd_oob_ops *);
int (*_erase)(struct mtd_info *,struct erase_info *);
int (*_block_isbad)(struct mtd_info *,loff_t);
int (*_block_markbad)(struct mtd_info *,loff_t);
};
static inline int mtd_is_bitflip(int err) { return err == -EUCLEAN; }
#endif
