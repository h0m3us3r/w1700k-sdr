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
	LED_RAINBOW,	/* the hue goes once round each `period` */
};

struct led_look {
	enum led_pattern pattern;
	struct hsb colour;
	float period, depth;
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
	case LED_RAINBOW:
		c.h = fmodf(c.h + 360.0f * (float)phase, 360.0f);
		break;
	case LED_STEADY:
		break;
	}
	return c;
}

static inline int led_look_same(const struct led_look *a, const struct led_look *b)
{
	return a->pattern == b->pattern && a->colour.h == b->colour.h && a->colour.s == b->colour.s &&
	       a->colour.b == b->colour.b && a->period == b->period && a->depth == b->depth;
}

/* ---- the vocabulary ----
 *
 *   starting            the hue once round in LED_BOOT_SECONDS, brightening from dark
 *   no stream link      yellow, breathing deeply: waiting for the cable or the far end
 *   link, radio idle    white, low and steady: powered, reachable, nothing loaded
 *   radio ready         green, steady: loaded and tuned, not streaming
 *   streaming           a colour for the sample rate, cyan through blue and violet to magenta
 *                       as it widens (40, 80, 160, 320 Msps): solid when every sample is carried
 *                       bit for bit, breathing when they are reduced to fewer bits (a paler
 *                       shade for that was tried and could not be told from the pure one), and
 *                       blinking slowly, lit as long as dark, when nothing leaves the port
 *   warning             yellow blinking: the radio is loaded but a synthesizer is out of lock
 *   fault               red blinking: the stream ended by itself (the unit's own record of it)
 *
 * Green, yellow and red mean go, look and stop wherever they appear; the stream's colours keep
 * clear of them. A change of state fades over LED_FADE_SECONDS; a new instruction from the host
 * (the state record rewritten) shows as a short lift towards white.
 */
#define LED_BOOT_SECONDS	3.0
#define LED_FADE_SECONDS	0.6
#define LED_ACK_SECONDS		0.25

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
	static const struct led_look fault = { LED_BLINK, { 0, 1, 1 }, 1.0f, 0.5f };
	static const struct led_look no_link = { LED_BREATHE, { 50, 1, 0.9f }, 3.0f, 0.85f };
	static const struct led_look warning = { LED_BLINK, { 50, 1, 0.9f }, 1.2f, 0.35f };
	static const struct led_look idle = { LED_STEADY, { 0, 0, 0.35f }, 0, 0 };
	static const struct led_look ready = { LED_STEADY, { 120, 1, 0.7f }, 0, 0 };
	struct led_look stream = { LED_STEADY, { 185, 1, 0.9f }, 0, 0 };

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
	stream.colour.h = s->rate_msps >= 320 ? 300 : s->rate_msps >= 160 ? 265 : s->rate_msps >= 80 ? 225 : 185;
	if (!s->flowing) {
		stream.pattern = LED_BLINK;
		stream.period = 2.0f;
		stream.depth = 0.5f;
	} else if (s->reduced) {
		stream.pattern = LED_BREATHE;
		stream.period = 3.0f;
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
};

static inline void led_engine_start(struct led_engine *e, double t)
{
	static const struct led_look boot = { LED_RAINBOW, { 0, 1, 1 }, (float)LED_BOOT_SECONDS, 0 };

	e->from = e->to = boot;
	e->changed = e->started = t;
	e->ack = -1;
}

static inline void led_engine_show(struct led_engine *e, const struct led_look *look, double t)
{
	if (led_look_same(&e->to, look))
		return;
	/* mid-fade, the old target is the nearest thing to what is showing */
	e->from = e->to;
	e->to = *look;
	e->changed = t;
}

static inline struct hsb led_engine_at(const struct led_engine *e, double t)
{
	float fade = led_clamp((float)((t - e->changed) / LED_FADE_SECONDS));
	struct hsb c = hsb_blend(led_look_at(&e->from, t), led_look_at(&e->to, t), fade * fade * (3 - 2 * fade));
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

#endif
