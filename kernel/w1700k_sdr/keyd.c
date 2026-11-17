// SPDX-License-Identifier: GPL-2.0-only
/*
 * w1700k-key [--chip DEV] [--line N] [--state FILE] [--slot-state FILE] [--slot PROGRAM]
 *            [--listen ADDRESS] [--port N] [--toggle S] [--program S] [--purge S] [--window S]
 *            [--confirm S] [--apply S] [--pending S] [--buttons FILE]
 *
 * The paperclip key (keyd.h): watches the reset button and does what a press means.
 *
 *   held 5 s, released    the slot's key is switched off, or on again
 *   held 10 s, released   for three minutes the unit takes one public key, a line of text, on TCP
 *                         port 7722 of ADDRESS (the management address; default: every address).
 *                         A press ends the wait.
 *   held 15 s, released   the slot's key is erased
 *
 * A key that arrives is checked and held, and its sender is told "pending key=TYPE:BASE64 hold=S
 * within=S": nothing is written and nothing logs in with it. The button decides:
 *
 *   held 3 s, released    the key goes into the slot; its sender is told "ok ..."
 *   held 5 s              the same, without waiting for the button to be let go
 *   a shorter press       it is discarded; "error: ..."
 *   a minute, no press    it is discarded; "error: ..."
 *
 * That was the one key of this wait, kept or not. Whoever else sends a line while it is held is
 * told "busy key=TYPE:BASE64", the key the unit holds, so that a host whose own key came second
 * can say so to the hand on the button. A line that is not a key is answered "error: ..." and the
 * wait goes on.
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
 * --program, --purge, --window, --confirm, --apply and --pending shorten the times: both for tests.
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
#define KEY_SENDERS 4		/* connections heard at once */
#define KEY_SENDER_SECONDS 3.0	/* a sender's line has to be whole within this, all told */

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
	    bind(fd, (struct sockaddr *)&at, sizeof(at)) || listen(fd, KEY_SENDERS)) {
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

struct sender {
	int fd;			/* -1: free */
	double until;		/* its line has to be whole by then */
	size_t used;
	char line[KEY_LINE_MAX + 1];
};

/* The wait for a key, and the key that came. */
struct wait {
	int listener;		/* -1: the slot is not open */
	double until;		/* the wait ends then, if no key has come */
	struct sender senders[KEY_SENDERS];
	int pending;		/* a key is held, for the button to keep or discard */
	char key[KEY_LINE_MAX];	/* that key, as the slot would hold it */
	double key_until;	/* it is discarded then */
	int holder;		/* its sender, kept open for the outcome; -1 when there is none */
	double confirm, apply, patience;
};

static struct sender *sender_free(struct wait *w)
{
	int i;

	for (i = 0; i < KEY_SENDERS; i++)
		if (w->senders[i].fd < 0)
			return &w->senders[i];
	return NULL;
}

/* The wait is over: `outcome`, if any, to the sender of the held key, and everything closed. */
static void wait_end(struct wait *w, const char *outcome)
{
	int i;

	if (w->holder >= 0) {
		if (outcome)
			reply(w->holder, outcome);
		close(w->holder);
	}
	for (i = 0; i < KEY_SENDERS; i++)
		if (w->senders[i].fd >= 0) {
			close(w->senders[i].fd);
			w->senders[i].fd = -1;
		}
	if (w->listener >= 0)
		close(w->listener);
	w->listener = w->holder = -1;
	w->pending = 0;
}

static void wait_accept(struct wait *w, double t)
{
	struct sender *s = sender_free(w);
	int fd;

	if (!s)
		return;
	fd = accept4(w->listener, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
	if (fd < 0)
		return;
	s->fd = fd;
	s->until = t + KEY_SENDER_SECONDS;
	s->used = 0;
}

/* What has come of a sender's line: 1 all of it, 0 not yet, -1 it ended without one. Nothing
 * here waits: a sender that trickles is cut off at its deadline, not followed. */
static int sender_line(struct sender *s)
{
	ssize_t n;

	while (s->used < KEY_LINE_MAX && !memchr(s->line, '\n', s->used)) {
		n = read(s->fd, s->line + s->used, KEY_LINE_MAX - s->used);
		if (n > 0) {
			s->used += (size_t)n;
			continue;
		}
		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
			return 0;
		break;
	}
	s->line[s->used] = 0;
	return memchr(s->line, '\n', s->used) ? 1 : -1;
}

/* A sender's whole line, or the lack of one: answered, and unless its key is now the held one,
 * closed. A key is only ever held here: the button keeps it (wait_keep). */
static void sender_judge(struct wait *w, struct sender *s, int whole, double t)
{
	char slot[KEY_LINE_MAX], text[KEY_LINE_MAX + 100];
	const char *wrong;
	int keep = 0;

	if (!whole) {
		reply(s->fd, "error: send one line, a public key, ended by a newline");
	} else if ((wrong = key_line_check(s->line, slot, sizeof(slot)))) {
		snprintf(text, sizeof(text), "error: %s", wrong);
		reply(s->fd, text);
	} else if (w->pending && strcmp(slot, w->key)) {
		snprintf(text, sizeof(text), "busy key=%s", w->key);
		reply(s->fd, text);
	} else {
		if (!w->pending) {
			w->pending = 1;
			memcpy(w->key, slot, sizeof(slot));
			w->key_until = t + w->patience;
			say("pending", t, "-");
		} else if (w->holder >= 0) {	/* the same key again: its newest sender hears the outcome */
			close(w->holder);
		}
		w->holder = s->fd;
		snprintf(text, sizeof(text), "pending key=%s hold=%g within=%g", w->key, w->confirm,
			 w->key_until - t > 0 ? w->key_until - t : 0.0);
		reply(s->fd, text);
		keep = 1;
	}
	if (!keep)
		close(s->fd);
	s->fd = -1;
}

/* The hand said yes: the held key goes into the slot. */
static void wait_keep(struct wait *w)
{
	char line[KEY_LINE_MAX + 1], answer[1200], text[1300];

	snprintf(line, sizeof(line), "%s\n", w->key);
	if (slot_run("program", line, answer, sizeof(answer))) {
		wait_end(w, "error: the unit could not keep the key (its storage is in use or failed); try again");
		say("failed", now(), "-");
	} else {
		snprintf(text, sizeof(text), "ok %s", answer);
		wait_end(w, text);
		say("taken", now(), "-");
	}
}

static int usage(void)
{
	fprintf(stderr, "usage: w1700k-key [--chip DEV] [--line N] [--state FILE] [--slot-state FILE] [--slot PROGRAM]\n"
		"       [--listen ADDRESS] [--port N] [--toggle S] [--program S] [--purge S] [--window S]\n"
		"       [--confirm S] [--apply S] [--pending S] [--buttons FILE]\n");
	return 2;
}

int main(int argc, char **argv)
{
	struct sigaction sa = { .sa_handler = on_signal };
	const char *chip = "/dev/gpiochip0", *address = NULL, *buttons = NULL;
	double toggle = KEY_TOGGLE_SECONDS, program = KEY_PROGRAM_SECONDS, purge = KEY_PURGE_SECONDS;
	double window = KEY_WINDOW_SECONDS;
	struct wait w = { .listener = -1, .holder = -1, .confirm = KEY_CONFIRM_SECONDS,
			  .apply = KEY_APPLY_SECONDS, .patience = KEY_PENDING_SECONDS };
	double pressed = 0, t;
	int line = 0, port = KEY_PORT, button, down = 0, ignore, i, change, got;
	char word[16] = "empty", answer[64];

	for (i = 0; i < KEY_SENDERS; i++)
		w.senders[i].fd = -1;
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
		else if (!strcmp(argv[i], "--confirm")) w.confirm = atof(value);
		else if (!strcmp(argv[i], "--apply")) w.apply = atof(value);
		else if (!strcmp(argv[i], "--pending")) w.patience = atof(value);
		else if (!strcmp(argv[i], "--buttons")) buttons = value;
		else return usage();
		i++;
	}
	if (line < 0 || port <= 0 || port > 65535 || toggle <= 0 || program <= toggle || purge <= program ||
	    window <= 0 || w.confirm <= 0 || w.apply <= w.confirm || w.patience <= 0)
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
		struct pollfd fds[2 + KEY_SENDERS];
		int at[KEY_SENDERS], listening = -1, count = 1, open_before = w.listener >= 0;

		fds[0] = (struct pollfd){ .fd = button, .events = POLLIN };
		if (open_before) {
			if (sender_free(&w)) {
				listening = count;
				fds[count++] = (struct pollfd){ .fd = w.listener, .events = POLLIN };
			}
			for (i = 0; i < KEY_SENDERS; i++) {
				at[i] = w.senders[i].fd >= 0 ? count : -1;
				if (at[i] >= 0)
					fds[count++] = (struct pollfd){ .fd = w.senders[i].fd, .events = POLLIN };
			}
		}
		got = poll(fds, (nfds_t)count, open_before ? 100 : -1);
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
				if (w.pending) {		/* the hand answers for the key that came */
					say("confirm", t, "-");
				} else if (w.listener >= 0) {	/* a press ends the wait, and means no more */
					wait_end(&w, NULL);
					ignore = 1;
					say("closed", t, "-");
				} else if (!ignore) {
					slot_state(word, sizeof(word));
					say("held", t, word);
				}
			} else if (change == 0 && down && w.pending) {
				down = 0;
				if (t - pressed >= w.confirm) {
					wait_keep(&w);
				} else {
					wait_end(&w, "error: the key was discarded: the button was pressed, not held");
					say("discarded", t, "-");
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
					if (slot_run("toggle", NULL, answer, sizeof(answer))) {
						say("failed", now(), "-");
					} else if (!strncmp(answer, "on", 2) || !strncmp(answer, "off", 3)) {
						say(!strncmp(answer, "on", 2) ? "on" : "off", now(), "-");
					} else {	/* nothing to switch: what had filled runs out */
						snprintf(answer, sizeof(answer), "%.3f empty", t - pressed);
						say("drain", t, answer);
					}
					break;
				case KEY_ACT_PURGE:
					if (slot_run("purge", NULL, answer, sizeof(answer)))
						say("failed", now(), "-");
					else
						say(!strncmp(answer, "purged", 6) ? "purged" : "empty", now(), "-");
					break;
				case KEY_ACT_PROGRAM:
					w.listener = listen_on(address, port);
					if (w.listener < 0) {
						say("failed", t, "-");
						break;
					}
					w.until = t + window;
					say("window", t, "-");
					break;
				}
			} else if (change < 0 && buttons) {
				break;	/* the test's presses are over */
			}
		}
		if (!open_before || w.listener < 0)	/* what was polled beside the button is gone, or was not there */
			continue;
		if (got > 0 && listening >= 0 && (fds[listening].revents & POLLIN))
			wait_accept(&w, t);
		for (i = 0; i < KEY_SENDERS; i++) {
			struct sender *s = &w.senders[i];
			int whole = 0;

			if (s->fd < 0)
				continue;
			if (got > 0 && at[i] >= 0 && fds[at[i]].revents)
				whole = sender_line(s);
			if (whole || t >= s->until)
				sender_judge(&w, s, whole > 0, t);
		}
		if (w.pending && down && t - pressed >= w.apply) {	/* held long enough: no need to let go */
			wait_keep(&w);
			ignore = 1;
		} else if (w.pending && !down && t >= w.key_until) {
			wait_end(&w, "error: the key was discarded: nobody held the button for it in time");
			say("discarded", now(), "-");
		} else if (!w.pending && now() >= w.until) {
			wait_end(&w, NULL);
			say("closed", now(), "-");
		}
	}
	wait_end(&w, NULL);
	say("none", now(), "-");
	return 0;
}
