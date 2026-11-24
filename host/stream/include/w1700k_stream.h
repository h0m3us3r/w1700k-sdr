/* SPDX-License-Identifier: MIT */
/*
 * The W1700K's IQ stream as every consumer on the host reads it: the shared memory of
 * w1700k-stream (host/stream). One POSIX shared-memory object, by default /dev/shm/w1700k-stream,
 * mapped by the service and by each consumer. Native byte order and alignment; every word shared
 * between processes is read and written with the __atomic builtins, from C and C++ alike.
 *
 * Layout: the header (4 KiB), then one 64-bit tag per slot, then the samples: per slot one slice
 * of W1700K_STREAM_SLICE complex samples, int16 I then int16 Q, on the scale of the radio's 14-bit
 * samples (full scale W1700K_STREAM_FULL_SCALE). The int16 range above it is headroom for the
 * correction filter's gain; a sample beyond it saturates and is counted.
 *
 * Slot `index & (ring_slices - 1)` holds slice `index` of run `run` (a run is one stream, numbered
 * from 1; `index` counts the unit's DMA completions from the run's first, 0..). Its tag is
 * w1700k_stream_tag(run, index), with W1700K_STREAM_TAG_EVENT for a firmware event (no samples and
 * no sample time: the slices either side are contiguous), W1700K_STREAM_TAG_LOST for a completion
 * that never arrived (no samples; the sample time is lost), and W1700K_STREAM_TAG_FLAT on a slice
 * the correction filter produced. A reader reads the tag (acquire), the samples, then the tag
 * again: a changed tag means the service overwrote the slot meanwhile (a seqlock).
 *
 * The service publishes slots in index order. `newest` is the frontier: every slot of the run
 * below it is final. Read run and newest together with w1700k_stream_position().
 *
 * Channels (version 2). A run is of one channel or of two: two receive chains of one band,
 * sampled at the same instants (the radio's two capture paths). A run's channels do not change;
 * read them with the run, w1700k_stream_layout(). A slot of a two-channel run holds
 * W1700K_STREAM_SLICE / 2 sample times, laid out by channel: channel 0's samples, then channel
 * 1's, sample k of each taken at the same instant (w1700k_stream_channel()). One tag covers the
 * slot, so an event or a loss is the same for both channels. A one-channel run is laid out as
 * in version 1. A reader that knows only one channel must refuse two (it would read two signals
 * end to end as one), which is why the version changed.
 */
#ifndef W1700K_STREAM_H
#define W1700K_STREAM_H

#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define W1700K_STREAM_NAME		"/w1700k-stream"
#define W1700K_STREAM_MAGIC		0x4d525453u	/* "STRM" */
#define W1700K_STREAM_VERSION		2u
#define W1700K_STREAM_MAX_CHANNELS	2u
#define W1700K_STREAM_SLICE		2048u		/* complex samples per slot */
#define W1700K_STREAM_FULL_SCALE	8192		/* the radio's 14-bit full scale */
#define W1700K_STREAM_HEADER_BYTES	4096u

#define W1700K_STREAM_TAG_EVENT		(1ull << 63)
#define W1700K_STREAM_TAG_LOST		(1ull << 62)
#define W1700K_STREAM_TAG_FLAT		(1ull << 61)
#define W1700K_STREAM_TAG_KIND		(W1700K_STREAM_TAG_EVENT | W1700K_STREAM_TAG_LOST | W1700K_STREAM_TAG_FLAT)

struct w1700k_stream_header {
	/* layout, fixed at creation */
	uint32_t magic, version;
	uint32_t header_bytes, slice_samples;
	uint64_t ring_slices;		/* a power of two */
	uint64_t tags_offset, samples_offset, total_bytes;
	uint64_t service_pid;
	uint64_t heartbeat_ns;		/* CLOCK_MONOTONIC, refreshed about every 100 ms */
	uint64_t receive_cpu;		/* 1 + the CPU that takes every datagram of the stream's interface,
					 * where one CPU does (an adapter with one receive queue): a
					 * consumer does well to keep its threads off it, as the service
					 * keeps its own; 0: none */
	uint64_t reserved0[7];

