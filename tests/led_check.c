// SPDX-License-Identifier: MIT
/*
 * led_check levels H S B           the four emitters' levels for a colour: "R G B W"
 * led_check blend H S B H S B T    the colour a fraction T from the first to the second: "H S B"
 * led_check look FILE LINK FLOWING ENDED
 *                                  the look for the state record in FILE (- for none) and what
 *                                  the unit sees itself: "PATTERN H S B PERIOD DEPTH turn SECONDS"
 * led_check engine                 a change of look and an acknowledgement, sampled: one
 *                                  "T H S B" line per 50 ms from 0 to 5 s
 * led_check heard T...             the host rewrites the state record at each time T: the
 *                                  times a lift begins, on one line
 * led_check rise                   the idle look's climb, left twice and come back to: one
 *                                  "T H S B" line per 50 ms from 0 to 12 s
 * kernel/w1700k_sdr/ledd.h built for the host, for tests/test_led.py.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ledd.h"

static struct hsb colour(char **argv)
{
	struct hsb c = { (float)atof(argv[0]), (float)atof(argv[1]), (float)atof(argv[2]) };

	return c;
}

int main(int argc, char **argv)
{
	static const char *const patterns[] = { "steady", "breathe", "blink", "rise" };
	static char text[8192];

	if (argc == 5 && !strcmp(argv[1], "levels")) {
		struct led_levels l = hsb_to_levels(colour(argv + 2));

		printf("%u %u %u %u\n", l.red, l.green, l.blue, l.white);
	} else if (argc == 9 && !strcmp(argv[1], "blend")) {
		struct hsb c = hsb_blend(colour(argv + 2), colour(argv + 5), (float)atof(argv[8]));

		printf("%.3f %.4f %.4f\n", c.h, c.s, c.b);
	} else if (argc == 6 && !strcmp(argv[1], "look")) {
		struct led_status s = { .link = atoi(argv[3]), .flowing = atoi(argv[4]) };
		struct led_look look;
		FILE *f = strcmp(argv[2], "-") ? fopen(argv[2], "r") : NULL;

		if (f) {
			text[fread(text, 1, sizeof(text) - 1, f)] = 0;
			fclose(f);
		}
		led_parse_state(f ? text : NULL, &s);
		s.ended = s.streaming && atoi(argv[5]);
		look = led_look_for(&s);
		printf("%s %.0f %.2f %.2f %.2f %.2f turn %.0f | loaded %d streaming %d reduced %d rate %d unlocked %d\n",
		       patterns[look.pattern], look.colour.h, look.colour.s, look.colour.b, look.period, look.depth,
		       look.turn,
		       s.loaded, s.streaming, s.reduced, s.rate_msps, s.unlocked);
	} else if (argc == 2 && !strcmp(argv[1], "engine")) {
		const struct led_look green = { LED_STEADY, { 120, 1, 0.7f }, 0, 0, 0 };
		const struct led_look blue = { LED_STEADY, { 225, 1, 0.9f }, 0, 0, 0 };
		struct led_engine e;
		struct hsb c;
		int k;

		led_engine_start(&e, 0);
		for (k = 0; k <= 100; k++) {
			double t = k * 0.05;

			if (k == 60)		/* 3.0 s: the greeting is over */
				led_engine_show(&e, &green, t);
			if (k == 76)		/* 3.8 s */
				led_engine_show(&e, &blue, t);
			if (k == 92)		/* 4.6 s: the host speaks */
				e.ack = t;
			c = led_engine_at(&e, t);
			printf("%.2f %.3f %.4f %.4f\n", t, c.h, c.s, c.b);
		}
	} else if (argc >= 3 && !strcmp(argv[1], "heard")) {
		struct led_engine e;
		double last = -1;
		int k;

		led_engine_start(&e, 0);
		for (k = 2; k < argc; k++) {
			led_engine_heard(&e, atof(argv[k]));
			if (e.ack != last)
				printf("%s%.2f", last < 0 ? "" : " ", e.ack);
			last = e.ack;
		}
		printf("\n");
	} else if (argc == 2 && !strcmp(argv[1], "rise")) {
		const struct led_status idle_state = { .link = 1 }, ready_state = { .link = 1, .loaded = 1 };
		const struct led_look idle = led_look_for(&idle_state), green = led_look_for(&ready_state);
		struct led_engine e;
		struct hsb c;
		int k;

		led_engine_start(&e, -10);	/* long since: the greeting is over */
		led_engine_show(&e, &green, -5);
		for (k = 0; k <= 240; k++) {
			double t = k * 0.05;

			if (k == 0)		/* 0 s: idle, the climb begins */
				led_engine_show(&e, &idle, t);
			if (k == 40)		/* 2 s: left part of the way up */
				led_engine_show(&e, &green, t);
			if (k == 80)		/* 4 s: back, and on from there */
				led_engine_show(&e, &idle, t);
			if (k == 160)		/* 8 s: left long after it finished */
				led_engine_show(&e, &green, t);
			if (k == 200)		/* 10 s: back, from low again */
				led_engine_show(&e, &idle, t);
			c = led_engine_at(&e, t);
			printf("%.2f %.3f %.4f %.4f\n", t, c.h, c.s, c.b);
		}
	} else {
		return 2;
	}
	return 0;
}
