/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * What the front status LED says, and how: the vocabulary of w1700k-led (ledd.c), kept apart from
 * its I/O so that tests/test_led.py can run it (tests/led_check.c).
 *
 * Colours are hue, saturation and brightness. Everything that mixes or fades them does it there;
 * hsb_to_levels, the last step, alone turns a colour into the four emitters' levels.
 */
#ifndef W1700K_LEDD_H
#define W1700K_LEDD_H
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "keyd.h"

struct hsb {
	float h;	/* hue, degrees: 0 red, 60 yellow, 120 green, 180 cyan, 240 blue, 300 magenta */
	float s;	/* 0 white .. 1 the pure hue */
	float b;	/* 0 dark .. 1 full, as the eye judges it */
};

struct led_levels {
	uint8_t red, green, blue, white;	/* each emitter, 0..255 */
};

/* Brightness is perceptual: the light is b ^ LED_GAMMA of full. */
#define LED_GAMMA 2.2f

static inline float led_clamp(float v)
{
	return v < 0 ? 0 : v > 1 ? 1 : v;
}

/* The colour a fraction t of the way from a to c: hue along the shorter way round, saturation and
 * brightness straight. White and black have no hue of their own, so they take the other's: a fade
 * up from dark or in from white does not pass through colours neither end has. */
static inline struct hsb hsb_blend(struct hsb a, struct hsb c, float t)
{
	struct hsb out;
	float d;

	t = led_clamp(t);
	if (a.s <= 0 || a.b <= 0)
		a.h = c.h;
	if (c.s <= 0 || c.b <= 0)
		c.h = a.h;
	d = fmodf(c.h - a.h + 540.0f, 360.0f) - 180.0f;
	out.h = fmodf(a.h + d * t + 360.0f, 360.0f);
	out.s = a.s + (c.s - a.s) * t;
	out.b = a.b + (c.b - a.b) * t;
	return out;
}

/* The only way out of HSB: the hue's mix of red, green and blue at full strength, what all three
 * share handed to the white emitter (so a pale colour is the hue plus white, and white is the
 * white emitter alone), all of it scaled by the brightness. */
static inline struct led_levels hsb_to_levels(struct hsb c)
{
	float h = fmodf(fmodf(c.h, 360.0f) + 360.0f, 360.0f) / 60.0f, s = led_clamp(c.s);
	float light = powf(led_clamp(c.b), LED_GAMMA), x = 1.0f - fabsf(fmodf(h, 2.0f) - 1.0f);
	float r = 0, g = 0, b = 0, w = 1.0f - s;
	struct led_levels out;

	switch ((int)h) {
	case 0: r = 1, g = x; break;
	case 1: r = x, g = 1; break;
	case 2: g = 1, b = x; break;
	case 3: g = x, b = 1; break;
	case 4: r = x, b = 1; break;
	default: r = 1, b = x; break;
	}
	out.red = (uint8_t)lroundf(255.0f * r * s * light);
	out.green = (uint8_t)lroundf(255.0f * g * s * light);
	out.blue = (uint8_t)lroundf(255.0f * b * s * light);
	out.white = (uint8_t)lroundf(255.0f * w * light);
	return out;
}

/* ---- looks: a colour in time ---- */
enum led_pattern {
	LED_STEADY,
	LED_BREATHE,	/* the brightness sinks by `depth` of itself and returns, once a `period` */
	LED_BLINK,	/* lit for `depth` of each `period`, dark for the rest */
	LED_RISE,	/* the brightness climbs from 1 - `depth` of itself to all of it over one
			 * `period`, once, and stays: t is then the time it has been climbing */
};

struct led_look {
	enum led_pattern pattern;	/* what the brightness does */
	struct hsb colour;
	float period, depth;
	float turn;	/* seconds for the hue to go once round the circle from `colour`; 0: it stays */
};

