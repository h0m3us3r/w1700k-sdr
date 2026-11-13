// SPDX-License-Identifier: GPL-2.0-only
/*
 * Stream export: each completed slice leaves as one UDP datagram whose payload
 * is the slice's own pages, so the only read of the samples after the device
 * wrote them is the MAC's DMA. A datagram is a 58-byte linear head (Ethernet,
 * IPv4, UDP, struct w1700k_sdr_export_header) plus one fragment per page of
 * the slice, each holding a page reference. The MAC computes the UDP checksum.
 *
 * Sends use __dev_direct_xmit, bypassing the qdisc: a full device queue
 * refuses the datagram and the slice is offered again, so the device's
 * backpressure is the only flow control. For that to be lossless on the wire
 * the QDMA congestion drop (airoha_eth TXQ_CNGST_DROP_EN) must be off.
 */
#include <linux/atomic.h>
#include <linux/delay.h>
#include <linux/etherdevice.h>
#include <linux/if_arp.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/kthread.h>
#include <linux/mm.h>
#include <linux/netdevice.h>
#include <linux/sched.h>
#include <linux/skbuff.h>
#include <linux/timekeeping.h>
#include <linux/udp.h>
#include <net/checksum.h>
#include <net/ip.h>
#include <net/net_namespace.h>
#include "export.h"
#include "hw/mt7996/wfdma.h"

#define SDR_EXPORT_HEAD (ETH_HLEN + sizeof(struct iphdr) + sizeof(struct udphdr) + \
			 sizeof(struct w1700k_sdr_export_header))
/* A DSA port (IFF_NO_QUEUE) never stops its queue: it hands every packet to
 * the conduit's qdisc, which drops when full. Keeping at most this many
 * datagrams outstanding keeps that qdisc from filling.
 */
#define SDR_EXPORT_NOQUEUE_INFLIGHT 256
#define SDR_EXPORT_CLOSE_MS 2000
/* Slices of margin behind the device's write position. */
#define SDR_EXPORT_MARGIN 64
/* A short-completions export that fell too far behind resumes this close to the newest. */
#define SDR_EXPORT_RESUME 256

/* Module-wide: the driver binds the one MT7996 primary interface. */
static atomic_t sdr_export_queued = ATOMIC_INIT(0);

int sdr_export_inflight(void)
{
	return atomic_read(&sdr_export_queued);
}

static void sdr_export_skb_free(struct sk_buff *skb)
{
	atomic_dec(&sdr_export_queued);
}

static int sdr_export_thread(void *arg);

static int sdr_export_check_dev(const struct net_device *dev, u32 payload)
{
	if (dev->type != ARPHRD_ETHER || !netif_running(dev) ||
	    !(dev->features & NETIF_F_SG) ||
	    dev->mtu < payload + SDR_EXPORT_HEAD - ETH_HLEN)
		return -EINVAL;
	return 0;
}

int sdr_export_open(struct sdr_export *x, const struct w1700k_sdr_stream_export *cfg,
		    struct page **pages, const struct sdr_dma_stream_header_view *h)
{
	int ret = -EINVAL;

	memset(x, 0, sizeof(*x));
	if (!cfg->ifindex)
		return 0;
	if ((cfg->flags & ~W1700K_SDR_EXPORT_FLAGS) || !cfg->dst_ip || !cfg->dst_port ||
	    !is_valid_ether_addr(cfg->dst_mac) || !pages || !h->slices ||
	    !h->slice_bytes || h->slice_bytes % PAGE_SIZE ||
	    h->header_bytes % PAGE_SIZE ||
	    h->slices <= h->slots + SDR_EXPORT_MARGIN)
		return -EINVAL;
	if (cfg->cpu && (cfg->cpu > nr_cpu_ids || !cpu_online(cfg->cpu - 1)))
		return -EINVAL;
	x->dev = dev_get_by_index(&init_net, cfg->ifindex);
	if (!x->dev)
		return -ENODEV;
	if (sdr_export_check_dev(x->dev, h->slice_bytes))
		goto fail;
	x->cfg = *cfg;
	x->pages = pages;
	x->header_pages = h->header_bytes >> PAGE_SHIFT;
	x->slice_pages = h->slice_bytes >> PAGE_SHIFT;
	x->slices = h->slices;
	x->slice_bytes = h->slice_bytes;
	/* The device writes up to `slots` slices ahead of `completed`. */
	x->safe = h->slices - h->slots - SDR_EXPORT_MARGIN;
	x->ctrl = h->ctrl;
	x->completed = h->completed;
	x->next = smp_load_acquire(x->completed);	/* pairs with the ring's release */
	x->task = kthread_create(sdr_export_thread, x, "sdr-export");
	if (IS_ERR(x->task)) {
		ret = PTR_ERR(x->task);
		x->task = NULL;
		goto fail;
	}
	get_task_struct(x->task);
	if (cfg->cpu)
		kthread_bind(x->task, cfg->cpu - 1);
	wake_up_process(x->task);
	return 0;
fail:
	dev_put(x->dev);
	x->dev = NULL;
	return ret;
}

