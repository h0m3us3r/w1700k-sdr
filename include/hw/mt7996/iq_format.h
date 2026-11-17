/* SPDX-License-Identifier: MIT */
/*
 * MT7996 IQ sample formats, capture memory layouts, rates, and the stream
 * and export wire formats built on them.
 *
 * ADDRESS SPACES:
 *  - SYSRAM capture window: MCU bus 0x00464000..0x00467fff. The host reads it
 *    through the generic remap (host.h); 0x40f000..0x467fff and the alias
 *    0x400000..0x403fff are host-readable memory.                 [measured]
 *  - UMAC pool buffer (ICAP0 pool route): MCU bus 0xe0400000 + byte, pool
 *    byte 0 being the start of the ICAP partition; the same bytes are
 *    host-readable, uncached (mcu_map.h). The MCU caches 0xe0000000+
 *    write-back.                                                   [measured]
 *  - Stream ring and export datagrams: SoC DRAM / UDP, little-endian.
 *
 * Confidence tags: [measured] verified on hardware, [static] read from
 * firmware code, [inferred].
 */
#ifndef HW_MT7996_IQ_FORMAT_H
#define HW_MT7996_IQ_FORMAT_H

#include "../hw_bits.h"

/* ------------------------------------------------------------------------ */
/* Sample word: one complex sample in 32 bits.                    [measured] */
/* ------------------------------------------------------------------------ */
/* I in [29:16], Q in [13:0], each signed 14-bit two's complement, not sign-
 * extended: the spare bits [31:30] and [15:14] are always 0 in captured data.
 * In little-endian memory: u16 at +0 = Q, u16 at +2 = I.
 * Frequency convention: baseband I + jQ at +f is RF LO + f (no inversion).  */
#define MT7996_IQ_WORD_BYTES		4u
#define MT7996_IQ_BITS			14u
#define MT7996_IQ_I			HW_GENMASK(29, 16)
#define MT7996_IQ_Q			HW_GENMASK(13, 0)
#define MT7996_IQ_SPARE			0xc000c000u
#define MT7996_IQ_MAX			8191
#define MT7996_IQ_MIN			(-8192)
#define MT7996_IQ_SEXT14(v)		((int)(((unsigned int)(v) & 0x3fffu) ^ 0x2000u) - 0x2000)
#define MT7996_IQ_GET_I(w)		MT7996_IQ_SEXT14(HW_GET(MT7996_IQ_I, (unsigned int)(w)))
#define MT7996_IQ_GET_Q(w)		MT7996_IQ_SEXT14(HW_GET(MT7996_IQ_Q, (unsigned int)(w)))
#define MT7996_IQ_WORD(i, q)		(HW_PREP(MT7996_IQ_I, (unsigned int)(i) & 0x3fffu) | \
					 HW_PREP(MT7996_IQ_Q, (unsigned int)(q) & 0x3fffu))

/* ------------------------------------------------------------------------ */
/* Beat: the ICAP writer's 128-bit unit, four sample words w0..w3, low half */
/* = w0, w1 and high half = w2, w3. Content by datapath and ICAPCR10 packer */
/* (icap.h):                                                       [measured] */
/*                                                                          */
/*  2-sample datapath (bands 0 and 1):                                      */
/*   no packer   w0, w1 = path 0 t, t+1;  w2, w3 = path 1 t, t+1            */
/*   64->128     w0..w3 = path 0 t..t+3 (path 1 dropped)                    */
/*   32->64      w0, w1 = path 0 t, t+2 (every other sample);  w2 = w3 = 0  */
/*   96->128     3-word inputs (path 0 t, t+1; path 1 t) packed back to     */
/*               back across beats: 1365 inputs per 1024 beats              */
/*   24->32      the low 24 bits of slot 0 (Q[13:0], 2 zero bits, I[7:0]),  */
/*               4 units per 3 words, in w0 of each beat only               */
/*  Path 0/1 are the first/second ACTIVE RX stream, not fixed chains: with  */
/*  a single-chain mask path 1 is all zero.                                 */
/*                                                                          */
/*  Wide datapath (band 2 on a 320 MHz channel, 4 samples per 160 MHz       */
/*  clock per path; dfe.h lane mux): low half = the lane in LANE byte 0,    */
/*  high half = byte 1. With the 640 Msps lane word 0x09010800:             */
/*   no packer   w0 = t, w1 = t+2, w2 = t+1, w3 = t+3: one stream, "quad"   */
/*               order, time order w0, w2, w1, w3 (= lo.w0, hi.w0, lo.w1,   */
/*               hi.w1), the order the firmware's own dump reader uses.     */
/*   64->128     lane 0 of every clock: t, t+2, t+4, t+6 = every other      */
/*               sample, 320 Msps, unfiltered (beyond +-160 MHz folds)      */
/*   32->64      one sample per clock, 160 Msps, high half 0                */
/* ------------------------------------------------------------------------ */
#define MT7996_IQ_BEAT_BYTES		16u
#define MT7996_IQ_BEAT_WORDS		4u
/* Word index within the beat of quad sample k (k = 0..3, time order). */
#define MT7996_IQ_QUAD_WORD(k)		((((unsigned int)(k) & 1u) << 1) | (((unsigned int)(k) >> 1) & 1u))

