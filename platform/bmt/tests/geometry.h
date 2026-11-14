/* SPDX-License-Identifier: GPL-2.0-only */
/* The part a harness models: the W25N04KV, or the W25N04LV with
 * -DTEST_W25N04LV. Every harness runs once for each. */
#ifndef TEST_GEOMETRY_H
#define TEST_GEOMETRY_H

#ifdef TEST_W25N04LV
#define T_TOTAL 2048u		/* blocks */
/* The LV's pool is the vendor scan's: 163 good blocks from the top plus the
 * two factory-bad blocks T_FACTORY_TOP_A/B the binding harnesses place in
 * that span. The table-level harnesses set the same pool directly, and also
 * run with -DT_POOL=250u, the clamp. */
#ifndef T_POOL
#define T_POOL 165u
#endif
#define T_PS 4096u		/* main bytes per page */
#define T_OS 256u		/* OOB bytes per page */
#define T_FREE 112u		/* free, ECC-protected OOB bytes per page */
#else
#define T_TOTAL 4096u
#define T_POOL 250u		/* always clamped: nothing is scanned */
#define T_PS 2048u
#define T_OS 128u
#define T_FREE 56u
#endif

#define T_SYSTEM (T_TOTAL - T_POOL)	/* the reserve's first block, the BBT's */
#define T_LAST (T_TOTAL - 1u)		/* the BMT's block */
#define T_PAGES 64u
#define T_BS (T_PAGES * T_PS)
#define T_SECTIONS (T_FREE / 14u)	/* 16-byte spare groups with free bytes */
/* The writable range, `system` to the end of `durable`, in blocks. */
#define T_FIRST ((unsigned)(0x700000ull / T_BS))
#define T_END ((unsigned)(0x4600000ull / T_BS))
#define T_STRIDE (T_PS + T_FREE)
/* Factory-bad blocks in the LV's scanned span (OOB byte 0 cleared). */
#define T_FACTORY_TOP_A (T_LAST - 40u)
#define T_FACTORY_TOP_B (T_LAST - 41u)
#ifdef TEST_W25N04LV
#define T_FACTORY_TOP(b) ((b) == T_FACTORY_TOP_A || (b) == T_FACTORY_TOP_B)
#else
#define T_FACTORY_TOP(b) 0
#endif

#endif
