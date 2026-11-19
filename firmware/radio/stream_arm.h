/* SPDX-License-Identifier: MIT */
/* The capture route and ICAP0 around a stream (stream_arm.c). */
#ifndef RADIO_STREAM_ARM_H
#define RADIO_STREAM_ARM_H

/* Before the producer runs: ICAP0 into the pool, stopped over the 1 MiB ring. With `two_paths`
 * its packer is off, and a beat is paths 0 and 1 at two sample times each; else path 0 at four. */
void stream_arm(int two_paths);
/* After it (not while a core may still read the pool: the WA must have left the packer). */
void stream_disarm(void);

#endif
