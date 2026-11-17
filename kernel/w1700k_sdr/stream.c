// SPDX-License-Identifier: GPL-2.0-only
/*
 * mt7996-stream DEVICE --seconds S [--idle-ms N] [--go ADDR VALUE]
 *               [--stop ADDR VALUE] [--threads N] [--touch]
 *               [--data BYTES FILE] [--wrap] [--meta FILE] [--snap SLICES FILE]
 *               [--export IF SRC_IP DST_IP DST_MAC PORT] [--ports N]
 *               [--drain-ms N] [--export-cpu N] [--cpu N]
 *               [--send SRC_IP DST_IP PORT] [--format BITS] [--piece BYTES]
 *               [--datagram BYTES] [--senders N] [--split]
 *
 * One stream through the 1 GiB stream ring. The main thread runs the driver's
 * pump (STREAM_RX) for up to S seconds; the driver writes the go word to the
 * waiting producer, and on the deadline, a signal (SIGINT, SIGTERM, SIGHUP)
 * or --idle-ms without data writes the stop word and drains.
 * --cpu pins the pump thread.
 *
 * --threads readers (default 2, at most 3) follow the ring in place and count
 * what they see; --touch makes them read every payload word, as a consumer
 * would. A reader more than slices - slots behind counts as lapped.
 *
 * Afterwards --data writes up to BYTES of the stream's payload still held by
 * the ring, from the oldest completion kept or, with --wrap, centred on the
 * last wrap to slice 0; --meta writes each of those completions' ctrl word
 * (native u32). --snap: each SIGUSR1 copies the newest SLICES slices (64
 * behind the newest) to FILE and writes FILE.json {seq, chunk, slices, taken}.
 *
 * --export sends every slice as UDP from IF (see the uapi header); --ports
 * cycles the source port over N values from PORT, --drain-ms lets the export
 * catch up after pumping, --export-cpu pins the export thread.
 *
 * --send does the same from this process instead, through UDP sockets (one
 * copy, and the ordinary network stack): --senders threads (default 2, at
 * most 4) take the completions in turn, a few at a time. With --format 16,
 * 14, 8 or 7 a raw slice leaves reduced to that many bits per I/Q pair
 * (hw/mt7996/iq_reduce.h) under an "SDR2" header, the same fields with the
 * format in bits [55:48] and the slice's scale code in [63:56] of the last;
 * anything else (events, packed slices) leaves as it is under "SDR1".
 * --datagram lets a datagram carry several whole slices, each behind its own
 * header, up to BYTES of UDP payload (the path's MTU less 28). --piece splits
 * every slice into datagrams of at most BYTES of it (256..8192), for a path
 * without jumbo frames; a slice's pieces share a source port, in order.
 *
 * --split (with --export and --send --format 14) is for a stream whose radio
 * reduces 10 of every 16 chunks itself (the split schedule: 320 Msps on a link
 * the lossless schedules do not fit). The driver's export then carries only
 * the completions shorter than a slice, zero-copy, and the senders only the
 * raw ones, reduced. The radio cuts its chunks to 7 bits under a shift it is
 * told: a thread here keeps that shift wide enough for twice the largest
 * magnitude the senders saw in the last second (they read the chunks in
 * between), writing the mailbox word when it changes. A level that more than
 * doubles within a millisecond wraps in the radio's chunks until the next
 * write; the receiver counts the chunks whose neighbours say so.
 *
 * Reading the slices costs the SoC's memory bus: with the capture running at
 * 320 Msps one sender reads 0.60 GB/s, two 1.0 and three 0.93, so the senders
 * carry 160 Msps (0.64 GB/s) and not 320 (1.28).
 * Senders the ring laps skip to the newest completions. With --cpu the pump
 * keeps its CPU to itself (held up for 10 ms at 320 Msps it runs the device
 * out of descriptors, which ends the stream); each sender has one of the
 * others to itself where there are enough, the network device's NAPI threads
 * what is left, and the driver's export thread any but the pump's; all of
 * them run above ordinary processes while the stream does.
 *
 * Prints one `stream` JSON line.
 */
#include "client.h"
#include <arpa/inet.h>
#include <dirent.h>
#include <endian.h>
#include <net/if.h>
#include <netinet/in.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stddef.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <time.h>
#include "hw/mt7996/iq_reduce.h"
#include "hw/mt7996/probe_page.h"
#include "hw/mt7996/wfdma.h"

#define THREADS_MAX 3U
#define SNAP_MARGIN 64U
#define SENDERS_MAX 4U
#define SEND_BATCH 64U		/* datagrams, and slices or pieces, per sendmmsg */
/* Slices per batch: few, so that what a sender has reduced is still in the
 * cache when the kernel copies it out. The four cores share a 512 KiB L2, and a
 * batch of 16 (128 KiB read, 58 KiB written) pushed its own output out to
 * memory and fetched it back: at 320 Msps with the radio reducing half the
 * chunks, two senders reduced 92 % of the other half in batches of 16, and all
 * of it in batches of 2, 4 or 8.
 */
#define SEND_SLICES 4U
#define SEND_CLAIM 4U		/* completions a sender takes at a time */
#define SEND_PORTS_MAX 64U	/* sockets per sender */
#define SEND_PIECE_MIN 256U
#define SEND_SNDBUF (1U << 20)
#define SEND_IDLE_NS 20000L
#define SEND_BUSY_NS 100000L
#define SEND_RESUME 256U	/* a lapped sender resumes this many slices behind the newest */
#define SEND_MAGIC_REDUCED 0x32524453U	/* "SDR2" */
/* Scheduling: the pump and the driver's export thread first (-20), then the senders and the
 * network threads that complete their transmissions, all above ordinary processes: a shell, the
 * control client or an ssh session on the unit must not take a sender's CPU (two busy loops did,
 * and a 320 Msps stream lost a fifth of its slices while they ran).
 */
