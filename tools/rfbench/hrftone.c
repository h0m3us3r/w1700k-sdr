// SPDX-License-Identifier: MIT
/* A steady test tone from a HackRF One, for measuring the receiver against a known source.
 *
 *   hrftone --info                      the board and its firmware; transmits nothing
 *   hrftone FREQ_HZ TXVGA_DB            a tone at FREQ_HZ until "q" or end of input on stdin
 *
 * While it runs, lines on stdin retune it: "f HZ" a new frequency, "g DB" a new TX gain
 * (0..47 dB, the HackRF's TX VGA), "q" stop. Each is answered with a line on stdout.
 *
 * The tone is the carrier offset by a quarter of the sample rate (LO at FREQ - 2 MHz, tone at
 * +2 MHz), so the HackRF's own carrier leakage sits 2 MHz below it and is not mistaken for it.
 * The front-end amplifier is never enabled: the output stays at most about 0 dBm.
 */
#include <libhackrf/hackrf.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SAMPLE_RATE 8000000.0
#define OFFSET_HZ 2000000ULL
#define AMPLITUDE 100

static volatile sig_atomic_t stopping;
static void on_signal(int s) { (void)s; stopping = 1; }

/* A quarter of the sample rate: four samples a cycle, exact in 8 bits. */
static int tx(hackrf_transfer *t)
{
	static const int8_t cycle[8] = { AMPLITUDE, 0, 0, AMPLITUDE, -AMPLITUDE, 0, 0, -AMPLITUDE };

	for (int i = 0; i + 8 <= t->valid_length; i += 8)
		memcpy(t->buffer + i, cycle, 8);
	return 0;
}

static int check(int rc, const char *what)
{
	if (rc != HACKRF_SUCCESS) {
		fprintf(stderr, "hrftone: %s: %s\n", what, hackrf_error_name(rc));
		return 1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	hackrf_device *dev = NULL;
	char line[64], version[64] = "";
	uint8_t board = 0;

	if (argc != 2 && argc != 3) {
		fprintf(stderr, "usage: hrftone --info | hrftone FREQ_HZ TXVGA_DB\n");
		return 2;
	}
	if (check(hackrf_init(), "init") || check(hackrf_open(&dev), "open"))
		return 1;
	hackrf_board_id_read(dev, &board);
	hackrf_version_string_read(dev, version, sizeof(version) - 1);
	if (!strcmp(argv[1], "--info")) {
		printf("board %s, firmware %s\n", hackrf_board_id_name(board), version);
		hackrf_close(dev);
		hackrf_exit();
		return 0;
	}
	if (argc != 3)
		return 2;
	uint64_t freq = strtoull(argv[1], NULL, 10);
	unsigned gain = (unsigned)strtoul(argv[2], NULL, 10);

	if (freq < 1000000ULL + OFFSET_HZ || freq > 6000000000ULL || gain > 47) {
		fprintf(stderr, "hrftone: frequency 3 MHz..6 GHz, gain 0..47 dB\n");
		return 2;
	}
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	if (check(hackrf_set_sample_rate(dev, SAMPLE_RATE), "sample rate") ||
	    check(hackrf_set_amp_enable(dev, 0), "amplifier off") ||
	    check(hackrf_set_txvga_gain(dev, gain), "gain") ||
	    check(hackrf_set_freq(dev, freq - OFFSET_HZ), "frequency") ||
	    check(hackrf_start_tx(dev, tx, NULL), "start"))
		return 1;
	printf("on %llu %u\n", (unsigned long long)freq, gain);
	fflush(stdout);
	while (!stopping && fgets(line, sizeof(line), stdin)) {
		if (line[0] == 'q')
			break;
		if (line[0] == 'f') {
			freq = strtoull(line + 1, NULL, 10);
			if (freq < 1000000ULL + OFFSET_HZ || freq > 6000000000ULL || hackrf_set_freq(dev, freq - OFFSET_HZ))
				printf("error frequency\n");
			else
				printf("on %llu %u\n", (unsigned long long)freq, gain);
		} else if (line[0] == 'g') {
			unsigned g = (unsigned)strtoul(line + 1, NULL, 10);
			if (g > 47 || hackrf_set_txvga_gain(dev, g))
				printf("error gain\n");
			else
				printf("on %llu %u\n", (unsigned long long)freq, gain = g);
		}
		fflush(stdout);
	}
	hackrf_stop_tx(dev);
	hackrf_close(dev);
	hackrf_exit();
	printf("off\n");
	return 0;
}