static inline struct hsb led_look_at(const struct led_look *l, double t)
{
	struct hsb c = l->colour;
	double phase = l->period > 0 ? fmod(t, l->period) / l->period : 0;

	switch (l->pattern) {
	case LED_BREATHE:
		c.b *= 1.0f - l->depth * 0.5f * (1.0f - cosf(6.2831853f * (float)phase));
		break;
	case LED_BLINK:
		if (phase >= l->depth)
			c.b = 0;
		break;
	case LED_RISE:
		c.b *= 1.0f - l->depth * (1.0f - led_clamp(l->period > 0 ? (float)(t / l->period) : 1.0f));
		break;
	case LED_STEADY:
		break;
	}
	if (l->turn > 0)
		c.h = fmodf(c.h + 360.0f * (float)(fmod(t, l->turn) / l->turn), 360.0f);
	return c;
}

static inline int led_look_same(const struct led_look *a, const struct led_look *b)
{
	return a->pattern == b->pattern && a->colour.h == b->colour.h && a->colour.s == b->colour.s &&
	       a->colour.b == b->colour.b && a->period == b->period && a->depth == b->depth &&
	       a->turn == b->turn;
}

/* ---- the vocabulary ----
 *
 *   starting            the hue once round in LED_BOOT_SECONDS, brightening from dark
 *   no stream link      yellow, breathing deeply: waiting for the cable or the far end
 *   link, radio idle    white, brightening from low to full over LED_RISE_SECONDS and staying
 *                       there, like a bar filling: powered, reachable, the radio not loaded (or
 *                       being loaded). The next state takes over the moment it comes; a climb
 *                       left unfinished goes on from where it was when the state returns, and one
 *                       that had finished starts again from low
 *   radio ready         green, steady: loaded and tuned, not streaming
 *   streaming           a colour for the sample rate: cyan, blue and magenta for 40, 80 and
 *                       160 Msps, a third of the circle between each and none nearer than that
 *                       to green or red; and at 320 Msps, everything the receiver has, every
 *                       colour: the hue going round once in LED_RAINBOW_SECONDS (4, 6 and 8 s
 *                       were tried on a unit; 6 was chosen). The brightness says the rest:
 *                       solid when every sample is carried bit for bit, breathing when they are
 *                       reduced to fewer bits (a paler shade for that was tried and could not be
 *                       told from the pure one), and blinking slowly, lit as long as dark, when
 *                       nothing leaves the port
 *   warning             yellow blinking: the radio is loaded but a synthesizer is out of lock
 *   fault               red blinking: the stream ended by itself (the unit's own record of it)
 *
 * Green, yellow and red mean go, look and stop wherever they stand still or blink on their own;
 * a stream's fixed colours keep clear of them, and the rainbow only passes through. A change of state fades over LED_FADE_SECONDS; a new instruction from the host
 * (the state record rewritten, however many times within LED_ACK_QUIET) shows as one short
 * lift towards white.
 */
#define LED_BOOT_SECONDS	3.0
#define LED_RAINBOW_SECONDS	6.0f
#define LED_FADE_SECONDS	0.6
#define LED_RISE_SECONDS	6.0f
#define LED_ACK_SECONDS		0.25
#define LED_ACK_QUIET		1.5	/* one lift for all the host writes within this long */

struct led_status {
	int link;	/* the stream port has carrier */
	int loaded;	/* the radio runs our firmware */
	int streaming;	/* a stream was started (the state record has its packing) */
	int ended;	/* ... and its pump has ended by itself */
	int unlocked;	/* the captured band's synthesizer is out of lock */
	int flowing;	/* bytes are leaving the stream port */
	int reduced;	/* the packing carries fewer than 14 bits a component: the colour breathes */
	int rate_msps;	/* the captured band's sample rate */
};

