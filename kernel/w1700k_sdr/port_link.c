// SPDX-License-Identifier: MIT
/*
 * w1700k-port-link: one of the unit's serial ports, joined to standard input and output.
 *
 *   w1700k-port-link --port DEV [--baud N]
 *
 * Every byte read from standard input goes to the port and every byte from the port to
 * standard output, as they come and unchanged, until standard input ends. Run over ssh it is
 * the port on the host. Nothing here speaks to what is on the port or knows its protocol.
 *
 * Two ports are handed over this way, each by its owner's command, which checks that there is
 * somebody to talk to and names the port and its speed (platform/README.md):
 *
 *   w1700k-efr32 link   the low-power radio (a Silicon Labs EFR32MG21) on the SoC's second
 *                       UART, /dev/ttyS1; its factory firmware talks at 115200 baud
 *   w1700k-gps link     the GPS receiver (an Airoha AG3352) on the high-speed UART,
 *                       /dev/ttyS2, which sends at 921600 baud
 *
 * Both are 8N1 with no flow control. The speed asked for is the speed on the wire: that takes
 * our kernel patch 0015, without which the second UART ran at half of 230400 and a quarter of
 * 115200, and the high-speed one at twice what was asked. 115200 is the default.
 *
 * Each port is there only where its owner keeps the thing on it switched on: the loader
 * enables it in the device tree at such a boot, and not otherwise.
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

static int usage(void)
{
	fprintf(stderr, "usage: w1700k-port-link --port DEV [--baud 115200|230400|460800|921600]\n");
	return 2;
}

static speed_t speed_for(long baud)
{
	switch (baud) {
	case 115200: return B115200;
	case 230400: return B230400;
	case 460800: return B460800;
	case 921600: return B921600;
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
	const char *port = NULL;
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
	if (!port)
		return usage();
	speed = speed_for(baud);
	if (speed == B0) {
		fprintf(stderr, "w1700k-port-link: %ld baud is not a speed of these ports (115200, 230400, 460800, 921600)\n", baud);
		return 2;
	}
	fd = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK);
	if (fd < 0) {
		fprintf(stderr, "w1700k-port-link: %s: %s\n", port, strerror(errno));
		return 1;
	}
	/* A port the device tree leaves disabled still has its device file: it is of no type. */
	if (isatty(fd) && !ioctl(fd, TIOCGSERIAL, &serial) && serial.type == PORT_UNKNOWN) {
		fprintf(stderr, "w1700k-port-link: %s is not there: the device tree of this boot leaves the port "
			"disabled\n", port);
		return 1;
	}
	if (isatty(fd)) {
		if (tcgetattr(fd, &tio) < 0) {
			fprintf(stderr, "w1700k-port-link: %s: %s\n", port, strerror(errno));
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
			fprintf(stderr, "w1700k-port-link: %s cannot be set to %ld baud: %s\n", port, baud, strerror(errno));
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
			fprintf(stderr, "w1700k-port-link: %s\n", strerror(errno));
			return 1;
		}
		if (fds[1].revents & (POLLIN | POLLERR | POLLHUP)) {
			n = read(fd, buffer, sizeof(buffer));
			if (n > 0) {
				if (put(STDOUT_FILENO, buffer, n) < 0)
					return 0;		/* nobody reads any more */
			} else if (n == 0 || (errno != EAGAIN && errno != EINTR)) {
				fprintf(stderr, "w1700k-port-link: %s: %s\n", port, n ? strerror(errno) : "closed");
				return 1;
			}
			continue;
		}
		if (ends)			/* standard input ended and the port has said its last */
			return 0;
		if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
			n = read(STDIN_FILENO, buffer, sizeof(buffer));
			if (n > 0) {
				if (put(fd, buffer, n) < 0) {
					fprintf(stderr, "w1700k-port-link: %s: %s\n", port, strerror(errno));
					return 1;
				}
			} else if (n == 0 || (errno != EAGAIN && errno != EINTR)) {
				ends = 1;
			}
		}
	}
}
