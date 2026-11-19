// SPDX-License-Identifier: MIT
/*
 * w1700k-efr32-line: the reset line of the unit's low-power radio.
 *
 *   w1700k-efr32-line off      hold the radio in reset
 *   w1700k-efr32-line on       let it run
 *   w1700k-efr32-line status   which it is: `efr32=on` or `efr32=off`
 *   options: --chip DEV (default /dev/gpiochip0), --line N (default 24)
 *
 * The W1700K carries a Silicon Labs EFR32MG21 beside its Wi-Fi radios: a 2.4 GHz chip for
 * Bluetooth LE and IEEE 802.15.4 (Zigbee, Thread) with its own antenna and a transmitter of
 * 10 dBm. Its reset is line 24 of the pin controller, low for reset, and the chip pulls that
 * line high itself: left alone, it starts whatever firmware it holds at power-on. Nothing of
 * ours talks to it (its port is disabled in the device tree) and it has no power switch that
 * we know of, so off is reset: the loader holds the line low at every boot, before any root
 * runs, unless the unit's owner has said otherwise (`efr32_off` and `efr32_pass` in its
 * environment; w1700k-efr32 is what the owner uses, and calls this).
 *
 * `off` drives the line low and lets it go; this pin controller leaves a line as it was last
 * driven when its holder lets go, which is what `off` relies on (as w1700k-gps does). `on` does
 * not drive it high: it makes the line an input again, as the board had it, and the chip's own
 * pull-up ends the reset. `status` reads the line as it is, without driving it.
 *
 * That is the W1700K. The same loader and root run on the XR1710G, which has no such chip, and
 * what its line 24 does is not known. The loader holds the line only on a unit it has named a
 * W1700K (`/chosen/w1700k,board` in the device tree), and so does this: on a board named
 * anything else, one the loader could not name included, it does nothing and says so,
 * whatever it was asked. (The chip does nothing unasked, so a W1700K that cannot be named loses
 * little; the GPS receiver, which reports where the unit is from power-on, is treated the
 * other way.) No name at all is a loader from before boards were told apart, which ran on the
 * W1700K alone.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#include <linux/gpio.h>

/* The board's name, as the loader hands it over. */
#ifndef BOARD_NAME
#define BOARD_NAME "/proc/device-tree/chosen/w1700k,board"
#endif

/* Boards this system runs on that carry the radio (platform/boards.py: efr32_reset_line). */
static const char *const with_radio[] = { "w1700k" };

/* The board's name if it is not one known to carry the radio, else NULL: also where no name was
 * handed over. */
static const char *board_without_radio(void)
{
	static char name[32];
	FILE *handed = fopen(BOARD_NAME, "r");

	if (!handed)
		return NULL;
	if (!fgets(name, sizeof(name), handed))		/* a device-tree string: up to its NUL */
		name[0] = 0;
	fclose(handed);
	for (size_t i = 0; i < sizeof(with_radio) / sizeof(with_radio[0]); i++)
		if (!strcmp(name, with_radio[i]))
			return NULL;
	return name;
}

enum { OFF, ON, STATUS };

static int usage(void)
{
	fprintf(stderr, "usage: w1700k-efr32-line off | on | status [--chip DEV] [--line N]\n");
	return 2;
}

int main(int argc, char **argv)
{
	struct gpio_v2_line_request request = { .num_lines = 1 };
	struct gpio_v2_line_values values = { .mask = 1 };
	const char *chip = "/dev/gpiochip0";
	const char *board;
	long line = 24;
	int what, fd;
	char *end;

	if (argc < 2)
		return usage();
	if (!strcmp(argv[1], "off"))
		what = OFF;
	else if (!strcmp(argv[1], "on"))
		what = ON;
	else if (!strcmp(argv[1], "status"))
		what = STATUS;
	else
		return usage();
	for (int i = 2; i < argc; i++) {
		if (!strcmp(argv[i], "--chip") && i + 1 < argc) {
			chip = argv[++i];
		} else if (!strcmp(argv[i], "--line") && i + 1 < argc) {
			errno = 0;
			line = strtol(argv[++i], &end, 10);
			if (errno || *end || end == argv[i] || line < 0 || line > 1023)
				return usage();
		} else {
			return usage();
		}
	}
	board = board_without_radio();
	if (board) {
		fprintf(stderr, "w1700k-efr32-line: this board (%s) is not known to carry the low-power radio; "
			"nothing done\n", *board ? board : "unnamed");
		return 1;
	}
	fd = open(chip, O_RDONLY);
	if (fd < 0) {
		fprintf(stderr, "w1700k-efr32-line: %s: %s\n", chip, strerror(errno));
		return 1;
	}
	request.offsets[0] = (unsigned int)line;
	strcpy(request.consumer, "w1700k-efr32");
	if (what == OFF) {
		request.config.flags = GPIO_V2_LINE_FLAG_OUTPUT;
		request.config.num_attrs = 1;
		request.config.attrs[0].attr.id = GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES;
		request.config.attrs[0].attr.values = 0;
		request.config.attrs[0].mask = 1;
	} else if (what == ON) {	/* an input: nothing of ours drives the chip's reset */
		request.config.flags = GPIO_V2_LINE_FLAG_INPUT;
	}				/* status: with no direction asked for, the line is taken as it is */
	if (ioctl(fd, GPIO_V2_GET_LINE_IOCTL, &request) < 0) {
		fprintf(stderr, "w1700k-efr32-line: line %ld of %s: %s\n", line, chip, strerror(errno));
		return 1;
	}
	if (what == ON)		/* the pull-up's time to bring the line up */
		nanosleep(&(struct timespec){ .tv_nsec = 1000000 }, NULL);
	if (ioctl(request.fd, GPIO_V2_LINE_GET_VALUES_IOCTL, &values) < 0) {
		fprintf(stderr, "w1700k-efr32-line: line %ld of %s cannot be read: %s\n", line, chip, strerror(errno));
		return 1;
	}
	if (what == OFF && (values.bits & 1)) {
		fprintf(stderr, "w1700k-efr32-line: line %ld does not read as it was driven\n", line);
		return 1;
	}
	if (what == ON && !(values.bits & 1)) {
		fprintf(stderr, "w1700k-efr32-line: line %ld stays low when let go: something else holds it\n", line);
		return 1;
	}
	printf("efr32=%s\n", values.bits & 1 ? "on" : "off");
	return 0;
}