#define NICE_FIRST (-20)
#define NICE_STREAM (-10)
#define SHIFT_TICK_NS 1000000L	/* the radio's shift follows the senders' peaks every millisecond */
#define SHIFT_SPANS 100U	/* and holds the largest of this many spans */
#define SHIFT_SPAN_TICKS 10U	/* of 10 ms each: the last second */

struct reader {
	pthread_t tid;
	uint64_t next, buffers, bytes, full, lapped, checksum;
};

struct sender {
	pthread_t tid;
	uint64_t slices, reduced, datagrams, lapped, errors, busy;
	uint32_t max_lag;	/* the furthest behind the device it took a completion */
	int fds[SEND_PORTS_MAX];
	uint32_t fd_count, fd_next;
};

/* One sendmmsg: whole slices, each as a record (header, bytes) of a datagram
 * that may hold several, or as pieces, a datagram each.
 */
struct batch {
	struct mmsghdr msgs[SEND_BATCH];
	struct iovec iov[2 * SEND_BATCH];
	struct w1700k_sdr_export_header head[SEND_BATCH];
	uint32_t count, records, slices;
	uint32_t open_bytes;	/* payload of the last datagram, 0: closed to more */
	_Alignas(16) uint8_t data[SEND_SLICES][IQ_REDUCE_MAX_BYTES + IQ_REDUCE_SLACK];
};

struct send_options {
	struct in_addr src, dst;
	uint32_t port, ports, format, piece, datagram, senders;
	int set, split;
};

struct options {
	double seconds;
	uint32_t idle_ms, threads, data_cap, pump_cpu, snap_slices, ports;
	const char *data_path, *meta_path, *snap_path;
	int touch, wrap, export_set, cpu_set;
	struct send_options send;
	struct w1700k_sdr_stream_rx req;
};

static const struct w1700k_sdr_stream_ring *ring;
static const uint8_t *slices;
static atomic_int pumping = 1;
static uint32_t reader_count, touch_payload;
static uint64_t snap_start;
static uint32_t snap_slices;
static const char *snap_path;
static struct send_options sending;
static _Atomic uint64_t send_next;	/* the first completion no sender has taken */
static _Atomic uint32_t send_peak;	/* the largest magnitude reduced since the shifter last looked */
static int device_fd = -1;
static pthread_t shifter_tid;
static struct { uint32_t writes, errors, last, lowest, highest; } shifting;

static int usage(void)
{
	fprintf(stderr,
		"usage: mt7996-stream DEVICE --seconds S [--idle-ms N] [--go ADDR VALUE]\n"
		"       [--stop ADDR VALUE] [--threads N] [--touch] [--data BYTES FILE] [--wrap]\n"
		"       [--meta FILE] [--snap SLICES FILE] [--export IF SRC_IP DST_IP DST_MAC PORT]\n"
		"       [--ports N] [--drain-ms N] [--export-cpu N] [--cpu N]\n"
		"       [--send SRC_IP DST_IP PORT] [--format 16|14|8|7] [--piece BYTES]\n"
		"       [--datagram BYTES] [--senders N] [--split]\n");
	return 2;
}

static uint64_t completed(void)
{
	return __atomic_load_n(&ring->completed, __ATOMIC_ACQUIRE);
}

/* A completion's length is the radio's word for it, a 14-bit field: up to 16383, of a slice that
 * holds slice_bytes. Whoever reads the slice by it reads no further than the slice. */
static uint32_t slice_len_of(uint32_t ctrl)
{
	uint32_t n = HW_GET(WFDMA_DESC_CTRL_SD_LEN0, ctrl);

	return n < ring->slice_bytes ? n : ring->slice_bytes;
}

static uint32_t slice_len(uint64_t seq)
{
	return slice_len_of(ring->ctrl[seq % ring->slices]);
}

static const uint8_t *slice_at(uint64_t seq)
{
	return slices + (seq % ring->slices) * ring->slice_bytes;
}

/* Completions next, next + reader_count, ...: each read in place. */
static void *reader(void *arg)
{
	struct reader *r = arg;
	uint64_t c, s0, s1, behind = ring->slices - ring->slots;
	const uint64_t *w;
	uint32_t n, k;

	for (;;) {
		c = completed();
		if (r->next >= c) {
			if (!atomic_load(&pumping) && r->next >= completed())
				break;
			continue;
		}
		if (c - r->next > behind) {
			r->lapped++;
			r->next += reader_count;
			continue;
		}
		n = slice_len(r->next);
		if (touch_payload) {
			w = (const uint64_t *)slice_at(r->next);
			for (s0 = s1 = 0, k = 0; k + 1 < n / 8; k += 2) {
				s0 += w[k];
				s1 ^= w[k + 1];
			}
			r->checksum += s0 ^ s1;
			if (completed() - r->next > behind)	/* overwritten meanwhile */
				r->lapped++;
		}
		r->buffers++;
		r->bytes += n;
		r->full += n >= ring->slice_bytes / 2;
		r->next += reader_count;
	}
	return NULL;
}

static void send_pause(long ns)
{
	const struct timespec t = { .tv_nsec = ns };

	nanosleep(&t, NULL);
}

/* The batch to one socket; the next batch takes the next source port. A full
 * device queue refuses a datagram (IP_RECVERR): it is offered again.
 */
