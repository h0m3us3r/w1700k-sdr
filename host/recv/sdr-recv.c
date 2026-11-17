// SPDX-License-Identifier: GPL-2.0-only
/* sdr-recv: records the W1700K's IQ stream as w1700k-stream serves it to every consumer on the
 * host (host/stream, include/w1700k_stream.h): complex int16 on the 14-bit scale, corrected for
 * the decimation's roll-off where the service applies it.
 *
 * It follows one run (stream) in slot order: from its first slice when started before the run,
 * else from the newest. Every slice becomes 2048 samples of the recording; a lost one is recorded
 * as zeros so that sample time stays true, and a firmware event (no samples, no sample time) is
 * left out. A slot the ring overwrote before it was read counts as an overrun (zeros too). It
 * stops after --seconds of wall time from the first slice, when the run ends (a new one begins),
 * after --idle-ms without a new slice, or when --capacity-mib is full. With --out, PATH holds the
 * samples and PATH.json what they are; a JSON line goes to stdout every --report seconds and at
 * the end.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "w1700k_stream.h"

#define EXAMPLES 64

static volatile sig_atomic_t stop;

static void on_signal(int sig)
{
	(void)sig;
	stop = 1;
}

static void usage(void)
{
	fprintf(stderr,
		"usage: sdr-recv [options]\n"
		"  --stream NAME       w1700k-stream's shared memory (default " W1700K_STREAM_NAME ")\n"
		"  --seconds S         stop S seconds after the first slice (default 10)\n"
		"  --idle-ms I         stop after I ms without a new slice (default 1000)\n"
		"  --wait S            give up if no stream comes in S seconds (default 120)\n"
		"  --out PATH          keep the samples: PATH and PATH.json (default: count only)\n"
		"  --capacity-mib M    memory for the recording, allocated up front (default 4096)\n"
		"  --report S          a progress line every S seconds (default 10)\n");
}

static double number(const char *option, const char *text)
{
	char *end;
	double v = strtod(text, &end);

	if (!*text || *end || !(v > 0)) {
		fprintf(stderr, "sdr-recv: %s needs a positive number, not \"%s\"\n", option, text);
		exit(2);
	}
	return v;
}

struct tally {
	uint64_t run, first, slices, samples, events, lost, overruns, flattened;
	uint64_t lost_ex[EXAMPLES], overrun_ex[EXAMPLES];
	unsigned n_lost_ex, n_overrun_ex;
	const char *ended;
};

static void print_tally(FILE *f, const struct tally *t, double seconds, const char *out)
{
	unsigned i;

	fprintf(f, "{\"run\":%" PRIu64 ",\"first_slice\":%" PRIu64 ",\"slices\":%" PRIu64 ",\"samples\":%" PRIu64
		",\"events\":%" PRIu64 ",\"lost\":%" PRIu64 ",\"overruns\":%" PRIu64 ",\"flattened\":%" PRIu64
		",\"seconds\":%.3f,\"lost_examples\":[", t->run, t->first, t->slices, t->samples, t->events, t->lost,
		t->overruns, t->flattened, seconds);
	for (i = 0; i < t->n_lost_ex; i++)
		fprintf(f, "%s%" PRIu64, i ? "," : "", t->lost_ex[i]);
	fprintf(f, "],\"overrun_examples\":[");
	for (i = 0; i < t->n_overrun_ex; i++)
		fprintf(f, "%s%" PRIu64, i ? "," : "", t->overrun_ex[i]);
	fprintf(f, "],\"ended\":\"%s\",\"out\":\"%s\"", t->ended ? t->ended : "", out ? out : "");
	if (out)
		fprintf(f, ",\"format\":\"ci16: int16 I then int16 Q per sample, 14-bit scale (full scale 8192)\"");
	fprintf(f, "}\n");
}

int main(int argc, char **argv)
{
	const char *name = W1700K_STREAM_NAME, *out = NULL;
	double seconds = 10, idle_ms = 1000, wait_s = 120, capacity_mib = 4096, report_s = 10;
	struct w1700k_stream_header *h = NULL;
	struct tally t = {0};
	int16_t *buffer = NULL;
	uint64_t capacity_slices = 0, index, start_ns = 0, last_ns, report_ns, slice_bytes = W1700K_STREAM_SLICE * 4;
	size_t bytes = 0;
	int i, status = 0;

	for (i = 1; i < argc; i++) {
		const char *arg = argv[i];

		if (!strcmp(arg, "-h") || !strcmp(arg, "--help")) {
			usage();
			return 0;
		}
		if (i + 1 >= argc) {
			fprintf(stderr, "sdr-recv: %s needs a value\n", arg);
			usage();
			return 2;
		}
		if (!strcmp(arg, "--stream"))
			name = argv[++i];
		else if (!strcmp(arg, "--seconds"))
			seconds = number(arg, argv[++i]);
		else if (!strcmp(arg, "--idle-ms"))
			idle_ms = number(arg, argv[++i]);
		else if (!strcmp(arg, "--wait"))
			wait_s = number(arg, argv[++i]);
		else if (!strcmp(arg, "--out"))
			out = argv[++i];
		else if (!strcmp(arg, "--capacity-mib"))
			capacity_mib = number(arg, argv[++i]);
		else if (!strcmp(arg, "--report"))
			report_s = number(arg, argv[++i]);
		else {
			fprintf(stderr, "sdr-recv: unknown option %s\n", arg);
			usage();
			return 2;
		}
	}
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	if (out) {
		capacity_slices = (uint64_t)(capacity_mib * 1048576.0) / slice_bytes;
		buffer = malloc(capacity_slices * slice_bytes);
		if (!capacity_slices || !buffer) {
			fprintf(stderr, "sdr-recv: cannot allocate %.0f MiB\n", capacity_mib);
			return 1;
		}
		memset(buffer, 0, capacity_slices * slice_bytes);	/* touched now, not while recording */
	}

	/* The service, then a run: the next one from its start, or the current one from now. */
	{
		const uint64_t deadline = w1700k_stream_now_ns() + (uint64_t)(wait_s * 1e9);
		uint64_t run = 0, newest = 0;

		while (!stop) {
			if (!h)
				h = w1700k_stream_open(name, 0, &bytes, NULL);
			if (h && w1700k_stream_alive(h))
				break;
			if (w1700k_stream_now_ns() > deadline) {
				if (!h && (errno == EACCES || errno == EPROTO))
					fprintf(stderr, "sdr-recv: %s is not a stream service's of this user (%s)\n", name,
						errno == EACCES ? "it belongs to someone else, or others may write it"
								: "its layout is not the ring's");
				else
					fprintf(stderr, "sdr-recv: no stream service at %s\n", name);
				return 1;
			}
			usleep(100000);
		}
		if (stop)
			return 1;
		w1700k_stream_position(h, &run, &newest);
		fprintf(stderr, "sdr-recv: reading %s%s\n", name, run ? ", the current run from now" : ", the next run from its start");
		if (run) {
			t.run = run;
			t.first = newest;
		} else {
			while (!stop && !run) {
				if (w1700k_stream_now_ns() > deadline) {
					fprintf(stderr, "sdr-recv: no stream came\n");
					return 1;
				}
				usleep(1000);
				w1700k_stream_position(h, &run, &newest);
			}
			t.run = run;
			t.first = 0;
		}
	}

	index = t.first;
	last_ns = report_ns = w1700k_stream_now_ns();
	while (!stop) {
		uint64_t run, newest, now = w1700k_stream_now_ns();

		w1700k_stream_position(h, &run, &newest);
		if (run != t.run) {
			t.ended = "the run ended";
			break;
		}
		if (index >= newest) {
			if (now - last_ns > (uint64_t)(idle_ms * 1e6)) {
				t.ended = "idle";
				break;
			}
			if (start_ns && now - start_ns > (uint64_t)(seconds * 1e9)) {
				t.ended = "seconds";
				break;
			}
			if (now - report_ns > (uint64_t)(report_s * 1e9)) {
				print_tally(stdout, &t, start_ns ? (now - start_ns) / 1e9 : 0, out);
				fflush(stdout);
				report_ns = now;
			}
			usleep(100);
			continue;
		}
		last_ns = now;
		if (!start_ns)
			start_ns = now;
		if (now - start_ns > (uint64_t)(seconds * 1e9)) {
			t.ended = "seconds";
			break;
		}
		for (; index < newest && !stop; index++) {
			const uint64_t want = w1700k_stream_tag(run, index);
			uint64_t tag = w1700k_stream_load(&w1700k_stream_tags(h)[index & (h->ring_slices - 1)]);
			int16_t *dst;
			int ok = 0;

			if (tag == (want | W1700K_STREAM_TAG_EVENT)) {
				t.events++;
				continue;
			}
			if (out && t.slices == capacity_slices) {
				t.ended = "capacity";
				goto done;
			}
			dst = out ? buffer + t.slices * W1700K_STREAM_SLICE * 2 : NULL;
			if ((tag & ~W1700K_STREAM_TAG_FLAT) == want) {
				if (dst)
					memcpy(dst, w1700k_stream_slice(h, index), slice_bytes);
				__atomic_thread_fence(__ATOMIC_ACQUIRE);
				ok = w1700k_stream_load(&w1700k_stream_tags(h)[index & (h->ring_slices - 1)]) == tag;
				t.flattened += ok && (tag & W1700K_STREAM_TAG_FLAT);
			}
			if (!ok) {
				if (dst)
					memset(dst, 0, slice_bytes);
				if (tag == (want | W1700K_STREAM_TAG_LOST)) {
					if (t.n_lost_ex < EXAMPLES)
						t.lost_ex[t.n_lost_ex++] = index;
					t.lost++;
				} else {
					if (t.n_overrun_ex < EXAMPLES)
						t.overrun_ex[t.n_overrun_ex++] = index;
					t.overruns++;
				}
			}
			t.slices++;
			t.samples += W1700K_STREAM_SLICE;
		}
	}
done:
	if (!t.ended)
		t.ended = "stopped";
	{
		const double took = start_ns ? (w1700k_stream_now_ns() - start_ns) / 1e9 : 0;

		if (out) {
			char path[4096];
			FILE *f = fopen(out, "wb"), *j;
			int ok = f && fwrite(buffer, slice_bytes, t.slices, f) == t.slices;

			if (f && fclose(f))
				ok = 0;
			snprintf(path, sizeof(path), "%s.json", out);
			j = fopen(path, "w");
			if (j) {
				print_tally(j, &t, took, out);
				if (fclose(j))
					ok = 0;
			} else {
				ok = 0;
			}
			if (!ok) {
				fprintf(stderr, "sdr-recv: writing %s: %s\n", out, strerror(errno));
				status = 1;
			}
		}
		print_tally(stdout, &t, took, out);
	}
	free(buffer);
	if (h)
		munmap(h, bytes);
	return status;
}
