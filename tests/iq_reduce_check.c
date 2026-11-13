// SPDX-License-Identifier: MIT
/*
 * iq_reduce_check radio14 SHIFT [decode]: the radio-reduced chunk's model instead: each slice's
 * 3588 bytes, or with `decode` the slice read back.
 * iq_reduce_check FORMAT [decode]: raw slices (2048 le32 words each) on stdin;
 * for each, the scale code (one byte) and the reduced slice on stdout, or with
 * `decode` the slice reduced and read back (2048 le32 words). Built for the
 * host it runs the reference encoder, for the unit the NEON one: the two must
 * agree byte for byte (tests/test_iq_reduce.py).
 */
#include <stdio.h>
#include <stdlib.h>
#include "hw/mt7996/iq_reduce.h"

int main(int argc, char **argv)
{
	static uint32_t words[IQ_REDUCE_SAMPLES], back[IQ_REDUCE_SAMPLES];
	static uint8_t out[IQ_REDUCE_MAX_BYTES + IQ_REDUCE_SLACK];
	unsigned format = argc > 1 ? (unsigned)atoi(argv[1]) : 0, bytes = iq_reduce_bytes(format), code;
	int decode = argc > 2;

	if (argc > 2 && !strcmp(argv[1], "radio14")) {
		static uint8_t chunk[IQ_RADIO14_BYTES];
		unsigned shift = (unsigned)atoi(argv[2]);

		if (shift > IQ_RADIO14_SHIFT_MAX)
			return 2;
		while (fread(words, sizeof(words), 1, stdin) == 1) {
			iq_radio14_encode(shift, words, chunk);
			if (argc > 3) {
				if (iq_radio14_decode(chunk, back) != (int)shift)
					return 3;
				fwrite(back, sizeof(back), 1, stdout);
			} else {
				fwrite(chunk, 1, sizeof(chunk), stdout);
			}
		}
		return fflush(stdout) ? 1 : 0;
	}
	if (!bytes)
		return 2;
	while (fread(words, sizeof(words), 1, stdin) == 1) {
		code = iq_reduce_encode(format, words, out);
		if (decode) {
			iq_reduce_decode(format, code, out, back);
			fwrite(back, sizeof(back), 1, stdout);
		} else {
			fputc((int)code, stdout);
			fwrite(out, 1, bytes, stdout);
		}
	}
	return fflush(stdout) ? 1 : 0;
}