static void send_flush(struct sender *s, struct batch *b)
{
	int fd = s->fds[s->fd_next++ % s->fd_count], n;
	uint32_t at = 0;

	while (at < b->count) {
		n = sendmmsg(fd, b->msgs + at, b->count - at, 0);
		if (n > 0) {
			at += (uint32_t)n;
			s->datagrams += (uint64_t)n;
		} else if (errno == ENOBUFS || errno == EAGAIN) {
			s->busy++;
			send_pause(SEND_BUSY_NS);
		} else if (errno != EINTR) {
			s->errors++;	/* this datagram is not sent */
			at++;
		}
	}
	b->count = b->records = b->slices = b->open_bytes = 0;
}

/* One record: its header and `n` bytes; into the open datagram, or a new one. */
static void send_record(struct batch *b, uint32_t magic, uint64_t word, uint32_t offset,
			uint32_t slice_bytes, const uint8_t *data, uint32_t n, int join)
{
	uint32_t r = b->records++, size = (uint32_t)sizeof(b->head[r]) + n;
	struct msghdr *m;

	b->head[r] = (struct w1700k_sdr_export_header){
		.magic = htole32(magic), .offset = htole16((uint16_t)offset),
		.slice_bytes = htole16((uint16_t)slice_bytes), .seq = htole64(word),
	};
	b->iov[2 * r] = (struct iovec){ &b->head[r], sizeof(b->head[r]) };
	b->iov[2 * r + 1] = (struct iovec){ (void *)(uintptr_t)data, n };
	if (join && b->open_bytes && b->open_bytes + size <= sending.datagram) {
		b->msgs[b->count - 1].msg_hdr.msg_iovlen += 2;
		b->open_bytes += size;
		return;
	}
	memset(&b->msgs[b->count], 0, sizeof(b->msgs[0]));
	m = &b->msgs[b->count++].msg_hdr;
	m->msg_iov = &b->iov[2 * r];
	m->msg_iovlen = 2;	/* an empty second part is no harm */
	b->open_bytes = join ? size : 0;
}

/* `len` bytes of one completion into the batch: whole, or as pieces. */
static void send_add(struct batch *b, uint32_t magic, uint64_t word, const uint8_t *data, uint32_t len)
{
	uint32_t at = 0, n;

	if (!sending.piece || len <= sending.piece) {
		send_record(b, magic, word, 0, len, data, len, len != 0);
		return;
	}
	for (; at < len; at += n) {
		n = len - at < sending.piece ? len - at : sending.piece;
		send_record(b, magic, word, at, len, data + at, n, 0);
	}
}

/* Completion `seq` into the batch: reduced, or as it is. False: the ring
 * overwrote it meanwhile.
 */
static int send_slice(struct sender *s, struct batch *b, uint64_t seq, uint64_t behind,
		      uint32_t reduced_bytes)
{
	uint32_t n = slice_len(seq), code, peak, seen;
	const uint8_t *p = slice_at(seq);

	if (sending.split && n != IQ_REDUCE_SAMPLES * 4)
		return 1;	/* the driver's export sends what is not a raw slice */
	if (reduced_bytes && n == IQ_REDUCE_SAMPLES * 4) {
		code = iq_reduce_encode_peak(sending.format, (const uint32_t *)(const void *)p,
					     b->data[b->slices], &peak);
		if (completed() - seq > behind)
			return 0;
		for (seen = atomic_load(&send_peak); peak > seen;)
			if (atomic_compare_exchange_weak(&send_peak, &seen, peak))
				break;
		send_add(b, SEND_MAGIC_REDUCED,
			 seq | (uint64_t)sending.format << 48 | (uint64_t)code << 56,
			 b->data[b->slices], reduced_bytes);
		s->reduced++;
	} else {
		send_add(b, W1700K_SDR_EXPORT_MAGIC, seq, p, n);	/* read in place */
	}
	b->slices++;
	s->slices++;
	return 1;
}

/* One word of the firmware's mailbox, through the driver (the pump writes it
 * between sweeps). 0 or -1.
 */
static int mailbox_write(uint32_t address, uint32_t value)
{
	static struct w1700k_sdr_ctl c;	/* 800 bytes; one caller at a time */

	memset(&c, 0, sizeof(c));
	c.count = 1;
	c.timeout_ms = 100;
	c.address[0] = address;
	c.value[0] = value;
	return ioctl(device_fd, W1700K_SDR_CTL, &c) || c.result ? -1 : 0;
}

/* --split: the shift for the chunks the radio reduces, from the largest
 * magnitude the senders reduced in the last second (kept as the largest of
 * each 10 ms): it rises within a tick and falls only when a larger level has
 * left that second. Also moves the driver's export thread, once it exists,
 * off the pump's CPU.
 */
static void steer_threads(uint32_t cpu, int all);
static uint32_t pump_cpu_of;
static int pump_cpu_known;

static void *shifter(void *arg)
{
	uint32_t spans[SHIFT_SPANS] = { 0 }, span = 0, held, peak, shift, ticks = 0, k;
	int seen = 0;

	(void)arg;
	while (atomic_load(&pumping)) {
		send_pause(SHIFT_TICK_NS);
		ticks++;
		if (pump_cpu_known && (ticks == 5 || ticks == 50 || ticks == 500))
			steer_threads(pump_cpu_of, 0);
		peak = atomic_exchange(&send_peak, 0);
		if (!seen && !peak)
			continue;	/* nothing reduced yet: the shift stays where it cannot wrap */
		seen = 1;
		if (ticks % SHIFT_SPAN_TICKS == 0) {
			span = (span + 1) % SHIFT_SPANS;
			spans[span] = 0;
		}
		if (peak > spans[span])
			spans[span] = peak;
		for (held = 0, k = 0; k < SHIFT_SPANS; k++)
			held = spans[k] > held ? spans[k] : held;
		shift = iq_radio14_shift_for(held);
		if (shift == shifting.last && shifting.writes)
			continue;
		if (mailbox_write(MT7996_PROBE_WM_SHIFT, shift)) {
			shifting.errors++;
			continue;
		}
		shifting.writes++;
		shifting.last = shift;
		shifting.lowest = shift < shifting.lowest ? shift : shifting.lowest;
		shifting.highest = shift > shifting.highest ? shift : shifting.highest;
	}
	return NULL;
}