/* ------------------------------------------------------------------------ */
/* SYSRAM capture window: 16 KiB, 1024 beats, 8 banks of 2 KiB.  [measured] */
/* Beat b (= ICAP address unit b * 4): low 64 bits at                       */
/* 0x464000 + (b >> 9) * 0x2000 + (b & 511) * 8, high 64 bits 0x1000 on.    */
/* Pages: 0x464000 beats 0-511 low, 0x465000 beats 0-511 high,              */
/* 0x466000 beats 512-1023 low, 0x467000 beats 512-1023 high.               */
/* One capture holds 2 streams x 2048 samples, or 1 x 4096 (64->128 or     */
/* quad). Every capture rewrites it, re-arms included.                      */
/*                                                                          */
/* HAZARD: while ICAP EN is set, each page reads back as its own last 8     */
/*   bytes repeated 512 times; while a bank is granted by the route, its    */
/*   host view is frozen. Clear EN and the route grant, then read.          */
/* HAZARD: SYSRAM 0x404000..0x40efff and 0x468000..0x47ffff are not memory  */
/*   from the host: reads return a latch.                                   */
/* ------------------------------------------------------------------------ */
#define MT7996_IQ_WIN_BASE		0x00464000u
#define MT7996_IQ_WIN_SIZE		0x00004000u
#define MT7996_IQ_WIN_BEATS		1024u
#define MT7996_IQ_WIN_HALF_OFS		0x00001000u	/* high 64 bits of a beat */
/* Byte offset within the window of beat b's low / high 64 bits. */
#define MT7996_IQ_WIN_OFS_LO(b) \
	((((unsigned int)(b) >> 9) * 0x2000u) + (((unsigned int)(b) & 511u) * 8u))
#define MT7996_IQ_WIN_OFS_HI(b)		(MT7996_IQ_WIN_OFS_LO(b) + MT7996_IQ_WIN_HALF_OFS)
#define MT7996_IQ_WIN_LO(b)		(MT7996_IQ_WIN_BASE + MT7996_IQ_WIN_OFS_LO(b))
#define MT7996_IQ_WIN_HI(b)		(MT7996_IQ_WIN_BASE + MT7996_IQ_WIN_OFS_HI(b))
/* Window half (512 beats = 2048 samples in all) fill time = the deadline for
 * a half hand-off during a ring, at an aggregate rate in Msps: 25.6 us at
 * 2 x 40, 12.8 us at 2 x 80, 3.2 us at 640.            [measured] [inferred] */
#define MT7996_IQ_WIN_HALF_SAMPLES	2048u
#define MT7996_IQ_WIN_HALF_NS(msps)	(MT7996_IQ_WIN_HALF_SAMPLES * 1000u / (unsigned int)(msps))

/* ------------------------------------------------------------------------ */
/* Pool buffer (ICAP0 only, cap_route.h +0x28[0]).                [measured] */
/* ICAP address unit u is pool byte 4u: beats are 16 contiguous bytes, in   */
/* the beat format above, with no bank interleave. The ICAP address space   */
/* caps the buffer at 1 MiB; its usable size is the ICAP partition of the   */
/* SYSRAM pool (mcu_map.h): 512 KiB with a 256-bank ICAP partition, 1 MiB   */
/* with 512 banks. A ring wraps phase-continuously.                         */
/* 512 KiB = 32768 beats: 1.64 ms at 2 x 40 Msps, 819 us at 2 x 80 Msps,    */
/* 205 us at 2 x 320 Msps or 1 x 640 Msps.                                  */
/*                                                                          */
/* HAZARD: an MCU reader goes through the write-back D-cache; drop its      */
/*   lines before the writer comes round again (one lap of a 1 MiB ring at  */
/*   1.28 GB/s is 819 us), and read a chunk only once the following chunk   */
/*   is written (the prefetcher reads past the end).                        */
/* HAZARD: see icap.h for the host pool read that hung PCIe after a ring    */
/*   with a SWITCH pulse.                                                   */
/* ------------------------------------------------------------------------ */
#define MT7996_IQ_POOL_BASE		0xe0400000u
#define MT7996_IQ_POOL_ADDR(unit)	(MT7996_IQ_POOL_BASE + ((unsigned int)(unit) << 2))
#define MT7996_IQ_POOL_MAX		0x00100000u
#define MT7996_IQ_POOL_512K		0x00080000u

