/* SPDX-License-Identifier: GPL-2.0-only */
/* The unit's front status LED: four emitters on SoC GPIOs, dimmed by the SoC's waveform
 * generators (led.c).
 */
#ifndef W1700K_SDR_LED_H
#define W1700K_SDR_LED_H
#include <linux/mutex.h>
#include <linux/regmap.h>
#include "w1700k_sdr.h"

struct sdr_led {
	struct regmap *map;	/* the AN7581 GPIO block, as the pin controller has it; NULL: no LED */
	struct mutex lock;
	bool taken;		/* the pins are under their generators */
};

int sdr_led_init(struct sdr_led *led);
void sdr_led_exit(struct sdr_led *led);
int sdr_led_set(struct sdr_led *led, const struct w1700k_sdr_led *req);
#endif
