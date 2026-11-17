// SPDX-License-Identifier: MIT
// Test helper: raw slices to w1700k-stream as fast as asked, from several sockets at once, so its
// receive threads are all busy at the same time (tests/test_stream.py). The sequence numbers come
// from one counter, so the stream is in order but for what the threads overtake each other by.
//
//   stream_blast PORT MAGIC THREADS SLICES_PER_SECOND SECONDS
//
// Prints the number of slices sent.
#include <arpa/inet.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

enum { SLICE_BYTES = 8192, HEADER = 16, BURST = 16 };

static atomic_ullong next_seq;
static unsigned port, threads;
static uint32_t magic;
static double rate, seconds;

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

static void *blast(void *arg)
{
	(void)arg;
	static _Thread_local uint8_t datagram[HEADER + SLICE_BYTES];
	struct sockaddr_in to = { .sin_family = AF_INET, .sin_port = htons(port) };
	int fd = socket(AF_INET, SOCK_DGRAM, 0);
	uint16_t offset = 0, bytes = SLICE_BYTES;
	double began = now(), each = threads / rate;
	unsigned long long sent = 0;

	inet_pton(AF_INET, "127.0.0.1", &to.sin_addr);
	if (fd < 0 || connect(fd, (struct sockaddr *)&to, sizeof(to)))
		return NULL;
	memcpy(datagram, &magic, 4);
	memcpy(datagram + 4, &offset, 2);
	memcpy(datagram + 6, &bytes, 2);
	while (now() - began < seconds) {
		for (int k = 0; k < BURST; k++, sent++) {
			uint64_t seq = atomic_fetch_add(&next_seq, 1);
			uint32_t *words = (uint32_t *)(datagram + HEADER);

			memcpy(datagram + 8, &seq, 8);
			for (int n = 0; n < 64; n++)	/* 14-bit samples, spare bits clear, no two slices alike */
				words[n * 32] = (uint32_t)((seq + n) * 2654435761u) & 0x3fff3fffu;
			if (send(fd, datagram, sizeof(datagram), 0) < 0)
				usleep(100);		/* the socket's queue is full: let the receiver catch up */
		}
		double ahead = began + sent * each - now();
		if (ahead > 0)
			usleep((useconds_t)(ahead * 1e6));
	}
	close(fd);
	return NULL;
}

int main(int argc, char **argv)
{
	pthread_t thread[64];

	if (argc != 6)
		return 2;
	port = atoi(argv[1]);
	magic = strtoul(argv[2], NULL, 0);
	threads = atoi(argv[3]);
	rate = atof(argv[4]);
	seconds = atof(argv[5]);
	if (!threads || threads > 64 || rate <= 0)
		return 2;
	for (unsigned k = 0; k < threads; k++)
		pthread_create(&thread[k], NULL, blast, NULL);
	for (unsigned k = 0; k < threads; k++)
		pthread_join(thread[k], NULL);
	printf("%llu\n", (unsigned long long)atomic_load(&next_seq));
	return 0;
}