/* ------------------------------------------------------------------------ */
/* Stream chunks.                                                 [measured] */
/* The streaming producer rings ICAP0 over the pool buffer and publishes    */
/* every 8 KiB chunk, in order, on MCU_DMA0 TX ring 0 into host WFDMA RX    */
/* ring 0 (wfdma.h); 8 KiB is the largest power of two the 14-bit           */
/* descriptor length carries. Chunk k is ring bytes (k * 8192) mod ring.    */
/* Standard setting: ICAPCR10 = 64->128, so a raw chunk is 512 beats =      */
/* 2048 consecutive samples of path 0, in word order. Firmware events share */
/* the same ring and are shorter than 4096 bytes. The chunk length alone   */
/* tells the three apart: 8192 raw, 7168 packed, < 4096 event.              */
/* ------------------------------------------------------------------------ */
#define MT7996_IQ_CHUNK_BYTES		8192u
#define MT7996_IQ_CHUNK_SAMPLES		2048u
#define MT7996_IQ_CHUNK_PACKED_BYTES	7168u
/* A radio-reduced chunk (the split schedule, 320 Msps on a link too slow for
 * the lossless ones): 7 bits of each of I and Q under one shift s (0..7) for
 * the chunk, q = (x >> s) & 0x7f: two's complement, rounded down, exact while
 * -64 <= x >> s <= 63 and wrapped beyond (the host sets s from the levels it
 * sees in the chunks between, with room to spare). Two samples fold into one
 * word shaped like a sample word,
 *   Q(2j) | Q(2j+1) << 7 in [13:0],  I(2j) | I(2j+1) << 7 in [29:16],
 * and those 1024 words are packed 8 to 7 as below: 896 words. Then one
 * trailer word, RADIO14_TRAILER | s. 3588 bytes: the length tells it from an
 * event (shorter than 4096 too) together with the trailer.
 * A sample is read back as (q << s) + ((1 << s) >> 1), the middle of its step.
 * At 320 Msps, each packer on 5 of every 16 chunks, the kernel costs 11.5k
 * cycles a chunk on the WM (78 % busy) and 12.8k on the WA (87 %).  [measured] */
#define MT7996_IQ_CHUNK_RADIO14_BYTES	3588u
#define MT7996_IQ_RADIO14_DATA_BYTES	3584u
#define MT7996_IQ_RADIO14_TRAILER	0x52443100u	/* "RD1", then the shift in [2:0] */
#define MT7996_IQ_RADIO14_TRAILER_MASK	0xffffff00u
#define MT7996_IQ_RADIO14_SHIFT_MAX	7u
#define MT7996_IQ_CHUNK_EVENT_MAX	4095u	/* longest non-sample completion */
#define MT7996_IQ_CHUNKS_PER_MIB	128u	/* one 1 MiB ICAP lap */
/* Length of a ring completion from its ctrl word (DDONE | LS0 | len << 16;
 * struct w1700k_sdr_stream_ring in include/uapi/w1700k_sdr.h). */
#define MT7996_IQ_CTRL_LEN		HW_GENMASK(29, 16)

/* Packed chunk: of every 8 raw words w0..w7 (spare bits clear), w7's 28 data
 * bits go into the spare bits of w0..w6, 2 + 2 bits each:
 *   o_k = w_k | ((w7 << (14 - 2k)) & 0xc000c000),  k = 0..6
 * so o_k[15:14] = w7[2k+1:2k] (Q bits) and o_k[31:30] = w7[2k+17:2k+16]
 * (I bits). 8 words -> 7 (28-bit samples), 8192 -> 7168 bytes. The MCU
 * packers pack a fixed share of chunks (positions within a power-of-two
 * period of at most 32 chunks); half the chunks packed carries 320 Msps on
 * one 10 Gbit/s link at 97 % load. Every chunk packed runs to 160 Msps
 * (4.52 Gbit/s on the wire): 11.4k cycles a chunk on either packer, each
 * 62 % busy, the capture at most 4.6 chunks ahead.                 [measured] */
#define MT7996_IQ_PACK_GROUP_IN		8u
#define MT7996_IQ_PACK_GROUP_OUT	7u
#define MT7996_IQ_PACK_WORD(w, w7, k) \
	((unsigned int)(w) | (((unsigned int)(w7) << (14u - 2u * (unsigned int)(k))) & MT7996_IQ_SPARE))
#define MT7996_IQ_UNPACK_WORD(o)	((unsigned int)(o) & ~MT7996_IQ_SPARE)
/* OR the seven results for k = 0..6 to rebuild w7. */
#define MT7996_IQ_UNPACK_W7_PART(o, k) \
	(((unsigned int)(o) & MT7996_IQ_SPARE) >> (14u - 2u * (unsigned int)(k)))

