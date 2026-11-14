// SPDX-License-Identifier: GPL-2.0-only
/*
 * w1700k-key [--chip DEV] [--line N] [--state FILE] [--slot-state FILE] [--slot PROGRAM]
 *            [--listen ADDRESS] [--port N] [--toggle S] [--program S] [--purge S] [--window S]
 *            [--buttons FILE]
 *
 * The paperclip key (keyd.h): watches the reset button and does what a press means.
 *
 *   held 5 s, released    the slot's key is switched off, or on again
 *   held 10 s, released   for three minutes the unit takes one public key, a line of text, on TCP
 *                         port 7722 of ADDRESS (the management address; default: every address),
 *                         checks it, keeps it in the slot and answers "ok ..." or "error: ...".
 *                         A press ends the wait.
 *   held 15 s, released   the slot's key is erased
 *
 * It keeps no key itself. PROGRAM (default /usr/sbin/w1700k-keyslot) does: `toggle`, `purge`, and
 * `program` with the key on its input; it prints what the slot then is, and what a host is told. The slot's
 * state as that program leaves it is read from --slot-state (default /run/w1700k-keyslot), for the
 * LED's colour while the button is held.
 *
 * What the button is doing goes to FILE (default /run/w1700k-key) for the LED (ledd.h): a word,
 * the time it began and a detail.
 *
 * The button is line N (default 0) of the GPIO chip DEV (default /dev/gpiochip0), low when
 * pressed. --buttons reads presses from FILE instead (a FIFO: `d` down, `u` up), and --toggle,
 * --program, --purge and --window shorten the times: both for tests.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/gpio.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "keyd.h"

#define DEBOUNCE_US 20000
#define KEY_TRIES 5		/* lines that were not keys, before the wait is ended */

static const char *state_path = "/run/w1700k-key";
static const char *slot_state_path = "/run/w1700k-keyslot";
static const char *slot_program = "/usr/sbin/w1700k-keyslot";
static volatile sig_atomic_t stopping;

static void on_signal(int sig)
{
	(void)sig;
	stopping = 1;
}