static inline struct led_look led_look_for(const struct led_status *s)
{
	static const struct led_look fault = { LED_BLINK, { 0, 1, 1 }, 1.0f, 0.5f, 0 };
	static const struct led_look no_link = { LED_BREATHE, { 50, 1, 0.9f }, 3.0f, 0.85f, 0 };
	static const struct led_look warning = { LED_BLINK, { 50, 1, 0.9f }, 1.2f, 0.35f, 0 };
	static const struct led_look idle = { LED_RISE, { 0, 0, 1.0f }, LED_RISE_SECONDS, 0.65f, 0 };
	static const struct led_look ready = { LED_STEADY, { 120, 1, 0.7f }, 0, 0, 0 };
	struct led_look stream = { LED_STEADY, { 180, 1, 0.9f }, 0, 0, 0 };

	if (s->streaming && s->ended)
		return fault;
	if (!s->link)
		return no_link;
	if (s->loaded && s->unlocked)
		return warning;
	if (!s->loaded)
		return idle;
	if (!s->streaming)
		return ready;
	if (s->rate_msps >= 320) {
		stream.colour.h = 0;
		stream.turn = LED_RAINBOW_SECONDS;
	} else {
		stream.colour.h = s->rate_msps >= 160 ? 300 : s->rate_msps >= 80 ? 240 : 180;
	}
	if (!s->flowing) {
		stream.pattern = LED_BLINK;
		stream.period = 2.0f;
		stream.depth = 0.5f;
	} else if (s->reduced) {
		stream.pattern = LED_BREATHE;
		/* Under the rainbow a breath of half its turn would dim the same two colours every
		 * time; at 2.5 s every colour has its turn at full brightness (30 s for all). */
		stream.period = stream.turn > 0 ? 2.5f : 3.0f;
		stream.depth = 0.65f;
	}
	return stream;
}

/* ---- the controller's state record (host/w1700k/controller.py _save, JSON) ---- */
static inline const char *led_json_value(const char *json, const char *key)
{
	const char *at = json ? strstr(json, key) : NULL;

	return at ? at + strlen(key) : NULL;
}

/* loaded, streaming, reduced, rate_msps and unlocked from the record; the rest of *s is kept. */
static inline void led_parse_state(const char *json, struct led_status *s)
{
	const char *v, *bands;
	char key[24];
	int band = 0, k;

	s->loaded = s->streaming = s->reduced = s->unlocked = 0;
	s->rate_msps = 0;
	v = led_json_value(json, "\"loaded\": ");
	s->loaded = v && !strncmp(v, "true", 4);
	v = led_json_value(json, "\"band\": ");
	if (v)
		band = atoi(v);
	v = led_json_value(json, "\"packing\": ");
	if (v && *v == '"') {
		s->streaming = 1;
		s->reduced = !strncmp(v, "\"split\"", 7) || (v[1] >= '0' && v[1] <= '9');
	}
	bands = led_json_value(json, "\"bands\": {");
	snprintf(key, sizeof(key), "\"%d\": {", band);
	v = led_json_value(led_json_value(bands, key), "\"sample_rate\": ");
	if (v)
		s->rate_msps = (int)(atof(v) / 1e6 + 0.5);
	v = led_json_value(json, "\"synth_locked\": [");
	for (k = 0; v && k < band; k++) {	/* the band's element */
		v = strchr(v, ',');
		v = v ? v + 1 : NULL;
	}
	while (v && *v == ' ')
		v++;
	s->unlocked = s->loaded && v && !strncmp(v, "false", 5);
}

/* ---- the engine: the look now, the one it is fading to, and the host's last instruction ---- */
struct led_engine {
	struct led_look from, to;
	double changed;		/* when `to` was set */
	double ack;		/* when the host last spoke; < 0: never */
	double started;
	double risen;		/* a LED_RISE look: how long `to` had climbed before `changed` */
	double from_risen;	/* and how long `from` had when it was left */
};

/* A look's own time: a climb counts the seconds it has been climbing, the rest the clock. */
static inline double led_engine_time(const struct led_look *look, double climbed, double t)
{
	return look->pattern == LED_RISE ? climbed : t;
}

static inline void led_engine_start(struct led_engine *e, double t)
{
	static const struct led_look boot = { LED_STEADY, { 0, 1, 1 }, 0, 0, (float)LED_BOOT_SECONDS };

	e->from = e->to = boot;
	e->changed = e->started = t;
	e->ack = -1;
	e->risen = e->from_risen = 0;
}

/* The host has just rewritten the state record. One operation rewrites it several times (that
 * it has begun, what it changed, that it is done): they are one instruction, and get one lift.
 */