/* ------------------------------------------------------------------------ */
/* Export datagram: each completed SoC ring slice leaves as UDP/IPv4 (DF   */
/* set, UDP checksum valid), straight from the ring pages.        [measured] */
/* Payload = this 16-byte header + bytes [offset, offset + n) of slice      */
/* `seq`. A 10 Gbit/s path sends whole slices (8192 + 16 bytes of UDP       */
/* payload; MTU >= 8236, 9000 used); a paced path sends pieces. The source  */
/* port cycles over a range so receive flows spread across queues.          */
/* Receivers: a missing seq is a lost slice; raw slices must have the spare */
/* bits clear; a slice equal to the one 128 slices earlier (one 1 MiB ICAP  */
/* lap) means the reader overtook the writer.                               */
/* Wire budget: 8192 payload bytes per 8274 on the wire, so one 10G link    */
/* carries at most 1.238 GB/s against 1.28 GB/s raw at 320 Msps.            */
/* ------------------------------------------------------------------------ */
/* A slice reduced on the SoC (iq_reduce.h) comes under "SDR2": the same      */
/* header with seq in bits [47:0] of the last field, the format (bits per I/Q */
/* pair) in [55:48] and the scale code in [63:56]. Datagrams of the unit's    */
/* own senders may hold several whole slices, each behind its header; a       */
/* record shorter than the rest of its slice is a piece and ends its datagram.*/
#define MT7996_IQ_EXPORT_MAGIC		0x31524453u	/* "SDR1", le32 */
#define MT7996_IQ_EXPORT_MAGIC_REDUCED	0x32524453u	/* "SDR2", le32 */
#define MT7996_IQ_EXPORT_HDR_BYTES	16u
#define MT7996_IQ_EXPORT_OFS_MAGIC	0u	/* le32 */
#define MT7996_IQ_EXPORT_OFS_OFFSET	4u	/* le16: offset of this piece in the slice */
#define MT7996_IQ_EXPORT_OFS_SLICE_BYTES 6u	/* le16: completion length (8192/7168/<4096) */
#define MT7996_IQ_EXPORT_OFS_SEQ	8u	/* le64: completion number; slice = seq % slices */
#define MT7996_IQ_EXPORT_UDP_PORT	7700u	/* tools' default destination port */

/* ------------------------------------------------------------------------ */
/* Rates                                                           [measured] */
/* The channel bandwidth sets the data rate: 2 x channel bandwidth complex  */
/* samples/s per stream. The capture path writes no ADC clock; SPECTRUM     */
/* u4BW only sets the engine's sampling (dfe.h RATE), which holds samples   */
/* when faster than the data and decimates without a filter when slower.    */
/*                                                                          */
/*   band 0 (2.4 GHz, 4 chains)  BW20:  2 streams x  40 Msps per instance   */
/*                               BW40:  2 streams x  80 Msps per instance   */
/*                               BW160: 2 streams x 320 Msps (our channel   */
/*       set: MediaTek's 2.4 GHz tables end at BW40; chanset.h)             */
/*       ICAP0 carries streams 0-1, ICAP1 streams 2-3. All four at once     */
/*       only with ICAP0 into the pool and ICAP1 into the window.           */
/*   band 1 (5 GHz, 4 chains)    BW160: 2 streams x 320 Msps                */
/*   band 2 (6 GHz, 4+1 chains)  BW320: 1 stream  x 640 Msps (u4BW 5), or   */
/*       any two 64-bit lanes, e.g. 2 paths x 320 Msps of even samples      */
/*       (unfiltered)                                                       */
/*   Window: 4096 samples per capture in all cases (51.2 us at 2 x 40,      */
/*   25.6 us at 2 x 80, 6.4 us at 2 x 320 or 1 x 640 Msps).                 */
/*                                                                          */
/* Writer ceiling: one beat per 160 MHz clock = 640 M samples/s, reached on */
/* bands 1 and 2. Streamed: one path at 320 Msps (64->128) = 1.28 GB/s raw. */
/*                                                                          */
/* HAZARD: a u4BW above the channel's bandwidth only oversamples; on BW20,  */
/*   u4BW 1/2/4/5 repeat exactly 50/75/87.5/87.5 % of adjacent samples.     */
/*   Check for repeats before trusting a rate.                              */
/* ------------------------------------------------------------------------ */
#define MT7996_IQ_RATE_SPS(bw_mhz)	(2000000u * (unsigned int)(bw_mhz))
#define MT7996_IQ_WRITER_CLOCK_HZ	160000000u
#define MT7996_IQ_WRITER_MAX_SPS	640000000u
#define MT7996_IQ_STREAM_SPS		320000000u
#define MT7996_IQ_STREAM_BYTES_PER_S	1280000000u

#endif /* HW_MT7996_IQ_FORMAT_H */