/* Takes the next SEND_CLAIM completions no sender has, waits for the device
 * to complete them, and sends them; whichever sender is free takes the next.
 * Lapped, the senders go on near the newest completion, so that they send
 * whole stretches again rather than race the device along the ring.
 */
static void *sender(void *arg)
{
	struct sender *s = arg;
	const uint64_t behind = ring->slices - ring->slots;
	const uint32_t reduced_bytes = iq_reduce_bytes(sending.format);
	const uint32_t pieces_max = sending.piece ? (ring->slice_bytes + sending.piece - 1) / sending.piece : 1;
	struct batch *b = aligned_alloc(64, (sizeof(*b) + 63) & ~(size_t)63);
	uint64_t c, seq, end, next;

	if (!b)
		return NULL;
	setpriority(PRIO_PROCESS, (id_t)syscall(SYS_gettid), NICE_STREAM);
	memset(b, 0, offsetof(struct batch, data));
	for (;;) {
		seq = atomic_fetch_add(&send_next, SEND_CLAIM);
		end = seq + SEND_CLAIM;
		while (seq < end) {
			c = completed();
			if (seq >= c) {
				if (b->count) {
					send_flush(s, b);
					continue;
				}
				if (!atomic_load(&pumping) && seq >= completed())
					goto out;
				send_pause(SEND_IDLE_NS);
				continue;
			}
			if (c - seq > behind) {
				/* The rest of this claim is lost, and what the others
				 * have not taken up to near the newest. */
				s->lapped += end - seq;
				next = atomic_load(&send_next);
				while (next + SEND_RESUME < c)
					if (atomic_compare_exchange_weak(&send_next, &next, c - SEND_RESUME)) {
						s->lapped += c - SEND_RESUME - next;	/* what nobody took */
						break;
					}
				break;
			}
			if (c - seq > s->max_lag)
				s->max_lag = (uint32_t)(c - seq);
			if (b->records + pieces_max > SEND_BATCH || b->slices == SEND_SLICES)
				send_flush(s, b);
			if (!send_slice(s, b, seq, behind, reduced_bytes))
				s->lapped++;
			seq++;
		}
	}
out:
	if (b->count)
		send_flush(s, b);
	free(b);
	return NULL;
}

/* How many CPUs the unit has. Not sysconf(): the C library the unit's programs
 * are built with counts the CPUs the calling thread may run on, which is one
 * for a thread pinned to its own (seen: the export thread and every network
 * thread confined to CPU 0 by a count taken on a pinned thread, and a 320 Msps
 * stream falling 0.8 s behind there).
 */
static long cpu_count(void)
{
	static long n;
	int first, last = -1;
	FILE *f;

	if (n)
		return n;
	f = fopen("/sys/devices/system/cpu/possible", "r");
	if (f) {
		if (fscanf(f, "%d-%d", &first, &last) == 1)
			last = first;
		fclose(f);
	}
	n = last >= 0 ? last + 1 : sysconf(_SC_NPROCESSORS_CONF);
	return n;
}

/* Every CPU but `cpu`; with `all`, every CPU. */
static void cpus_but(cpu_set_t *set, uint32_t cpu, int all)
{
	long n = cpu_count(), k;

	CPU_ZERO(set);
	for (k = 0; k < n && k < CPU_SETSIZE; k++)
		if (all || (uint32_t)k != cpu)
			CPU_SET(k, set);
}

/* Where the senders run beside a pump on `pump`: each on a CPU of its own,
 * counted down from the last one that is not the pump's, when that leaves a
 * CPU for the kernel's network threads (4 CPUs, 2 senders: CPUs 1 and 2, and
 * CPU 0 for the rest). A sender that shares its CPU loses it for milliseconds
 * at a time, and at 320 Msps it has little room to make them up. Returns the
 * sender's CPU, or -1: no CPU each.
 */
static int sender_cpu(uint32_t pump, uint32_t index, uint32_t senders)
{
	long n = cpu_count(), k, left = (long)index;

	if (n < (long)senders + 2)
		return -1;
	for (k = n - 1; k >= 0; k--)
		if ((uint32_t)k != pump && left-- == 0)
			return (int)k;
	return -1;
}

/* The network devices' NAPI threads (kernel threads named napi/...) and the
 * driver's export thread (sdr-export, when it is not bound to a CPU) off the
 * pump's CPU, or with `all` back on every CPU. Best effort.
 */