static inline void led_engine_heard(struct led_engine *e, double t)
{
	if (e->ack < 0 || t - e->ack >= LED_ACK_QUIET)
		e->ack = t;
}

static inline void led_engine_show(struct led_engine *e, const struct led_look *look, double t)
{
	if (led_look_same(&e->to, look))
		return;
	/* mid-fade, the old target is the nearest thing to what is showing */
	e->from = e->to;
	if (e->from.pattern == LED_RISE) {
		/* Left where it stands. Unfinished, it goes on from there the next time; finished,
		 * the next one starts again from low. */
		e->from_risen = e->risen + (t - e->changed);
		e->risen = e->from_risen < e->from.period ? e->from_risen : 0;
	}
	e->to = *look;
	e->changed = t;
}

static inline struct hsb led_engine_at(const struct led_engine *e, double t)
{
	float fade = led_clamp((float)((t - e->changed) / LED_FADE_SECONDS));
	struct hsb c = hsb_blend(led_look_at(&e->from, led_engine_time(&e->from, e->from_risen, t)),
				 led_look_at(&e->to, led_engine_time(&e->to, e->risen + (t - e->changed), t)),
				 fade * fade * (3 - 2 * fade));
	struct hsb lifted;
	float up;

	if (t - e->started < 1.0)	/* out of the dark */
		c.b *= (float)(t - e->started);
	if (e->ack >= 0 && t - e->ack < LED_ACK_SECONDS) {	/* up towards white and back */
		up = 1.0f - fabsf((float)((t - e->ack) / LED_ACK_SECONDS) * 2.0f - 1.0f);
		lifted = c;
		lifted.s = 0;
		lifted.b = 1;
		c = hsb_blend(c, lifted, 0.6f * up);
	}
	return c;
}

/* ---- the button: what the LED says while a hand on the unit speaks through the paperclip ----
 *
 * w1700k-key (keyd.c) writes what the button is doing, one line: a word, the time it began (the
 * monotonic clock both programs read) and a detail. While that goes on it is all the LED shows,
 * over whatever the receiver is doing, and the receiver's look returns when it is over.
 *
 *   held      for KEY_TOGGLE_SECONDS the LED says what letting go at their end will do to the
 *             slot's key, in colour and in direction (red against green is the colour blindness
 *             most people with one have): green rising out of the dark, like the bar of the idle
 *             look, when the key will be switched on; red sinking from full when it will be
 *             switched off; white rising when the slot is empty and there is nothing to switch.
 *             Then a flash, and the same colour pulsing: letting go now does it. After KEY_PROGRAM_SECONDS it flashes again and
 *             pulses blue, faster: letting go now opens the slot for a new key. After
 *             KEY_PURGE_SECONDS a third flash, and red blinking hard: letting go now erases the
 *             slot's key (pale again where there is none)
 *   drain     let go before anything: what had filled runs out, nothing happened
 *   on, off   switched: green, twice up and staying; red, sinking into the dark
 *   window    blue, breathing: the unit waits for a key
 *   taken     the key is in: a flash, then the blue turns through cyan to green and stays a moment
 *   closed    the wait ended without a key: the blue goes out
 *   purged    erased: the red pales to white as it sinks into the dark
 *   failed    red, three times: the key could not be kept
 *
 * Green and red keep their meaning (go, stop); blue is the colour of waiting to be paired.
 */
enum led_key_phase {
	LED_KEY_NONE,
	LED_KEY_HELD,
	LED_KEY_DRAIN,
	LED_KEY_ON,
	LED_KEY_OFF,
	LED_KEY_EMPTY,
	LED_KEY_WINDOW,
	LED_KEY_TAKEN,
	LED_KEY_CLOSED,
	LED_KEY_FAILED,
	LED_KEY_PURGED,
};

struct led_key {
	enum led_key_phase phase;
	double since;		/* when the phase began */
	enum key_slot slot;	/* held, drain: the slot as it is */
	double held;		/* drain: how long the button had been down */
};

