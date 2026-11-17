/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef W1700K_SDR_EXPORT_H
#define W1700K_SDR_EXPORT_H

#include <linux/types.h>
#include "w1700k_sdr.h"

struct page;
struct net_device;
struct task_struct;

/* What the export needs of the stream ring header. */
struct sdr_dma_stream_header_view {
	const u64 *completed;
	const __u32 *ctrl;
	u32 slots, slices, slice_bytes, header_bytes;
};

/*
 * The stream ring's slices sent as UDP datagrams straight from their pages
 * (struct w1700k_sdr_stream_export). A kernel thread follows the completion
 * count the pump publishes; the pump itself does no network work.
 */
struct sdr_export {
	struct net_device *dev;		/* NULL: no export */
	struct w1700k_sdr_stream_export cfg;
	struct task_struct *task;
	struct page **pages;		/* the ring's, header first */
	u32 header_pages, slice_pages, slices, slice_bytes;
	u32 safe;			/* lag beyond which a slice may be rewritten */
	const __u32 *ctrl;
	const u64 *completed;
	u64 next;			/* next completion to send */
	u16 port_next, ip_id;
	u64 drain_until;
	bool stopping, done;
	int result;
	u64 sent, busy, unsent, skipped, last_ns;
	u32 max_lag, inflight_max;
};

/* Validate the configuration, hold the network device and start the thread
 * at the current completion. 0 with x->dev == NULL when cfg->ifindex is 0.
 */
int sdr_export_open(struct sdr_export *x, const struct w1700k_sdr_stream_export *cfg,
		    struct page **pages, const struct sdr_dma_stream_header_view *h);

/*
 * Pumping ended at `end` completions: let the thread send the rest for up to
 * cfg.drain_ms, stop it, wait (at most 2 s) until the device has released
 * every datagram, drop the device and report the export_* fields.
 */
void sdr_export_finish(struct sdr_export *x, u64 end, struct w1700k_sdr_stream_rx *out);

/* Datagrams still held by a network device; their destructor is in this
 * module and their pages belong to the ring.
 */
int sdr_export_inflight(void);

#endif