static double now(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

/* What the button is doing, for the LED: replaced whole, so that it is never read half written. */
static void say(const char *phase, double since, const char *detail)
{
	char part[256];
	FILE *f;

	snprintf(part, sizeof(part), "%s.part", state_path);
	f = fopen(part, "w");
	if (!f)
		return;
	fprintf(f, "%s %.3f %s\n", phase, since, detail);
	if (fclose(f) || rename(part, state_path))
		unlink(part);
}

static void slot_state(char *word, size_t size)
{
	FILE *f = fopen(slot_state_path, "r");

	snprintf(word, size, "empty");
	if (f) {
		if (fscanf(f, "%15s", word) != 1)
			snprintf(word, size, "empty");
		fclose(f);
	}
}

/* Run the slot's program with `verb`, `input` on its stdin; its first line of output in `out`.
 * Its exit status, or -1. */
static int slot_run(const char *verb, const char *input, char *out, size_t size)
{
	int in[2], back[2], status = -1;
	size_t used = 0;
	ssize_t n;
	pid_t pid;

	out[0] = 0;
	if (pipe(in) || pipe(back))
		return -1;
	pid = fork();
	if (pid < 0)
		return -1;
	if (!pid) {
		dup2(in[0], 0);
		dup2(back[1], 1);
		close(in[0]), close(in[1]), close(back[0]), close(back[1]);
		execl(slot_program, slot_program, verb, (char *)NULL);
		_exit(127);
	}
	close(in[0]);
	close(back[1]);
	if (input && write(in[1], input, strlen(input)) < 0)
		perror("w1700k-key: the slot's program");
	close(in[1]);
	while (used + 1 < size && (n = read(back[0], out + used, size - 1 - used)) > 0)
		used += (size_t)n;
	out[used] = 0;
	out[strcspn(out, "\r\n")] = 0;
	close(back[0]);
	while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
		;
	return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static int button_open(const char *chip, unsigned int line)
{
	struct gpio_v2_line_request req = { .num_lines = 1 };
	int fd = open(chip, O_RDONLY | O_CLOEXEC);

	if (fd < 0) {
		perror(chip);
		return -1;
	}
	req.offsets[0] = line;
	snprintf(req.consumer, sizeof(req.consumer), "w1700k-key");
	/* Active low: a rising edge is the press. The pin's pull is left as the board has it. */
	req.config.flags = GPIO_V2_LINE_FLAG_INPUT | GPIO_V2_LINE_FLAG_ACTIVE_LOW |
			   GPIO_V2_LINE_FLAG_EDGE_RISING | GPIO_V2_LINE_FLAG_EDGE_FALLING;
	req.config.num_attrs = 1;
	req.config.attrs[0].mask = 1;
	req.config.attrs[0].attr.id = GPIO_V2_LINE_ATTR_ID_DEBOUNCE;
	req.config.attrs[0].attr.debounce_period_us = DEBOUNCE_US;
	if (ioctl(fd, GPIO_V2_GET_LINE_IOCTL, &req) < 0) {
		fprintf(stderr, "w1700k-key: %s line %u: %s\n", chip, line, strerror(errno));
		close(fd);
		return -1;
	}
	close(fd);
	return req.fd;
}

static int button_down_now(int fd)
{
	struct gpio_v2_line_values values = { .mask = 1 };

	return !ioctl(fd, GPIO_V2_LINE_GET_VALUES_IOCTL, &values) && (values.bits & 1);
}

/* The next change of the button: 1 down, 0 up, -1 none (or the source ended). */
static int button_read(int fd, int fifo)
{
	struct gpio_v2_line_event event;
	char c;

	if (fifo)
		return read(fd, &c, 1) == 1 ? (c == 'd' ? 1 : c == 'u' ? 0 : -1) : -1;
	if (read(fd, &event, sizeof(event)) != sizeof(event))
		return -1;
	return event.id == GPIO_V2_LINE_EVENT_RISING_EDGE;
}

static int listen_on(const char *address, int port)
{
	struct sockaddr_in at = { .sin_family = AF_INET, .sin_port = htons((uint16_t)port) };
	int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0), one = 1;

	if (fd < 0)
		return -1;
	setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	if ((address && inet_pton(AF_INET, address, &at.sin_addr) != 1) ||
	    bind(fd, (struct sockaddr *)&at, sizeof(at)) || listen(fd, 1)) {
		fprintf(stderr, "w1700k-key: cannot listen on %s port %d: %s\n", address ? address : "any address",
			port, strerror(errno));
		close(fd);
		return -1;
	}
	return fd;
}

static void reply(int fd, const char *text)
{
	if (write(fd, text, strlen(text)) < 0 || write(fd, "\n", 1) < 0)
		perror("w1700k-key: reply");
}

/* One connection of the wait: a line, taken as a key or refused. 1: the key is in the slot;
 * 0: it was not a key, the wait goes on; -1: a key, but the slot did not keep it. */
static int take_key(int listener)
{
	char line[KEY_LINE_MAX + 1], slot[KEY_LINE_MAX], answer[1200], text[1300];
	struct timeval patience = { .tv_sec = 5 };
	int fd = accept(listener, NULL, NULL), result = 0;
	const char *wrong;
	size_t used = 0;
	ssize_t n;

	if (fd < 0)
		return 0;
	setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &patience, sizeof(patience));
	setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &patience, sizeof(patience));
	while (used < KEY_LINE_MAX && !memchr(line, '\n', used) && (n = read(fd, line + used, KEY_LINE_MAX - used)) > 0)
		used += (size_t)n;
	line[used] = 0;
	if (!memchr(line, '\n', used)) {
		reply(fd, "error: send one line, a public key, ended by a newline");
	} else if ((wrong = key_line_check(line, slot, sizeof(slot) - 1))) {
		snprintf(text, sizeof(text), "error: %s", wrong);
		reply(fd, text);
	} else {
		strcat(slot, "\n");
		if (slot_run("program", slot, answer, sizeof(answer))) {
			reply(fd, "error: the unit could not keep the key (its storage is in use or failed); try again");
			result = -1;
		} else {
			snprintf(text, sizeof(text), "ok %s", answer);
			reply(fd, text);
			result = 1;
		}
	}
	close(fd);
	return result;
}

static int usage(void)
{
	fprintf(stderr, "usage: w1700k-key [--chip DEV] [--line N] [--state FILE] [--slot-state FILE] [--slot PROGRAM]\n"
		"       [--listen ADDRESS] [--port N] [--toggle S] [--program S] [--purge S] [--window S]\n"
		"       [--buttons FILE]\n");
	return 2;
}

