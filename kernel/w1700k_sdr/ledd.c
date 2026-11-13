// SPDX-License-Identifier: GPL-2.0-only
/*
 * w1700k-led DEVICE [--state FILE] [--done FILE] [--iface IF] [--brightness B]
 * w1700k-led DEVICE --demo [SECONDS]
 * w1700k-led DEVICE --show H S B
 * w1700k-led DEVICE --status
 *
 * The front status LED as the unit's own voice (the vocabulary is in ledd.h). Started at boot and
 * left running: every quarter of a second it looks at what the unit knows of itself, and fifty
 * times a second it renders the look that goes with it.
 *
 *   the stream port's carrier, and whether bytes leave it    /sys/class/net/IF (default eth1)
 *   radio loaded, band, sample rate, packing, lock           FILE, the controller's state record
 *                                                            (default /tmp/w1700k-state.json)
 *   a stream that ended by itself                            the record still names its packing
 *                                                            and --done FILE exists
 *                                                            (default /tmp/w1700k-stream.done)
 *
 * The device is opened for each change of the levels and closed again: a held descriptor would
 * keep the driver from being replaced (`w1700k load`). While the driver is away, nothing is
 * written and the LED keeps what it had.
 *
 * --brightness scales everything (0..1, default 0.8). --demo walks through every look, SECONDS
 * each (default 5), naming them on stdout. --show sets one colour (hue in degrees, saturation
 * and brightness 0..1) and exits. --status prints what the unit knows of itself and the look
 * that goes with it, and leaves the LED alone.
 */
#include "client.h"
#include <signal.h>
#include <sys/stat.h>
#include <time.h>
#include "ledd.h"

#define FRAME_NS 20000000L	/* 50 frames a second */
#define LOOK_EVERY 0.25		/* seconds between looks at the unit's state */
#define FLOWING_BYTES 250000	/* in a quarter second: 8 Mbit/s and more is a stream */

static const char *device;
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

/* The levels to the driver. 0, or -1 while there is no driver to take them. */
static int put(struct led_levels l)
{
	struct w1700k_sdr_led req = { .red = l.red, .green = l.green, .blue = l.blue, .white = l.white };
	int fd = open(device, O_RDWR | O_CLOEXEC), ret;

	if (fd < 0)
		return -1;
	ret = ioctl(fd, W1700K_SDR_LED, &req);
	close(fd);
	return ret ? -1 : 0;
}

static int read_text(const char *path, char *text, size_t size)
{
	FILE *f = fopen(path, "r");
	size_t n;

	if (!f)
		return -1;
	n = fread(text, 1, size - 1, f);
	text[n] = 0;
	fclose(f);
	return 0;
}

struct watch {
	char carrier[96], tx_bytes[96];
	const char *state, *done;
	unsigned long long last_tx;
	time_t state_mtime;
	long state_nsec;
	int seen_state;
};

/* What the unit knows now. Returns 1 when the host has just rewritten the state record. */
static int look(struct watch *w, struct led_status *s)
{
	static char text[8192];
	unsigned long long tx;
	struct stat st;
	int spoke = 0;

	s->link = !read_text(w->carrier, text, sizeof(text)) && text[0] == '1';
	tx = !read_text(w->tx_bytes, text, sizeof(text)) ? strtoull(text, NULL, 10) : 0;
	s->flowing = tx - w->last_tx >= FLOWING_BYTES;
	w->last_tx = tx;
	if (stat(w->state, &st)) {
		led_parse_state(NULL, s);	/* no record: nothing loaded since boot */
		w->seen_state = 0;
	} else if (!w->seen_state || st.st_mtime != w->state_mtime || st.st_mtim.tv_nsec != w->state_nsec) {
		if (!read_text(w->state, text, sizeof(text)))
			led_parse_state(text, s);
		spoke = w->seen_state;	/* not the record found at start */
		w->seen_state = 1;
		w->state_mtime = st.st_mtime;
		w->state_nsec = st.st_mtim.tv_nsec;
	}
	s->ended = s->streaming && !access(w->done, F_OK);
	return spoke;
}