	/* the published position */
	uint64_t position_seq;		/* odd while the service writes run and newest */
	uint64_t run;			/* 1..; 0: no stream yet */
	uint64_t newest;		/* slots of the run below this are final */
	uint64_t run_started_ns, last_any_ns;	/* CLOCK_MONOTONIC */
	uint64_t received;		/* the newest completion received + 1 (ahead of newest) */
	uint64_t run_channels;		/* the run's channels, 1 or 2; written with run and newest */
	uint64_t reserved1[9];

	/* counters since the service started */
	uint64_t datagrams, bytes;
	uint64_t slices, packed, events, lost;	/* completions filed: samples (packed among them), events, lost */
	/* pieces: slices sent as several datagrams of which not all arrived. bad: datagrams that are
	 * not the stream's: a wrong magic or length, an index far ahead of where the stream is, or
	 * (with --unit) another sender */
	uint64_t pieces, bad, late, before_start, duplicates, socket_drops;
	/* the export's integrity: raw slices with spare bits set (words), and slices or 32-byte lines
	 * equal to the same slice or line one lap (128 completions) earlier: a capture or packer
	 * that repeated old data */
	uint64_t spare_words, stale_slices, stale_lines;
	uint64_t flattened, saturated;	/* slices the correction produced; samples clipped to int16 */
	uint64_t overruns;		/* of the lost: arrived, but overwritten in the service's receive
					 * ring before it was published (the service fell behind) */
	uint64_t reduced;		/* of the slices: sent with fewer bits than 14 per component */
	uint64_t suspect;		/* of a split stream's radio chunks: beside a slice louder than their
					 * shift holds, so samples in them may have wrapped (counted, not dropped) */
	uint64_t reserved2[10];

	/* control: written by consumers, read by the service */
	uint64_t restart_requests;	/* +1: the next completion starts a new run */
	uint64_t flatten;		/* 1: correct the decimation's roll-off where it applies */
	uint64_t radio_sample_rate;	/* samples/s; 0: unknown */
	uint64_t radio_capture_rate;	/* samples/s before the stream (640/2: 640e6); 0: unknown */
	uint64_t radio_filter_hz;	/* the analog filter's width; 0: unknown */
	uint64_t radio_channels;	/* 2: the stream is of two channels (0, 1: of one). Whoever starts a
					 * stream writes it first; a change makes the next completion start
					 * a new run, so no run is of both. radio_sample_rate is a channel's */
	uint64_t reserved3[10];

	/* the service's view of the control */
	uint64_t flatten_active;	/* the correction applies to the slices being published */
	uint64_t flatten_rate;		/* the sample rate whose correction applies; 0: none */
	uint64_t reserved4[14];
};

#define w1700k_stream_load(p)		__atomic_load_n((p), __ATOMIC_ACQUIRE)
#define w1700k_stream_store(p, v)	__atomic_store_n((p), (v), __ATOMIC_RELEASE)

static inline uint64_t w1700k_stream_tag(uint64_t run, uint64_t index)
{
	return (run << 40) | (index + 1);
}

static inline uint64_t *w1700k_stream_tags(const struct w1700k_stream_header *h)
{
	return (uint64_t *)((char *)h + h->tags_offset);
}

static inline int16_t *w1700k_stream_slice(const struct w1700k_stream_header *h, uint64_t index)
{
	return (int16_t *)((char *)h + h->samples_offset) +
	       (size_t)(index & (h->ring_slices - 1)) * W1700K_STREAM_SLICE * 2;
}

/* run and newest, read consistently. */
static inline void w1700k_stream_position(const struct w1700k_stream_header *h, uint64_t *run, uint64_t *newest)
{
	uint64_t s0, s1;

	do {
		s0 = w1700k_stream_load(&h->position_seq);
		*run = w1700k_stream_load(&h->run);
		*newest = w1700k_stream_load(&h->newest);
		s1 = w1700k_stream_load(&h->position_seq);
	} while ((s0 & 1) || s0 != s1);
}

/* run, newest and the run's channels (1 or 2), read consistently. */
static inline void w1700k_stream_layout(const struct w1700k_stream_header *h, uint64_t *run, uint64_t *newest,
					unsigned *channels)
{
	uint64_t s0, s1, c;

	do {
		s0 = w1700k_stream_load(&h->position_seq);
		*run = w1700k_stream_load(&h->run);
		*newest = w1700k_stream_load(&h->newest);
		c = w1700k_stream_load(&h->run_channels);
		s1 = w1700k_stream_load(&h->position_seq);
	} while ((s0 & 1) || s0 != s1);
	*channels = c == 2 ? 2 : 1;
}