static void steer_threads(uint32_t cpu, int all)
{
	DIR *proc = opendir("/proc");
	struct dirent *e;
	char path[64], name[32];
	cpu_set_t set, wide;
	uint32_t k;
	FILE *f;

	if (!proc)
		return;
	cpus_but(&set, cpu, all);
	wide = set;
	for (k = 0; !all && sending.set && k < sending.senders; k++)	/* nor the senders' own CPUs */
		if (sender_cpu(cpu, k, sending.senders) >= 0)
			CPU_CLR(sender_cpu(cpu, k, sending.senders), &set);
	while ((e = readdir(proc))) {
		if (e->d_name[0] < '0' || e->d_name[0] > '9')
			continue;
		snprintf(path, sizeof(path), "/proc/%.20s/comm", e->d_name);
		f = fopen(path, "r");
		if (!f)
			continue;
		if (fgets(name, sizeof(name), f) &&
		    (!strncmp(name, "napi/", 5) || !strncmp(name, "sdr-export", 10))) {
			/* The export thread works in short bursts and must run when it wakes, or
			 * the radio's chunks reach the host late: it has priority and every CPU
			 * but the pump's, the senders' included (it costs them little). Left to
			 * the one CPU the network threads, the interrupts and everything else
			 * share, it fell half a second behind whenever that CPU was busy, and
			 * stayed there. */
			sched_setaffinity((pid_t)atoi(e->d_name), sizeof(set), name[0] == 's' ? &wide : &set);
			setpriority(PRIO_PROCESS, (id_t)atoi(e->d_name),
				    all ? 0 : name[0] == 's' ? NICE_FIRST : NICE_STREAM);
		}
		fclose(f);
	}
	closedir(proc);
}

/* Sender `index`'s sockets: every senders-th source port from PORT + index. */
static int send_open(struct sender *s, uint32_t index)
{
	struct sockaddr_in from = { .sin_family = AF_INET, .sin_addr = sending.src };
	struct sockaddr_in to = { .sin_family = AF_INET, .sin_addr = sending.dst,
				  .sin_port = htons((uint16_t)sending.port) };
	int one = 1, pmtu = IP_PMTUDISC_DO, sndbuf = SEND_SNDBUF, fd;
	uint32_t port;

	for (port = index; port < sending.ports || !s->fd_count; port += sending.senders) {
		if (s->fd_count == SEND_PORTS_MAX || sending.port + port > 0xffff)
			break;
		fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
		if (fd < 0)
			return -1;
		from.sin_port = htons((uint16_t)(sending.port + port));
		setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));
		/* No fragments: a slice too long for the path is an error (--piece). */
		if (setsockopt(fd, IPPROTO_IP, IP_MTU_DISCOVER, &pmtu, sizeof(pmtu)) ||
		    setsockopt(fd, IPPROTO_IP, IP_RECVERR, &one, sizeof(one)) ||
		    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) ||
		    bind(fd, (struct sockaddr *)&from, sizeof(from)) ||
		    connect(fd, (struct sockaddr *)&to, sizeof(to))) {
			close(fd);
			return -1;
		}
		s->fds[s->fd_count++] = fd;
	}
	return s->fd_count ? 0 : -1;
}

static int snap_once(const char *tmp, const char *meta, uint32_t taken)
{
	uint64_t c = completed(), from, seq;
	FILE *f;

	if (c < snap_start + snap_slices + SNAP_MARGIN)
		return 0;
	from = c - SNAP_MARGIN - snap_slices;
	f = fopen(tmp, "wb");
	if (!f)
		return 0;
	for (seq = from; seq < from + snap_slices; seq++)
		fwrite(slice_at(seq), 1, slice_len(seq), f);
	fclose(f);
	if (completed() - from > ring->slices - ring->slots)	/* lapped: discard */
		return 0;
	if (rename(tmp, snap_path))
		return 0;
	f = fopen(meta, "w");
	if (f) {
		fprintf(f, "{\"seq\":%" PRIu64 ",\"chunk\":%" PRIu64 ",\"slices\":%" PRIu32
			",\"taken\":%" PRIu32 "}\n", from, from - snap_start, snap_slices, taken);
		fclose(f);
	}
	return 1;
}

/* SIGUSR1 is blocked everywhere else; this thread takes it. */
static void *snapper(void *arg)
{
	sigset_t *set = arg;
	char tmp[512], meta[512];
	uint32_t taken = 0;
	int sig;

	snprintf(tmp, sizeof(tmp), "%s.tmp", snap_path);
	snprintf(meta, sizeof(meta), "%s.json", snap_path);
	for (;;)
		if (!sigwait(set, &sig))
			taken += snap_once(tmp, meta, taken + 1);
	return NULL;
}

static int dump(const struct options *o, uint64_t from, uint64_t end)
{
	FILE *d = o->data_path ? fopen(o->data_path, "wb") : NULL;
	FILE *m = o->meta_path ? fopen(o->meta_path, "wb") : NULL;
	uint64_t seq, written = 0;
	uint32_t ctrl, n;
	int ret = 0;

	if ((o->data_path && !d) || (o->meta_path && !m))
		ret = -1;
	for (seq = from; !ret && seq < end; seq++) {
		ctrl = ring->ctrl[seq % ring->slices];
		n = slice_len_of(ctrl);	/* the descriptor is kept as it came; the data is the slice's */
		if (d && written + n <= o->data_cap) {
			if (fwrite(slice_at(seq), 1, n, d) != n)
				ret = -1;
			written += n;
		}
		if (m && fwrite(&ctrl, sizeof(ctrl), 1, m) != 1)
			ret = -1;
	}
	if (d && fclose(d))
		ret = -1;
	if (m && fclose(m))
		ret = -1;
	return ret;
}

