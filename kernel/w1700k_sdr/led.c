// SPDX-License-Identifier: GPL-2.0-only
/*
 * The front status LED: four emitters (red, green, blue, white), each on an AN7581 GPIO,
 * active-low, dimmed by the SoC's waveform generators ("GPIO flash mode": 8 generators, each a
 * period in 4 ms ticks and a high and a low time in 1/255ths of it, and a map of 16 entries
 * that gives a pin one of them). Once its bit in FLASH_MODE_CFG_EXT is set, GPIO g of 16..31
 * follows map entry 8 + (g & 7): GPIO 17, 19, 20 and 29 follow entries 9, 11, 12 and 13.
 * [measured on a unit by eye: with those entries each colour shows alone, bright at level 255
 * and dim at 10; entries g - 16 move nothing but the red, whose two candidates coincide.]
 * Each pin lights its colour when low. Reading GPIO_DATA does not show the waveform, with the
 * pin's output enabled or not: only what was last written.
 *
 * One generator per emitter, at the shortest period (4 ms, 250 Hz), so a level is a duty of
 * 0..255. The LED keeps what it was last given across a module reload. Nothing here touches
 * the MT7996: the LED works whatever the radio does.
 *
 * The block is the kernel's pin controller's too (pinctrl-airoha: a GPIO's direction and
 * output enable are bits of GPIO_CTRL1 and GPIO_OE, words it shares with the LED's pins), so
 * it is reached as that driver reaches it, through the system controller's register map and
 * under its lock. Mapped a second time and written with a read and a write of our own, a
 * change of ours could undo one the pin controller made in between: the reset button's line
 * is asked for (w1700k-key) at about the moment the LED's pins are first taken.
 */
#include <linux/bits.h>
#include <linux/err.h>
#include <linux/errno.h>
#include <linux/mfd/syscon.h>
#include <linux/of.h>
#include <linux/printk.h>
#include "led.h"
#include "hw/an7581/memmap.h"
#include "hw/an7581/scu.h"
#include "hw/an7581/w1700k_board.h"

/* The generators (AN7581 "flash" registers, in the GPIO block; names as in pwm-airoha.c). */
#define LED_FLASH_PRD_SET(bucket)	(0x3cu + 4u * ((bucket) / 2u))	/* 16 bits a bucket: low << 8 | high */
#define LED_FLASH_MAP(entry)		(0x4cu + 4u * ((entry) / 8u))	/* 4 bits an entry: enable, bucket */
#define LED_FLASH_MAP_EN		BIT(3)
#define LED_CYCLE_CFG(bucket)		(0x98u + 4u * ((bucket) / 4u))	/* 8 bits a bucket: period, 4 ms ticks */
#define LED_PERIOD_TICKS		1u
#define LED_MAP_ENTRY(gpio)		(8u + ((gpio) & 7u))	/* GPIO 16..31 */

static const u8 led_gpio[4] = {		/* bucket k drives emitter k */
	W1700K_GPIO_LED_STATUS_RED, W1700K_GPIO_LED_STATUS_GREEN,
	W1700K_GPIO_LED_STATUS_BLUE, W1700K_GPIO_LED_STATUS_WHITE,
};

/* The block's register map, found as the pin controller finds its own: it is the map of that
 * driver's parent node. The parent has no name to look it up by that every device tree gives
 * it (the kernel's own calls it "airoha,en7581-gpio-sysctl", the one this unit boots with only
 * "syscon"); the pin controller's node is the same in both. */
#define LED_PIN_CONTROLLER	"airoha,en7581-pinctrl"

static struct regmap *led_block(void)
{
	struct device_node *pins = of_find_compatible_node(NULL, NULL, LED_PIN_CONTROLLER);
	struct device_node *block = pins ? of_get_parent(pins) : NULL;
	struct regmap *map = block ? device_node_to_regmap(block) : ERR_PTR(-ENODEV);

	of_node_put(block);
	of_node_put(pins);
	return map;
}

static void led_update(struct sdr_led *led, u32 offset, u32 mask, u32 value)
{
	regmap_update_bits(led->map, offset, mask, value);
}

/* A generator's times for a brightness: the pin is low (lit) for level/255 of the period. */
static u32 led_times(u8 level)
{
	return (u32)level << 8 | (255u - level);
}

/* The pins to outputs under their generators; called with the first levels already written. */
static void led_take_pins(struct sdr_led *led)
{
	unsigned int k, n, e;

	for (k = 0; k < 4; k++) {
		n = led_gpio[k] - 16;
		e = LED_MAP_ENTRY(led_gpio[k]);
		led_update(led, LED_CYCLE_CFG(k), 0xffu << (8 * (k % 4)), LED_PERIOD_TICKS << (8 * (k % 4)));
		led_update(led, LED_FLASH_MAP(e), 0xfu << (4 * (e % 8)), (LED_FLASH_MAP_EN | k) << (4 * (e % 8)));
		led_update(led, AN7581_GPIO_CTRL1, 3u << (2 * n), 1u << (2 * n));
		led_update(led, AN7581_GPIO_OE, BIT(led_gpio[k]), BIT(led_gpio[k]));
		led_update(led, AN7581_GPIO_FLASH_MODE_CFG_EXT, BIT(n), BIT(n));
	}
}

int sdr_led_init(struct sdr_led *led)
{
	BUILD_BUG_ON(W1700K_GPIO_LED_STATUS_RED < 16 || W1700K_GPIO_LED_STATUS_RED > 31 ||
		     W1700K_GPIO_LED_STATUS_GREEN < 16 || W1700K_GPIO_LED_STATUS_GREEN > 31 ||
		     W1700K_GPIO_LED_STATUS_BLUE < 16 || W1700K_GPIO_LED_STATUS_BLUE > 31 ||
		     W1700K_GPIO_LED_STATUS_WHITE < 16 || W1700K_GPIO_LED_STATUS_WHITE > 31);
	mutex_init(&led->lock);
	led->taken = false;
	led->map = led_block();
	if (IS_ERR(led->map)) {
		int ret = PTR_ERR(led->map);

		led->map = NULL;
		pr_warn("w1700k_sdr: no status LED: the GPIO block's register map was not found (%d)\n", ret);
		return ret;
	}
	return 0;
}

void sdr_led_exit(struct sdr_led *led)
{
	led->map = NULL;	/* the map is the system controller's, and stays */
}

int sdr_led_set(struct sdr_led *led, const struct w1700k_sdr_led *req)
{
	if (!led->map)
		return -ENODEV;
	mutex_lock(&led->lock);
	regmap_write(led->map, LED_FLASH_PRD_SET(0), led_times(req->green) << 16 | led_times(req->red));
	regmap_write(led->map, LED_FLASH_PRD_SET(2), led_times(req->white) << 16 | led_times(req->blue));
	if (!led->taken) {
		led_take_pins(led);
		led->taken = true;
	}
	mutex_unlock(&led->lock);
	return 0;
}