/* Sample times in a slot of a run of `channels` channels. */
static inline unsigned w1700k_stream_slice_samples(unsigned channels)
{
	return W1700K_STREAM_SLICE / (channels == 2 ? 2 : 1);
}

/* Channel `channel`'s samples in slot `index` of a run of `channels` channels: int16 I, Q pairs,
 * w1700k_stream_slice_samples(channels) of them. */
static inline int16_t *w1700k_stream_channel(const struct w1700k_stream_header *h, uint64_t index,
					     unsigned channel, unsigned channels)
{
	return w1700k_stream_slice(h, index) + (size_t)channel * w1700k_stream_slice_samples(channels) * 2;
}

static inline uint64_t w1700k_stream_now_ns(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
}

/* The service is up: it refreshed its heartbeat within the last second. */
static inline int w1700k_stream_alive(const struct w1700k_stream_header *h)
{
	return w1700k_stream_now_ns() - w1700k_stream_load(&h->heartbeat_ns) < 1000000000ull;
}

/* The ring a header describes lies inside an object of `size` bytes: a power of two slices, the
 * tags after the header and before the samples, the samples to the end at most. */
static inline int w1700k_stream_layout_ok(uint64_t slices, uint64_t tags, uint64_t samples, uint64_t size)
{
	return slices && !(slices & (slices - 1)) && slices <= size / (W1700K_STREAM_SLICE * 4) &&
	       !(tags & 7) && !(samples & 1) && tags >= W1700K_STREAM_HEADER_BYTES && tags <= samples &&
	       slices * 8 <= samples - tags && samples <= size &&
	       slices * (W1700K_STREAM_SLICE * 4) <= size - samples;
}

/*
 * Maps the service's shared memory (name NULL: W1700K_STREAM_NAME); `writable` for a consumer that
 * writes the control words. Returns NULL with errno set: ENOENT while no service has created it,
 * EPROTO for another layout, EACCES for an object that is not the caller's own. *bytes receives the
 * mapping's length (for munmap), *inode the object's inode (a restarted service creates a new one).
 *
 * The name is one any user of the host can create an object under. What is read from the object
 * is trusted (the samples are taken for the radio's, the offsets are used to index it), so it has
 * to be this user's and writable by nobody else, as the service creates it (0600): then nothing
 * but this user's own programs wrote the layout that the accessors above read. The layout is
 * checked against the object's size all the same, once, here.
 */
static inline struct w1700k_stream_header *w1700k_stream_open(const char *name, int writable, size_t *bytes,
							      uint64_t *inode)
{
	struct w1700k_stream_header *h;
	struct stat st;
	int fd = shm_open(name ? name : W1700K_STREAM_NAME, writable ? O_RDWR : O_RDONLY, 0);

	if (fd < 0)
		return NULL;
	if (fstat(fd, &st)) {
		close(fd);
		return NULL;
	}
	if (st.st_uid != geteuid() || (st.st_mode & 022)) {
		close(fd);
		errno = EACCES;
		return NULL;
	}
	if ((size_t)st.st_size < W1700K_STREAM_HEADER_BYTES) {
		close(fd);
		errno = EPROTO;
		return NULL;
	}
	h = (struct w1700k_stream_header *)mmap(NULL, (size_t)st.st_size,
						writable ? PROT_READ | PROT_WRITE : PROT_READ, MAP_SHARED, fd, 0);
	close(fd);
	if (h == MAP_FAILED)
		return NULL;
	if (h->magic != W1700K_STREAM_MAGIC || h->version != W1700K_STREAM_VERSION ||
	    h->slice_samples != W1700K_STREAM_SLICE || h->total_bytes != (uint64_t)st.st_size ||
	    !w1700k_stream_layout_ok(h->ring_slices, h->tags_offset, h->samples_offset, (uint64_t)st.st_size)) {
		munmap(h, (size_t)st.st_size);
		errno = EPROTO;
		return NULL;
	}
	*bytes = (size_t)st.st_size;
	if (inode)
		*inode = (uint64_t)st.st_ino;
	return h;
}

#endif /* W1700K_STREAM_H */