/* IF SRC_IP DST_IP DST_MAC PORT */
static int export_target(char **argv, struct w1700k_sdr_stream_export *e)
{
	struct in_addr src, dst;
	unsigned int mac[6];
	uint32_t port;
	int k;

	e->ifindex = if_nametoindex(argv[0]);
	if (!e->ifindex || inet_pton(AF_INET, argv[1], &src) != 1 ||
	    inet_pton(AF_INET, argv[2], &dst) != 1 ||
	    sscanf(argv[3], "%x:%x:%x:%x:%x:%x", &mac[0], &mac[1], &mac[2], &mac[3],
		   &mac[4], &mac[5]) != 6 ||
	    client_u32(argv[4], &port) || !port || port > 0xffff)
		return -1;
	e->src_ip = src.s_addr;
	e->dst_ip = dst.s_addr;
	e->src_port = htons((uint16_t)port);
	e->dst_port = htons((uint16_t)port);
	for (k = 0; k < 6; k++)
		e->dst_mac[k] = (uint8_t)mac[k];
	return 0;
}

static int parse(int argc, char **argv, struct options *o)
{
	struct w1700k_sdr_stream_rx *req = &o->req;
	uint32_t value;
	int i, left;

	o->threads = 2;
	for (i = 2; i < argc; i++) {
		const char *a = argv[i];

		left = argc - i - 1;
		if (!strcmp(a, "--seconds") && left >= 1) {
			o->seconds = strtod(argv[++i], NULL);
		} else if (!strcmp(a, "--idle-ms") && left >= 1) {
			if (client_u32(argv[++i], &req->idle_ms))
				return -1;
		} else if (!strcmp(a, "--go") && left >= 2) {
			if (client_u32(argv[i + 1], &req->go_address) ||
			    client_u32(argv[i + 2], &req->go_value))
				return -1;
			i += 2;
		} else if (!strcmp(a, "--stop") && left >= 2) {
			if (client_u32(argv[i + 1], &req->stop_address) ||
			    client_u32(argv[i + 2], &req->stop_value))
				return -1;
			i += 2;
		} else if (!strcmp(a, "--threads") && left >= 1) {
			if (client_u32(argv[++i], &o->threads) || o->threads > THREADS_MAX)
				return -1;
		} else if (!strcmp(a, "--touch")) {
			o->touch = 1;
		} else if (!strcmp(a, "--data") && left >= 2) {
			if (client_u32(argv[++i], &o->data_cap))
				return -1;
			o->data_path = argv[++i];
		} else if (!strcmp(a, "--wrap")) {
			o->wrap = 1;
		} else if (!strcmp(a, "--meta") && left >= 1) {
			o->meta_path = argv[++i];
		} else if (!strcmp(a, "--snap") && left >= 2) {
			if (client_u32(argv[++i], &o->snap_slices) || !o->snap_slices ||
			    o->snap_slices > 4096)
				return -1;
			o->snap_path = argv[++i];
		} else if (!strcmp(a, "--export") && left >= 5) {
			if (export_target(argv + i + 1, &req->export))
				return -1;
			i += 5;
		} else if (!strcmp(a, "--ports") && left >= 1) {
			if (client_u32(argv[++i], &o->ports) || !o->ports || o->ports > 256)
				return -1;
		} else if (!strcmp(a, "--send") && left >= 3) {
			if (inet_pton(AF_INET, argv[i + 1], &o->send.src) != 1 ||
			    inet_pton(AF_INET, argv[i + 2], &o->send.dst) != 1 ||
			    client_u32(argv[i + 3], &o->send.port) || !o->send.port ||
			    o->send.port > 0xffff)
				return -1;
			o->send.set = 1;
			i += 3;
		} else if (!strcmp(a, "--format") && left >= 1) {
			if (client_u32(argv[++i], &o->send.format) || !iq_reduce_bytes(o->send.format))
				return -1;
		} else if (!strcmp(a, "--piece") && left >= 1) {
			if (client_u32(argv[++i], &o->send.piece) || o->send.piece < SEND_PIECE_MIN ||
			    o->send.piece > 8192)
				return -1;
		} else if (!strcmp(a, "--datagram") && left >= 1) {
			if (client_u32(argv[++i], &o->send.datagram) || o->send.datagram > 65507)
				return -1;
		} else if (!strcmp(a, "--split")) {
			o->send.split = 1;
		} else if (!strcmp(a, "--senders") && left >= 1) {
			if (client_u32(argv[++i], &o->send.senders) || !o->send.senders ||
			    o->send.senders > SENDERS_MAX)
				return -1;
		} else if (!strcmp(a, "--drain-ms") && left >= 1) {
			if (client_u32(argv[++i], &req->export.drain_ms) ||
			    req->export.drain_ms > W1700K_SDR_EXPORT_DRAIN_MAX)
				return -1;
			o->export_set = 1;
		} else if (!strcmp(a, "--export-cpu") && left >= 1) {
			if (client_u32(argv[++i], &value) || value > 63)
				return -1;
			req->export.cpu = (uint16_t)(value + 1);
			o->export_set = 1;
		} else if (!strcmp(a, "--cpu") && left >= 1) {
			if (client_u32(argv[++i], &o->pump_cpu) || o->pump_cpu > 63)
				return -1;
			o->cpu_set = 1;
		} else {
			return -1;
		}
	}
	if (o->export_set && !req->export.ifindex)
		return -1;
	/* Either the driver's export or the senders carry the stream; --split has both, the
	 * export the short completions and the senders the raw ones at 14 bits. */
	if (o->send.split ? !o->send.set || !req->export.ifindex || o->send.format != 14 || o->send.piece
			  : o->send.set ? req->export.ifindex != 0
					: o->send.format || o->send.piece || o->send.datagram || o->send.senders)
		return -1;
	if (o->send.split)
		req->export.flags |= W1700K_SDR_EXPORT_SHORT;
	if (o->ports && !req->export.ifindex && !o->send.set)
		return -1;
	req->export.src_ports = req->export.ifindex ? (uint16_t)o->ports : 0;
	o->send.ports = o->ports ? o->ports : 1;
	if (!o->send.senders)
		o->send.senders = 2;
	if (!(o->seconds > 0 && o->seconds * 1000 <= W1700K_SDR_STREAM_TIMEOUT_MAX))
		return -1;
	req->timeout_ms = (uint32_t)(o->seconds * 1000);
	return 0;
}