static bool sdr_export_full(const struct sdr_export *x)
{
	if (netif_xmit_frozen_or_stopped(netdev_get_tx_queue(x->dev, 0)))
		return true;
	return (x->dev->priv_flags & IFF_NO_QUEUE) &&
	       atomic_read(&sdr_export_queued) >= SDR_EXPORT_NOQUEUE_INFLIGHT;
}

/* Attach the slice's bytes, one fragment and one page reference per page. */
static void sdr_export_attach(struct sk_buff *skb, struct page **pages, u32 len)
{
	u32 at = 0, n;
	int frag = 0;

	while (at < len) {
		n = min_t(u32, len - at, PAGE_SIZE);
		get_page(pages[at >> PAGE_SHIFT]);
		skb_fill_page_desc(skb, frag++, pages[at >> PAGE_SHIFT], 0, n);
		at += n;
	}
	skb->len += len;
	skb->data_len += len;
	skb->truesize += len;
}

/* One datagram carrying completion `seq` (`len` bytes of its slice). */
static struct sk_buff *sdr_export_skb(struct sdr_export *x, u64 seq, u32 len)
{
	const struct w1700k_sdr_stream_export *c = &x->cfg;
	struct net_device *dev = x->dev;
	struct w1700k_sdr_export_header *h;
	unsigned int room = LL_RESERVED_SPACE(dev);
	struct sk_buff *skb;
	struct udphdr *uh;
	struct ethhdr *eth;
	struct iphdr *iph;
	u16 udp_len, ports;

	skb = alloc_skb(room + SDR_EXPORT_HEAD, GFP_KERNEL | __GFP_NOWARN);
	if (!skb)
		return NULL;
	skb_reserve(skb, room + SDR_EXPORT_HEAD - sizeof(*h));
	h = skb_put(skb, sizeof(*h));
	h->magic = cpu_to_le32(W1700K_SDR_EXPORT_MAGIC);
	h->offset = 0;
	h->slice_bytes = cpu_to_le16(len);
	h->seq = cpu_to_le64(seq);
	sdr_export_attach(skb, x->pages + x->header_pages +
			  (size_t)(seq % x->slices) * x->slice_pages, len);

	udp_len = sizeof(*uh) + sizeof(*h) + len;
	uh = skb_push(skb, sizeof(*uh));
	skb_reset_transport_header(skb);
	/* Cycling the source port spreads the flow over the receiver's queues. */
	ports = c->src_ports ? c->src_ports : 1;
	uh->source = htons(ntohs(c->src_port) + x->port_next++ % ports);
	uh->dest = c->dst_port;
	uh->len = htons(udp_len);
	uh->check = ~csum_tcpudp_magic(c->src_ip, c->dst_ip, udp_len, IPPROTO_UDP, 0);
	skb->ip_summed = CHECKSUM_PARTIAL;
	skb->csum_start = skb_transport_header(skb) - skb->head;
	skb->csum_offset = offsetof(struct udphdr, check);

	iph = skb_push(skb, sizeof(*iph));
	skb_reset_network_header(skb);
	*iph = (struct iphdr){
		.version = 4, .ihl = 5, .tot_len = htons(sizeof(*iph) + udp_len),
		.id = htons(x->ip_id++), .frag_off = htons(IP_DF), .ttl = 64,
		.protocol = IPPROTO_UDP, .saddr = c->src_ip, .daddr = c->dst_ip,
	};
	ip_send_check(iph);

	eth = skb_push(skb, ETH_HLEN);
	skb_reset_mac_header(skb);
	ether_addr_copy(eth->h_dest, c->dst_mac);
	ether_addr_copy(eth->h_source, dev->dev_addr);
	eth->h_proto = htons(ETH_P_IP);

	skb->dev = dev;
	skb->protocol = htons(ETH_P_IP);
	skb->destructor = sdr_export_skb_free;
	atomic_inc(&sdr_export_queued);
	return skb;
}

/* Send what the device accepts of the completions below `completed`. Never
 * blocks; 0, or -ENOBUFS once the oldest unsent slice may be rewritten.
 */