static int report(struct watch *w)
{
	static const char *const patterns[] = { "steady", "breathing", "blinking" };
	const struct timespec quarter = { .tv_nsec = 250000000L };
	struct led_status s = { 0 };
	struct led_look l;

	look(w, &s);
	nanosleep(&quarter, NULL);	/* whether bytes leave the port takes two readings */
	w->seen_state = 0;
	look(w, &s);
	l = led_look_for(&s);
	printf("{\"at\":\"led\",\"link\":%d,\"loaded\":%d,\"streaming\":%d,\"ended\":%d,\"unlocked\":%d"
	       ",\"flowing\":%d,\"reduced\":%d,\"rate_msps\":%d,\"look\":\"%s\",\"hue\":%.0f"
	       ",\"saturation\":%.2f,\"brightness\":%.2f,\"rainbow_seconds\":%.0f}\n",
	       s.link, s.loaded, s.streaming, s.ended, s.unlocked, s.flowing, s.reduced, s.rate_msps,
	       patterns[l.pattern], l.colour.h, l.colour.s, l.colour.b, l.turn);
	return 0;
}

static void frame_sleep(void)
{
	const struct timespec t = { .tv_nsec = FRAME_NS };

	nanosleep(&t, NULL);
}

/* Render the engine until `until` (0: until stopped), looking at the unit's state unless `w` is NULL. */
static void run(struct led_engine *e, struct watch *w, float brightness, double until)
{
	struct led_levels last = { 0 }, l;
	struct led_status status = { 0 };
	struct led_look target;
	double t, looked = 0;
	struct hsb c;
	int first = 1;

	while (!stopping && (!until || now() < until)) {
		t = now();
		if (w && t - looked >= LOOK_EVERY) {
			looked = t;
			if (look(w, &status))
				e->ack = t;
			if (t - e->started >= LED_BOOT_SECONDS) {	/* after the greeting */
				target = led_look_for(&status);
				led_engine_show(e, &target, t);
			}
		}
		c = led_engine_at(e, t);
		c.b *= brightness;
		l = hsb_to_levels(c);
		if (first || memcmp(&l, &last, sizeof(l))) {
			if (!put(l)) {
				last = l;
				first = 0;
			}
		}
		frame_sleep();
	}
}

static void demo(float brightness, double seconds)
{
	static const struct {
		const char *name;
		struct led_status s;
	} states[] = {
		{ "no stream link (yellow, breathing)", { .link = 0 } },
		{ "link up, radio idle (low white)", { .link = 1 } },
		{ "radio ready (green)", { .link = 1, .loaded = 1 } },
		{ "streaming 40 Msps, lossless (cyan, solid)", { .link = 1, .loaded = 1, .streaming = 1, .flowing = 1, .rate_msps = 40 } },
		{ "streaming 80 Msps, lossless (blue)", { .link = 1, .loaded = 1, .streaming = 1, .flowing = 1, .rate_msps = 80 } },
		{ "streaming 160 Msps, lossless (magenta)", { .link = 1, .loaded = 1, .streaming = 1, .flowing = 1, .rate_msps = 160 } },
		{ "streaming 160 Msps, reduced bits (the same colour, breathing)",
		  { .link = 1, .loaded = 1, .streaming = 1, .flowing = 1, .reduced = 1, .rate_msps = 160 } },
		{ "streaming 320 Msps, lossless (the rainbow, steady brightness)", { .link = 1, .loaded = 1, .streaming = 1, .flowing = 1, .rate_msps = 320 } },
		{ "streaming 320 Msps, reduced bits (the rainbow, breathing)",
		  { .link = 1, .loaded = 1, .streaming = 1, .flowing = 1, .reduced = 1, .rate_msps = 320 } },
		{ "streaming but nothing leaving the port (slow blink)",
		  { .link = 1, .loaded = 1, .streaming = 1, .rate_msps = 320 } },
		{ "warning: synthesizer out of lock (yellow blink)", { .link = 1, .loaded = 1, .unlocked = 1 } },
		{ "fault: the stream ended by itself (red blink)",
		  { .link = 1, .loaded = 1, .streaming = 1, .ended = 1, .rate_msps = 160 } },
		{ "radio ready again (green), with the host's acknowledgement", { .link = 1, .loaded = 1 } },
	};
	struct led_engine e;
	struct led_look target;
	unsigned int k;

	led_engine_start(&e, now());
	printf("starting (the hue once round, out of the dark)\n");
	fflush(stdout);
	run(&e, NULL, brightness, now() + LED_BOOT_SECONDS);
	for (k = 0; k < sizeof(states) / sizeof(states[0]) && !stopping; k++) {
		target = led_look_for(&states[k].s);
		led_engine_show(&e, &target, now());
		if (k + 1 == sizeof(states) / sizeof(states[0]))
			e.ack = now() + 1.5;
		printf("%s\n", states[k].name);
		fflush(stdout);
		run(&e, NULL, brightness, now() + seconds);
	}
}

