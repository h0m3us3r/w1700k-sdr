// SPDX-License-Identifier: MIT
/*
 * w1700k-efr32-link: the low-power radio's serial port, joined to standard input and output.
 *
 *   w1700k-efr32-link [--baud N] [--port DEV]
 *
 * Every byte read from standard input goes to the radio and every byte from the radio to
 * standard output, as they come and unchanged, until standard input ends. Run over ssh it is
 * the radio's port on the host: `ssh root@unit w1700k-efr32 link`, which is what the host's
 * `w1700k efr32` commands do. Nothing here speaks to the radio or knows its protocol.
 *
 * The radio (a Silicon Labs EFR32MG21) is on the SoC's second UART, /dev/ttyS1, 8N1 and no
 * flow control. Its factory firmware talks at 115200 baud, the default here; firmware an owner
 * puts in it may use another speed. On this port the kernel's driver gives half the speed it
 * is asked for (it takes every port's divider to be fed 20 MHz, and this one's is fed 10:
 * measured, the radio's boot message is read at 115200 only when 230400 is asked), so twice
 * the speed wanted is asked: 115200 and 230400 are what the port then reaches.
 *
 * The port is there only on a W1700K whose owner keeps the radio on: the loader enables it in
 * the device tree then, and not otherwise (platform/README.md, "The low-power radio").
 */
#define _DEFAULT_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#include <linux/serial.h>

/* What the driver is asked for to get a speed on this port (above). */
#ifndef PORT_SPEED_FACTOR
#define PORT_SPEED_FACTOR 2
#endif

static int usage(void)
{
	fprintf(stderr, "usage: w1700k-efr32-link [--baud 115200|230400] [--port DEV]\n");
	return 2;
}

static speed_t speed_for(long baud)
{
	switch (baud * PORT_SPEED_FACTOR) {
	case 115200: return B115200;
	case 230400: return B230400;
	case 460800: return B460800;
	default: return B0;
	}
}

/* All of `n` bytes to `fd`, waiting where it is not ready: 0, or -1 with errno. */
static int put(int fd, const unsigned char *bytes, ssize_t n)
{
	while (n > 0) {
		ssize_t done = write(fd, bytes, (size_t)n);

		if (done < 0) {
			struct pollfd wait = { .fd = fd, .events = POLLOUT };

			if (errno == EINTR)
				continue;
			if (errno != EAGAIN)
				return -1;
			poll(&wait, 1, -1);
			continue;
		}
		bytes += done;
		n -= done;
	}
	return 0;
}

int main(int argc, char **argv)
{
	const char *port = "/dev/ttyS1";
	struct serial_struct serial;
	struct termios tio;
	unsigned char buffer[4096];
	long baud = 115200;
	speed_t speed;
	char *end;
	int fd, ends = 0;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--port") && i + 1 < argc) {
			port = argv[++i];
		} else if (!strcmp(argv[i], "--baud") && i + 1 < argc) {
			errno = 0;
			baud = strtol(argv[++i], &end, 10);
			if (errno || *end || end == argv[i])
				return usage();
		} else {
			return usage();
		}
	}
	speed = speed_for(baud);
	if (speed == B0) {
		fprintf(stderr, "w1700k-efr32-link: %ld baud is not one this port reaches (115200, 230400)\n", baud);
		return 2;
	}
	fd = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK);
	if (fd < 0) {
		fprintf(stderr, "w1700k-efr32-link: %s: %s\n", port, strerror(errno));
		return 1;
	}
	/* A port the device tree leaves disabled still has its device file: it is of no type. */
	if (isatty(fd) && !ioctl(fd, TIOCGSERIAL, &serial) && serial.type == PORT_UNKNOWN) {
		fprintf(stderr, "w1700k-efr32-link: %s is not there: the loader enables the radio's port on a unit "
			"whose owner keeps the radio on (w1700k-efr32 on --keep, then restart)\n", port);
		return 1;
	}
	if (isatty(fd)) {
		if (tcgetattr(fd, &tio) < 0) {
			fprintf(stderr, "w1700k-efr32-link: %s: %s\n", port, strerror(errno));
			return 1;
		}
		cfmakeraw(&tio);
		tio.c_cflag &= ~(tcflag_t)(CSTOPB | CRTSCTS | PARENB);
		tio.c_cflag |= CLOCAL | CREAD;
		tio.c_cc[VMIN] = 1;
		tio.c_cc[VTIME] = 0;
		cfsetispeed(&tio, speed);
		cfsetospeed(&tio, speed);
		if (tcsetattr(fd, TCSANOW, &tio) < 0) {
			fprintf(stderr, "w1700k-efr32-link: %s cannot be set to %ld baud: %s\n", port, baud, strerror(errno));
			return 1;
		}
		tcflush(fd, TCIOFLUSH);
	}
	signal(SIGPIPE, SIG_IGN);
	for (;;) {
		struct pollfd fds[2] = { { .fd = ends ? -1 : STDIN_FILENO, .events = POLLIN }, { .fd = fd, .events = POLLIN } };
		ssize_t n;

		if (poll(fds, 2, ends ? 200 : -1) < 0) {
			if (errno == EINTR)
				continue;
			fprintf(stderr, "w1700k-efr32-link: %s\n", strerror(errno));
			return 1;
		}
		if (fds[1].revents & (POLLIN | POLLERR | POLLHUP)) {
			n = read(fd, buffer, sizeof(buffer));
			if (n > 0) {
				if (put(STDOUT_FILENO, buffer, n) < 0)
					return 0;		/* nobody reads any more */
			} else if (n == 0 || (errno != EAGAIN && errno != EINTR)) {
				fprintf(stderr, "w1700k-efr32-link: %s: %s\n", port, n ? strerror(errno) : "closed");
				return 1;
			}
			continue;
		}
		if (ends)			/* standard input ended and the radio has said its last */
			return 0;
		if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
			n = read(STDIN_FILENO, buffer, sizeof(buffer));
			if (n > 0) {
				if (put(fd, buffer, n) < 0) {
					fprintf(stderr, "w1700k-efr32-link: %s: %s\n", port, strerror(errno));
					return 1;
				}
			} else if (n == 0 || (errno != EAGAIN && errno != EINTR)) {
				ends = 1;
			}
		}
	}
}