static void on_signal(int sig)
{
	(void)sig;	/* interrupts the pump, which then stops the producer */
}

/*
 * The stop signals must reach the pump thread, so the helper threads are
 * created with them blocked. SIGUSR1 stays blocked everywhere when snapping;
 * the snapper takes it with sigwait.
 */
static int start_threads(const struct options *o, struct reader *readers, struct sender *senders,
			 uint64_t start)
{
	static sigset_t usr1;
	sigset_t stops;
	pthread_t tid;
	uint32_t i;

	sigemptyset(&stops);
	sigaddset(&stops, SIGINT);
	sigaddset(&stops, SIGTERM);
	sigaddset(&stops, SIGHUP);
	sigemptyset(&usr1);
	sigaddset(&usr1, SIGUSR1);
	if (pthread_sigmask(SIG_BLOCK, &stops, NULL))
		return -1;
	if (o->snap_path && (pthread_sigmask(SIG_BLOCK, &usr1, NULL) ||
			     pthread_create(&tid, NULL, snapper, &usr1)))
		return -1;
	for (i = 0; i < o->threads; i++) {
		readers[i] = (struct reader){ .next = start + i };
		if (pthread_create(&readers[i].tid, NULL, reader, &readers[i]))
			return -1;
	}
	for (i = 0; o->send.set && i < o->send.senders; i++) {
		cpu_set_t set;

		if (pthread_create(&senders[i].tid, NULL, sender, &senders[i]))
			return -1;
		if (o->cpu_set) {
			cpus_but(&set, o->pump_cpu, 0);
			if (sender_cpu(o->pump_cpu, i, o->send.senders) >= 0) {
				CPU_ZERO(&set);
				CPU_SET(sender_cpu(o->pump_cpu, i, o->send.senders), &set);
			}
			pthread_setaffinity_np(senders[i].tid, sizeof(set), &set);
		}
	}
	if (o->send.split) {
		cpu_set_t set;

		if (pthread_create(&shifter_tid, NULL, shifter, NULL))
			return -1;
		if (o->cpu_set) {
			cpus_but(&set, o->pump_cpu, 0);
			for (i = 0; i < o->send.senders; i++)
				if (sender_cpu(o->pump_cpu, i, o->send.senders) >= 0)
					CPU_CLR(sender_cpu(o->pump_cpu, i, o->send.senders), &set);
			pthread_setaffinity_np(shifter_tid, sizeof(set), &set);
		}
	}
	return pthread_sigmask(SIG_UNBLOCK, &stops, NULL) ? -1 : 0;
}

static int pin_pump(uint32_t cpu)
{
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	return sched_setaffinity(0, sizeof(set), &set);
}

/* The completion window the dump covers. */
static uint64_t dump_from(const struct options *o, const struct w1700k_sdr_stream_rx *r,
			  uint64_t end)
{
	uint64_t held = (uint64_t)r->slices - r->slots;
	uint64_t kept = end - r->first_seq < held ? r->first_seq : end - held;
	uint64_t half = o->data_cap / r->slice_bytes / 2, wrap;

	if (!o->wrap)
		return kept;
	wrap = end / r->slices * r->slices;
	if (wrap < half || wrap - half < kept || wrap >= end)
		return kept;
	return wrap - half;
}