#define LED_KEY_HUE_ON		120.0f
#define LED_KEY_HUE_OFF		0.0f
#define LED_KEY_HUE_PROGRAM	225.0f
#define LED_KEY_FLASH		0.12	/* seconds of white where a threshold is crossed */
#define LED_KEY_HELD_MOST	60.0	/* a button down for longer says nothing more */

/* "PHASE SINCE [DETAIL]"; anything else is no phase. */
static inline void led_key_parse(const char *text, struct led_key *k)
{
	static const char *const words[] = { "none", "held", "drain", "on", "off", "empty", "window",
					     "taken", "closed", "failed", "purged" };
	char word[16] = "", detail[16] = "", more[16] = "";
	double since = 0;
	unsigned int i;

	memset(k, 0, sizeof(*k));
	if (!text || sscanf(text, "%15s %lf %15s %15s", word, &since, detail, more) < 2)
		return;
	for (i = 1; i < sizeof(words) / sizeof(words[0]); i++)
		if (!strcmp(word, words[i]))
			k->phase = (enum led_key_phase)i;
	k->since = since;
	if (k->phase == LED_KEY_HELD)
		k->slot = key_slot_named(detail);
	else if (k->phase == LED_KEY_DRAIN) {
		k->held = atof(detail);
		k->slot = key_slot_named(more);
	}
}

/* A pulse that begins at its brightest: 1 down to 0 and back, once a period. */
static inline float led_key_pulse(double z, double period)
{
	return 0.5f + 0.5f * cosf(6.2831853f * (float)(fmod(z, period) / period));
}

static inline float led_key_smooth(float x)
{
	x = led_clamp(x);
	return x * x * (3 - 2 * x);
}

/* The first seconds of a hold, `held` in: what letting go at their end will do to the key. */
static inline struct hsb led_key_fill(enum key_slot slot, double held)
{
	float part = led_clamp((float)(held / KEY_TOGGLE_SECONDS));
	struct hsb c = { 0, 0, 0.06f + 0.94f * part };	/* nothing to switch: white, rising */

	if (slot == KEY_SLOT_OFF) {		/* on: green, rising */
		c.h = LED_KEY_HUE_ON;
		c.s = 1;
	} else if (slot == KEY_SLOT_ON) {	/* off: red, sinking */
		c.h = LED_KEY_HUE_OFF;
		c.s = 1;
		c.b = 1.0f - 0.88f * part;
	}
	return c;
}

/* A zone of the hold, `z` seconds in: a flash of white, then the hue, pulsing. */
static inline struct hsb led_key_zone(float hue, float saturation, float low, double z, double period)
{
	struct hsb c = { hue, 0, 1 };

	if (z >= LED_KEY_FLASH) {
		c.s = saturation * led_clamp((float)((z - LED_KEY_FLASH) / 0.15));
		c.b = low + (1.0f - low) * led_key_pulse(z - LED_KEY_FLASH, period);
	}
	return c;
}

/* The button's colour at t, and how much of the LED is its own (0: none, the receiver's look
 * shows; 1: all of it). */