static int sdr_export_pump(struct sdr_export *x, u64 completed)
{
	struct sk_buff *skb;
	u32 len, queued;
	u64 lag;
	int ret;

	while (x->next < completed) {
		lag = completed - x->next;
		if (lag > x->safe) {
			if (!(x->cfg.flags & W1700K_SDR_EXPORT_SHORT))
				return -ENOBUFS;
			/* Beside senders that carry on, giving up would cost the rest of
			 * the stream its short completions (seen: one stall of 0.8 s, and
			 * 62 % of every slice after it missing). Pass over what the device
			 * may be rewriting, count it, and go on from the newest.
			 */
			x->skipped += lag - SDR_EXPORT_RESUME;
			x->next = completed - SDR_EXPORT_RESUME;
			x->max_lag = max_t(u32, x->max_lag, x->safe);
			continue;
		}
		x->max_lag = max_t(u32, x->max_lag, lag);
		len = HW_GET(WFDMA_DESC_CTRL_SD_LEN0, READ_ONCE(x->ctrl[x->next % x->slices]));
		len = min(len, x->slice_bytes);
		if ((x->cfg.flags & W1700K_SDR_EXPORT_SHORT) && len >= x->slice_bytes) {
			x->next++;	/* a full slice: the mapping's reader sends it */
			continue;
		}
		/* An empty completion still goes out, as a bare header: a
		 * receiver then sees every sequence number and no false gap.
		 */
		if (sdr_export_full(x))
			return 0;
		skb = sdr_export_skb(x, x->next, len);
		if (!skb)
			return 0;
		ret = __dev_direct_xmit(skb, 0);
		if (ret != NETDEV_TX_OK) {
			/* Busy: still ours to free. Dropped: already freed.
			 * Either way the slice is offered again.
			 */
			if (!dev_xmit_complete(ret))
				kfree_skb(skb);
			x->busy++;
			return 0;
		}
		queued = atomic_read(&sdr_export_queued);
		x->inflight_max = max(x->inflight_max, queued);
		x->sent++;
		x->last_ns = ktime_get_ns();
		x->next++;
	}
	return 0;
}

/* Busy-polls the published completion count on its own CPU. */
static int sdr_export_thread(void *arg)
{
	struct sdr_export *x = arg;
	const bool polite = x->cfg.flags & W1700K_SDR_EXPORT_SHORT;
	u64 end, sent;
	int ret;

	while (!kthread_should_stop()) {
		/* Slices below `completed` have been synced for the CPU. */
		end = smp_load_acquire(x->completed);
		sent = x->sent;
		ret = sdr_export_pump(x, end);
		if (ret) {
			x->result = ret;
			break;
		}
		if (smp_load_acquire(&x->stopping) &&	/* drain_until is set first */
		    (x->next >= end || ktime_get_ns() >= READ_ONCE(x->drain_until)))
			break;
		/* Sharing its CPUs with the senders: wait for work rather than poll. */
		if (polite && x->sent == sent)
			usleep_range(20, 50);
		else
			cond_resched();
	}
	end = smp_load_acquire(x->completed);	/* as above */
	x->unsent = end > x->next ? end - x->next : 0;
	smp_store_release(&x->done, true);	/* counters before done */
	while (!kthread_should_stop()) {
		set_current_state(TASK_INTERRUPTIBLE);
		if (kthread_should_stop()) {
			__set_current_state(TASK_RUNNING);
			break;
		}
		schedule();
	}
	return 0;
}

void sdr_export_finish(struct sdr_export *x, u64 end, struct w1700k_sdr_stream_rx *out)
{
	unsigned int waited;
	int result;

	if (!x->dev)
		return;
	WRITE_ONCE(x->drain_until, ktime_get_ns() + (u64)x->cfg.drain_ms * NSEC_PER_MSEC);
	smp_store_release(&x->stopping, true);	/* drain_until before stopping */
	for (waited = 0; waited < x->cfg.drain_ms + 1000; waited++) {
		if (smp_load_acquire(&x->done))	/* the thread's counters are final */
			break;
		usleep_range(1000, 2000);
	}
	kthread_stop_put(x->task);
	x->task = NULL;
	result = x->result;
	if (!x->done) {
		x->unsent = end > x->next ? end - x->next : 0;
		result = result ? result : -ETIMEDOUT;
	}
	for (waited = 0; waited < SDR_EXPORT_CLOSE_MS && sdr_export_inflight(); waited++)
		usleep_range(1000, 2000);
	if (sdr_export_inflight() && !result)
		result = -ETIMEDOUT;	/* the device still holds datagrams */
	dev_put(x->dev);
	x->dev = NULL;
	out->export_result = result;
	out->export_slices = x->sent;
	out->export_busy = x->busy;
	out->export_unsent = x->unsent + x->skipped;
	out->export_last_ns = x->last_ns;
	out->export_max_lag = x->max_lag;
	out->export_inflight_max = x->inflight_max;
}