int main(int argc, char **argv)
{
	static struct options o;
	struct w1700k_sdr_stream_rx *req = &o.req, geometry;
	struct sigaction sa = { .sa_handler = on_signal };
	struct reader readers[THREADS_MAX];
	static struct sender senders[SENDERS_MAX];
	struct sender sent = { 0 };
	uint64_t start, end, from, buffers = 0, bytes = 0, full = 0, lapped = 0, checksum = 0;
	size_t map_bytes;
	uint32_t i;
	void *map;
	int fd, rc, err = 0, dumped = 0;

	if (argc < 2 || parse(argc, argv, &o))
		return usage();
	reader_count = o.threads;
	touch_payload = o.touch;
	snap_slices = o.snap_slices;
	snap_path = o.snap_path;
	sending = o.send;
	fd = client_open(argv[1], O_RDWR, W1700K_SDR_CAP_STREAM_RX);
	if (fd < 0)
		return 1;
	memset(&geometry, 0, sizeof(geometry));	/* timeout 0: geometry only */
	if (ioctl(fd, W1700K_SDR_STREAM_RX, &geometry) || !geometry.slices)
		return client_fail("STREAM_RX geometry");
	map_bytes = geometry.header_bytes + (size_t)geometry.slices * geometry.slice_bytes;
	map = mmap(NULL, map_bytes, PROT_READ, MAP_SHARED, fd, 0);
	if (map == MAP_FAILED)
		return client_fail("mmap");
	ring = map;
	slices = (const uint8_t *)map + geometry.header_bytes;
	/* No SA_RESTART: a stop signal only interrupts the pump. */
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGHUP, &sa, NULL);
	for (i = 0; o.send.set && i < o.send.senders; i++)
		if (send_open(&senders[i], i))
			return client_fail("--send socket");
	device_fd = fd;
	pump_cpu_of = o.pump_cpu;
	pump_cpu_known = o.cpu_set;
	if (o.send.split) {
		/* Until the senders have seen the signal: the shift that cannot wrap. */
		shifting.last = shifting.lowest = shifting.highest = IQ_RADIO14_SHIFT_MAX;
		if (mailbox_write(MT7996_PROBE_WM_SHIFT, shifting.last))
			return client_fail("the shift word");
		shifting.writes = 1;
	}
	start = snap_start = completed();
	atomic_store(&send_next, start);
	if (start_threads(&o, readers, senders, start))
		return client_fail("pthread_create");
	if (o.cpu_set && pin_pump(o.pump_cpu))
		return client_fail("sched_setaffinity");
	if (o.cpu_set && o.send.set) {
		steer_threads(o.pump_cpu, 0);
		setpriority(PRIO_PROCESS, 0, NICE_FIRST);	/* this thread: the pump */
	}
	rc = ioctl(fd, W1700K_SDR_STREAM_RX, req);
	if (rc)
		err = errno;
	if (rc && err == EINVAL && o.send.split)
		fprintf(stderr, "the driver refused --split: install the module of this build "
			"(W1700K_SDR_EXPORT_SHORT)\n");
	atomic_store(&pumping, 0);
	if (o.cpu_set && o.send.set)
		steer_threads(o.pump_cpu, 1);
	if (o.send.split)
		pthread_join(shifter_tid, NULL);
	for (i = 0; i < o.threads; i++) {
		pthread_join(readers[i].tid, NULL);
		buffers += readers[i].buffers;
		bytes += readers[i].bytes;
		full += readers[i].full;
		lapped += readers[i].lapped;
		checksum += readers[i].checksum;
	}
	for (i = 0; o.send.set && i < o.send.senders; i++) {
		pthread_join(senders[i].tid, NULL);
		sent.slices += senders[i].slices;
		sent.reduced += senders[i].reduced;
		sent.datagrams += senders[i].datagrams;
		sent.lapped += senders[i].lapped;
		sent.errors += senders[i].errors;
		sent.busy += senders[i].busy;
		sent.max_lag = senders[i].max_lag > sent.max_lag ? senders[i].max_lag : sent.max_lag;
	}
	end = req->first_seq + req->buffers;
	from = dump_from(&o, req, end);
	if (o.data_path || o.meta_path)
		dumped = dump(&o, from, end) ? -1 : 1;
	printf("{\"at\":\"stream\",\"errno\":%d,\"result\":%" PRId32 ",\"buffers\":%" PRIu64
	       ",\"malformed\":%" PRIu32 ",\"bytes\":%" PRIu64 ",\"full_buffers\":%" PRIu64
	       ",\"first_seq\":%" PRIu64 ",\"first_ns\":%" PRIu64 ",\"last_ns\":%" PRIu64
	       ",\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64 ",\"stop_ns\":%" PRIu64
	       ",\"max_backlog\":%" PRIu32 ",\"slots\":%" PRIu32 ",\"slices\":%" PRIu32
	       ",\"slice_bytes\":%" PRIu32 ",\"readers\":%" PRIu32 ",\"touch\":%d"
	       ",\"read_buffers\":%" PRIu64 ",\"read_bytes\":%" PRIu64 ",\"read_full\":%" PRIu64
	       ",\"lapped\":%" PRIu64 ",\"checksum\":\"%016" PRIx64 "\",\"start_seq\":%" PRIu64
	       ",\"dump_from_seq\":%" PRIu64 ",\"dumped\":%d"
	       ",\"export\":{\"result\":%" PRId32 ",\"slices\":%" PRIu64 ",\"busy\":%" PRIu64
	       ",\"unsent\":%" PRIu64 ",\"last_ns\":%" PRIu64 ",\"max_lag\":%" PRIu32
	       ",\"inflight_max\":%" PRIu32 "}"
	       ",\"send\":{\"senders\":%" PRIu32 ",\"format\":%" PRIu32 ",\"piece\":%" PRIu32
	       ",\"datagram\":%" PRIu32
	       ",\"slices\":%" PRIu64 ",\"reduced\":%" PRIu64 ",\"datagrams\":%" PRIu64
	       ",\"lapped\":%" PRIu64 ",\"errors\":%" PRIu64 ",\"busy\":%" PRIu64 ",\"max_lag\":%" PRIu32 "}"
	       ",\"split\":{\"on\":%d,\"shift_writes\":%" PRIu32 ",\"shift_errors\":%" PRIu32
	       ",\"shift_last\":%" PRIu32 ",\"shift_lowest\":%" PRIu32 ",\"shift_highest\":%" PRIu32 "}}\n",
	       err, req->result, (uint64_t)req->buffers, req->malformed, (uint64_t)req->bytes,
	       (uint64_t)req->full_buffers, (uint64_t)req->first_seq, (uint64_t)req->first_ns,
	       (uint64_t)req->last_ns, (uint64_t)req->start_ns, (uint64_t)req->end_ns,
	       (uint64_t)req->stop_ns, req->max_backlog, req->slots, req->slices,
	       req->slice_bytes, o.threads, o.touch, buffers, bytes, full, lapped, checksum,
	       start, from, dumped, req->export_result, (uint64_t)req->export_slices,
	       (uint64_t)req->export_busy, (uint64_t)req->export_unsent,
	       (uint64_t)req->export_last_ns, req->export_max_lag, req->export_inflight_max,
	       o.send.set ? o.send.senders : 0, o.send.format, o.send.piece, o.send.datagram,
	       sent.slices,
	       sent.reduced, sent.datagrams, sent.lapped, sent.errors, sent.busy, sent.max_lag,
	       o.send.split, shifting.writes, shifting.errors, shifting.last, shifting.lowest,
	       shifting.highest);
	munmap(map, map_bytes);
	close(fd);
	return err || dumped < 0 || fflush(stdout) ? 1 : 0;
}