static inline float led_key_at(const struct led_key *k, double t, struct hsb *c)
{
	double z = t - k->since;
	struct hsb white = { 0, 0, 1 };

	*c = white;
	if (z < 0)
		return 0;
	switch (k->phase) {
	case LED_KEY_HELD:
		if (z > LED_KEY_HELD_MOST)	/* not a hand: a record nobody ended */
			return 0;
		if (z < KEY_TOGGLE_SECONDS) {
			*c = led_key_fill(k->slot, z);
			return led_clamp((float)(z / 0.15));
		}
		if (z < KEY_PROGRAM_SECONDS) {
			z -= KEY_TOGGLE_SECONDS;
			if (k->slot == KEY_SLOT_EMPTY) {
				*c = led_key_zone(0, 0, 0.2f, z, 0.8);
				if (z >= LED_KEY_FLASH)
					c->b *= 0.45f;
			} else {
				*c = led_key_zone(k->slot == KEY_SLOT_ON ? LED_KEY_HUE_OFF : LED_KEY_HUE_ON, 1, 0.3f, z, 0.8);
			}
			return 1;
		}
		if (z < KEY_PURGE_SECONDS) {
			*c = led_key_zone(LED_KEY_HUE_PROGRAM, 1, 0.3f, z - KEY_PROGRAM_SECONDS, 0.45);
			return 1;
		}
		z -= KEY_PURGE_SECONDS;
		if (k->slot == KEY_SLOT_EMPTY) {
			*c = led_key_zone(0, 0, 0.2f, z, 0.8);
			if (z >= LED_KEY_FLASH)
				c->b *= 0.45f;
		} else if (z >= LED_KEY_FLASH) {	/* not a pulse: lit and dark, three times a second */
			c->h = LED_KEY_HUE_OFF;
			c->s = 1;
			c->b = fmod(z - LED_KEY_FLASH, 0.3) < 0.15 ? 1.0f : 0.06f;
		}
		return 1;
	case LED_KEY_DRAIN:
		*c = led_key_fill(k->slot, k->held);
		c->b *= 1.0f - led_key_smooth((float)(z / 0.35));
		return 1.0f - led_key_smooth((float)((z - 0.2) / 0.3));
	case LED_KEY_ON:
		c->h = LED_KEY_HUE_ON;
		c->s = 1;
		c->b = z < 0.6 ? 0.25f + 0.75f * (0.5f - 0.5f * cosf(6.2831853f * (float)(z / 0.3)))
			       : 0.25f + 0.75f * led_key_smooth((float)((z - 0.6) / 0.25));
		return 1.0f - led_key_smooth((float)((z - 1.7) / 0.5));
	case LED_KEY_OFF:
		c->h = LED_KEY_HUE_OFF;
		c->s = 1;
		c->b = 1.0f - led_key_smooth((float)(z / 1.3));
		return 1.0f - led_key_smooth((float)((z - 1.6) / 0.6));
	case LED_KEY_EMPTY:
		c->b = 0.3f;
		return 1.0f - led_key_smooth((float)(z / 0.8));
	case LED_KEY_WINDOW:
		if (z > KEY_WINDOW_SECONDS + 5)
			return 0;
		c->h = LED_KEY_HUE_PROGRAM;
		c->s = 1;
		c->b = 0.25f + 0.75f * led_key_pulse(z, 1.6);
		return 1;
	case LED_KEY_TAKEN:
		if (z >= LED_KEY_FLASH) {
			c->h = LED_KEY_HUE_PROGRAM +
			       (LED_KEY_HUE_ON - LED_KEY_HUE_PROGRAM) * led_key_smooth((float)((z - LED_KEY_FLASH) / 0.9));
			c->s = led_clamp((float)((z - LED_KEY_FLASH) / 0.15));
		}
		return 1.0f - led_key_smooth((float)((z - 2.0) / 0.6));
	case LED_KEY_CLOSED:
		c->h = LED_KEY_HUE_PROGRAM;
		c->s = 1;
		c->b = 0.6f * (1.0f - led_key_smooth((float)(z / 0.6)));
		return 1.0f - led_key_smooth((float)((z - 0.5) / 0.5));
	case LED_KEY_PURGED:
		c->h = LED_KEY_HUE_OFF;
		c->s = 1.0f - led_key_smooth((float)(z / 1.0));
		c->b = 1.0f - led_key_smooth((float)((z - 0.3) / 1.3));
		return 1.0f - led_key_smooth((float)((z - 1.7) / 0.5));
	case LED_KEY_FAILED:
		c->h = LED_KEY_HUE_OFF;
		c->s = 1;
		c->b = z < 1.2 && fmod(z, 0.4) < 0.2 ? 1 : 0;
		return 1.0f - led_key_smooth((float)((z - 1.2) / 0.6));
	case LED_KEY_NONE:
		break;
	}
	return 0;
}

/* The LED with the button's say laid over the receiver's look. A dark or white button colour takes
 * the look's hue as it comes and goes, as every fade here does. */
static inline struct hsb led_key_over(struct hsb look, const struct led_key *k, double t)
{
	struct hsb c;
	float own = led_key_at(k, t, &c);

	return own <= 0 ? look : own >= 1 ? c : hsb_blend(look, c, own);
}

#endif