static int usage(void)
{
	fprintf(stderr, "usage: w1700k-led DEVICE [--state FILE] [--done FILE] [--iface IF] [--brightness B]\n"
		"       [--demo [SECONDS] | --show H S B | --status]\n");
	return 2;
}

int main(int argc, char **argv)
{
	struct sigaction sa = { .sa_handler = on_signal };
	static struct watch w = { .state = "/tmp/w1700k-state.json", .done = "/tmp/w1700k-stream.done" };
	const char *iface = "eth1";
	float brightness = 0.8f;
	struct led_engine e;
	struct hsb colour = { 0 };
	double demo_seconds = 0;
	int i, fd, show = 0, status = 0;

	if (argc < 2 || argv[1][0] == '-')
		return usage();
	device = argv[1];
	for (i = 2; i < argc; i++) {
		if (!strcmp(argv[i], "--state") && i + 1 < argc) {
			w.state = argv[++i];
		} else if (!strcmp(argv[i], "--done") && i + 1 < argc) {
			w.done = argv[++i];
		} else if (!strcmp(argv[i], "--iface") && i + 1 < argc) {
			iface = argv[++i];
		} else if (!strcmp(argv[i], "--brightness") && i + 1 < argc) {
			brightness = led_clamp((float)atof(argv[++i]));
		} else if (!strcmp(argv[i], "--demo")) {
			demo_seconds = i + 1 < argc && argv[i + 1][0] != '-' ? atof(argv[++i]) : 5;
		} else if (!strcmp(argv[i], "--status")) {
			status = 1;
		} else if (!strcmp(argv[i], "--show") && i + 3 < argc) {
			show = 1;
			colour = (struct hsb){ (float)atof(argv[i + 1]), (float)atof(argv[i + 2]), (float)atof(argv[i + 3]) };
			i += 3;
		} else {
			return usage();
		}
	}
	fd = client_open(device, O_RDWR, W1700K_SDR_CAP_LED);	/* the driver, and that it has the LED */
	if (fd < 0)
		return 1;
	close(fd);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	if (show)
		return put(hsb_to_levels(colour)) ? client_fail(device) : 0;
	if (demo_seconds > 0) {
		demo(brightness, demo_seconds);
		return 0;
	}
	snprintf(w.carrier, sizeof(w.carrier), "/sys/class/net/%.40s/carrier", iface);
	snprintf(w.tx_bytes, sizeof(w.tx_bytes), "/sys/class/net/%.40s/statistics/tx_bytes", iface);
	if (status)
		return report(&w);
	led_engine_start(&e, now());
	run(&e, &w, brightness, 0);
	return 0;
}
