// SPDX-License-Identifier: MIT
/*
 * w1700k-gps: the power line of the unit's GPS receiver.
 *
 *   w1700k-gps off      hold the receiver off
 *   w1700k-gps on       let it run
 *   w1700k-gps status   which it is: `gps=on` or `gps=off`
 *   options: --chip DEV (default /dev/gpiochip0), --line N (default 43)
 *
 * The board carries an Airoha AG3352 GNSS receiver, which the stock firmware used to report where
 * the unit is for 6 GHz power control. Its power is line 43 of the pin controller, high for on,
 * and the board pulls that line high: left alone, the receiver runs from power-on and sends a
 * position every second on the SoC's high-speed UART, whether or not anything listens. Nothing
 * of ours listens (the port is disabled in the device tree), so the loader switches the
 * receiver off at every boot, before any root runs (`gps_off` in its environment). Where a unit
 * is stays with its owner: it is not found out unless the owner turns this on, and `on` lasts
 * until the unit next starts.
 *
 * The line is driven and then let go. This pin controller leaves a line as it was last driven
 * when its holder lets go (seen on a unit: the receiver stays off), which is what `off` relies
 * on; `status` reads the line as it is, without driving it.
 *
 * That is the W1700K. The same loader and root run on the XR1710G, which has no receiver:
 * line 43 there is the green LED of a 1 GbE port. The loader tells the board from the serial
 * number in the unit's factory identity and hands its name to the system in the device tree
 * (`/chosen/w1700k,board`: w1700k, xr1710g or unknown); on the XR1710G it puts the LED out
 * again after `gps_off`. On a board named as one without a receiver this does nothing and
 * says so, whatever it was asked. A board the loader could not name is treated as a W1700K.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <linux/gpio.h>

enum { OFF, ON, STATUS };

/* The board's name, as the loader hands it over. */
#ifndef BOARD_NAME
#define BOARD_NAME "/proc/device-tree/chosen/w1700k,board"
#endif

/* Boards this system runs on that carry no GPS receiver (platform/boards.py: gps_line None). */
static const char *const no_receiver[] = { "xr1710g" };

/* The board's name if it is one without a receiver, else NULL: also where no name was handed
 * over (a loader from before boards were told apart, which ran on the W1700K alone). */
static const char *board_without_receiver(void)
{
	char name[32] = "";
	FILE *handed = fopen(BOARD_NAME, "r");

	if (!handed)
		return NULL;
	if (!fgets(name, sizeof(name), handed))		/* a device-tree string: up to its NUL */
		name[0] = 0;
	fclose(handed);
	for (size_t i = 0; i < sizeof(no_receiver) / sizeof(no_receiver[0]); i++)
		if (!strcmp(name, no_receiver[i]))
			return no_receiver[i];
	return NULL;
}

static int usage(void)
{
	fprintf(stderr, "usage: w1700k-gps off | on | status [--chip DEV] [--line N]\n");
	return 2;
}

int main(int argc, char **argv)
{
	struct gpio_v2_line_request request = { .num_lines = 1 };
	struct gpio_v2_line_values values = { .mask = 1 };
	const char *chip = "/dev/gpiochip0";
	const char *board;
	long line = 43;
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
	board = board_without_receiver();
	if (board) {
		fprintf(stderr, "w1700k-gps: this board (%s) has no GPS receiver; nothing done\n", board);
		return 1;
	}
	fd = open(chip, O_RDONLY);
	if (fd < 0) {
		fprintf(stderr, "w1700k-gps: %s: %s\n", chip, strerror(errno));
		return 1;
	}
	request.offsets[0] = (unsigned int)line;
	strcpy(request.consumer, "w1700k-gps");
	if (what != STATUS) {		/* with no direction asked for, the line is taken as it is */
		request.config.flags = GPIO_V2_LINE_FLAG_OUTPUT;
		request.config.num_attrs = 1;
		request.config.attrs[0].attr.id = GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES;
		request.config.attrs[0].attr.values = what == ON;
		request.config.attrs[0].mask = 1;
	}
	if (ioctl(fd, GPIO_V2_GET_LINE_IOCTL, &request) < 0) {
		fprintf(stderr, "w1700k-gps: line %ld of %s: %s\n", line, chip, strerror(errno));
		return 1;
	}
	if (ioctl(request.fd, GPIO_V2_LINE_GET_VALUES_IOCTL, &values) < 0) {
		fprintf(stderr, "w1700k-gps: line %ld of %s cannot be read: %s\n", line, chip, strerror(errno));
		return 1;
	}
	if (what != STATUS && (int)(values.bits & 1) != (what == ON)) {
		fprintf(stderr, "w1700k-gps: line %ld does not read as it was driven\n", line);
		return 1;
	}
	printf("gps=%s\n", values.bits & 1 ? "on" : "off");
	return 0;
}