int main(int argc, char **argv)
{
	struct sigaction sa = { .sa_handler = on_signal };
	const char *chip = "/dev/gpiochip0", *address = NULL, *buttons = NULL;
	double toggle = KEY_TOGGLE_SECONDS, program = KEY_PROGRAM_SECONDS, purge = KEY_PURGE_SECONDS;
	double window = KEY_WINDOW_SECONDS;
	double pressed = 0, until = 0, t;
	int line = 0, port = KEY_PORT, button, listener = -1, down = 0, ignore, tries = 0, i, change, got;
	char word[16] = "empty", answer[64];
	struct pollfd fds[2];

	for (i = 1; i < argc; i++) {
		const char *value = i + 1 < argc ? argv[i + 1] : NULL;

		if (!value || argv[i][0] != '-')
			return usage();
		if (!strcmp(argv[i], "--chip")) chip = value;
		else if (!strcmp(argv[i], "--line")) line = atoi(value);
		else if (!strcmp(argv[i], "--state")) state_path = value;
		else if (!strcmp(argv[i], "--slot-state")) slot_state_path = value;
		else if (!strcmp(argv[i], "--slot")) slot_program = value;
		else if (!strcmp(argv[i], "--listen")) address = value;
		else if (!strcmp(argv[i], "--port")) port = atoi(value);
		else if (!strcmp(argv[i], "--toggle")) toggle = atof(value);
		else if (!strcmp(argv[i], "--program")) program = atof(value);
		else if (!strcmp(argv[i], "--purge")) purge = atof(value);
		else if (!strcmp(argv[i], "--window")) window = atof(value);
		else if (!strcmp(argv[i], "--buttons")) buttons = value;
		else return usage();
		i++;
	}
	if (line < 0 || port <= 0 || port > 65535 || toggle <= 0 || program <= toggle || purge <= program ||
	    window <= 0)
		return usage();
	button = buttons ? open(buttons, O_RDWR | O_CLOEXEC) : button_open(chip, (unsigned int)line);
	if (button < 0) {
		if (buttons)
			perror(buttons);
		return 1;
	}
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	signal(SIGPIPE, SIG_IGN);
	/* A button already down when this starts (held through a boot) means nothing until let go. */
	ignore = !buttons && button_down_now(button);
	say("none", now(), "-");

	while (!stopping) {
		fds[0] = (struct pollfd){ .fd = button, .events = POLLIN };
		fds[1] = (struct pollfd){ .fd = listener, .events = POLLIN };
		got = poll(fds, listener >= 0 ? 2 : 1, listener >= 0 ? 250 : -1);
		t = now();
		if (got < 0 && errno != EINTR)
			break;
		if (got > 0 && (fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)))
			break;
		if (got > 0 && (fds[0].revents & POLLIN)) {
			change = button_read(button, buttons != NULL);
			if (change == 1 && !down) {
				down = 1;
				pressed = t;
				if (listener >= 0) {		/* a press ends the wait, and means no more */
					close(listener);
					listener = -1;
					ignore = 1;
					say("closed", t, "-");
				} else if (!ignore) {
					slot_state(word, sizeof(word));
					say("held", t, word);
				}
			} else if (change == 0 && down) {
				down = 0;
				if (ignore) {
					ignore = 0;
					continue;
				}
				switch (key_action_for(t - pressed, toggle, program, purge)) {
				case KEY_ACT_NONE:
					snprintf(answer, sizeof(answer), "%.3f %s", t - pressed, word);
					say("drain", t, answer);
					break;
				case KEY_ACT_TOGGLE:
					if (slot_run("toggle", NULL, answer, sizeof(answer)))
						say("failed", now(), "-");
					else
						say(!strncmp(answer, "on", 2) ? "on" : !strncmp(answer, "off", 3) ? "off" : "empty",
						    now(), "-");
					break;
				case KEY_ACT_PURGE:
					if (slot_run("purge", NULL, answer, sizeof(answer)))
						say("failed", now(), "-");
					else
						say(!strncmp(answer, "purged", 6) ? "purged" : "empty", now(), "-");
					break;
				case KEY_ACT_PROGRAM:
					listener = listen_on(address, port);
					if (listener < 0) {
						say("failed", t, "-");
						break;
					}
					until = t + window;
					tries = 0;
					say("window", t, "-");
					break;
				}
			} else if (change < 0 && buttons) {
				break;	/* the test's presses are over */
			}
		}
		if (listener >= 0 && got > 0 && (fds[1].revents & POLLIN)) {
			got = take_key(listener);
			if (got || ++tries >= KEY_TRIES) {
				close(listener);
				listener = -1;
				say(got > 0 ? "taken" : "failed", now(), "-");
			}
		}
		if (listener >= 0 && now() >= until) {
			close(listener);
			listener = -1;
			say("closed", now(), "-");
		}
	}
	say("none", now(), "-");
	return 0;
}
