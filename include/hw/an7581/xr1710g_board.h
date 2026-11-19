/* SPDX-License-Identifier: MIT */
/*
 * Gemtek XR1710G board facts (FCC ID MXF-XR1710G, HW label 1.0): the parts, how
 * they are wired, and what its stock firmware does, beside the W1700K's
 * (w1700k_board.h), whose design it shares.
 *
 * NAMES:
 *   XR1710G_*             this board's own values
 *   XR1710G_GPIO_*        AN7581 GPIO line offsets (scu.h numbering)
 * Everything not named here is the W1700K's and is used under the W1700K's
 * names: the NAND part, its geometry and vendor tables (W1700K_NAND_*,
 * W1700K_BMT_*), the stock partition map, where the identity block and the
 * radio calibration lie, the DRAM size and the load addresses. Each of those
 * was read on a unit and found equal (below).
 *
 * TAGS: [measured] on one unit (HW 1.0, made 2024 week 5), from its stock boot
 * log, a backup and a raw image of its NAND, its stock device tree and a shell
 * in stock's failsafe mode. [static] from a document or another tree, named.
 * [photo] a marking read from a photograph of the board.
 *
 * One set of images runs on both boards. The loader tells them apart at every
 * boot by the serial number in the factory identity and hands the board's name
 * to the system (platform/boards.py).
 *
 * How it differs from the W1700K, all of it:
 *   - no GPS receiver, and GPIO 43, its power line there, is a port LED here;
 *   - the two 1 GbE ports' LEDs are on GPIO 33, 34, 43 and 44, not 9, 10, 27, 28;
 *   - GPIO 44 being an LED, there is no minor hardware-revision strap;
 *   - RTL8261BE PHYs, which answer as the RTL8261N of the W1700K (HW 1.x) does;
 *   - other DRAM parts of the same size; an older build of the same stock U-Boot.
 */
#ifndef HW_AN7581_XR1710G_BOARD_H
#define HW_AN7581_XR1710G_BOARD_H

#include "../hw_bits.h"

/* ======================================================================== */
/* Parts                                                                    */
/* ======================================================================== */

/*
 * SoC: Airoha AN7581GT [measured: stock U-Boot names it].
 * DRAM: 2 GiB DDR4, trained at 2666 [measured]; marked 3CR75 D88PK [photo], not
 *   the W1700K's ESMT parts. Stock's `sdram_conf` is 0x00108893 [measured].
 * Flash: Winbond W25N04KV, ID ef aa 23, 2048 + 128 byte pages, 128 KiB blocks
 *   [measured]. On the unit read: no factory-bad block, vendor pool 250, the
 *   BBT on block 3846 and the BMT on block 4095, as W1700K_BMT_* has them.
 * Radio: MediaTek MT7996 on PCIe, with MT7976GN, MT7977BN and MT7977AN front
 *   ends at UG1, UA1 and UE1 [photo].
 * 10GbE PHYs: two Realtek RTL8261BE [photo]. Both answer PHY ID 0x001ccaf3,
 *   the RTL8261N's, and stock drives them with its RTL8261N driver [measured].
 *   OpenWrt's driver tells the BE from the N by a model field and gives both
 *   the same probe, configuration and firmware file (rtl8261n.bin) [static:
 *   the pinned OpenWrt's realtek patch 742].
 * Board monitor: a Nuvoton fan and thermal controller on I2C0 at 0x2e: vendor
 *   ID 0x50, chip ID 0xc3, version 0x21 in registers 0xfd to 0xff [measured],
 *   which is what the NCT7802 driver looks for; marked NCT7511Y [photo], as on
 *   the W1700K (HW 1.x). A 4-wire fan hangs from it. Idle in stock's failsafe
 *   mode, lid closed: 27 C local, 26 C on remote sensor 1, remote sensor 2
 *   unconnected (reads 127), fan at 1952 RPM on a PWM of 0x7f [measured]. Its
 *   alert output is GPIO 11 [static: stock's fan test script], idle high.
 * No GPS receiver and no Bluetooth: none on the board [photo], and stock's
 *   root has no program, firmware or configuration for either [measured].
 */
#define XR1710G_PHY_10G_ID              0x001ccaf3u
#define XR1710G_I2C0_HWMON_ADDR         0x2eu
#define XR1710G_HWMON_VENDOR_ID         0x50u   /* register 0xfd */
#define XR1710G_HWMON_CHIP_ID           0xc3u   /* register 0xfe */

/* ======================================================================== */
/* Console and stock firmware                                               */
/* ======================================================================== */

/*
 * UART1, 115200 8N1, 3.3 V, on a populated 5-pin header on the component side
 * beside the DC jack and the reset button [measured].
 * Stock U-Boot: "U-Boot 2014.04-rc1 (Mar 15 2024) AXON 1.6", prompt `ECNT>`,
 *   a four-second autoboot countdown, `bootcmd=flash imgread 2048;bootm`. It
 *   has `loadb`, and boots an unsigned FIT staged in RAM (hash-checked only),
 *   although the first-stage loader prints "Secure key exist" and stock Linux
 *   reports "The key exists in this device" [measured]. The W1700K2 this
 *   project first ran on has build AXON 2.3; every command the installer uses
 *   behaves the same on 1.6 [measured].
 * Stock Linux (5.4.55, firmware XR1710G_fw_5.1.1.12) gives no login on the
 *   console. Its failsafe mode, offered 43 s after power-on ("Press the [f]
 *   key and hit [enter]"), gives a root shell with nothing mounted for
 *   writing. A normal boot deletes files from its settings area about 50 s in
 *   ("Rogue file cleanup"), so a unit that has booted stock once is no longer
 *   as it arrived [measured].
 */
#define XR1710G_CONSOLE_BAUD            115200u
#define XR1710G_STOCK_BOOTDELAY_S       4u

/* ======================================================================== */
/* PCIe: the MT7996                                                         */
/* ======================================================================== */

/*
 * As on the W1700K: 14c3:7990 on PCIe0 in x2 mode, linked at Gen3 x2, window at
 * 0x20000000; its second interface 14c3:7991 on PCIe2, linked at Gen2 x1,
 * window at 0x28000000; PCIe1 unused. ASPM is off on both links under stock
 * (link control 0 on both root ports and both functions) [measured: PCI
 * configuration space under stock Linux, which numbers the second domain 1].
 */

/* ======================================================================== */
/* Ethernet                                                                 */
/* ======================================================================== */

/*
 * As on the W1700K. The 10 GbE PHYs are at MDIO 8 and 5, reset by GPIO 31 and
 * GPIO 46, active-low [measured: stock's device tree and its PHY probe]. The
 * 1 GbE port marked for the installer, "lan3", is switch port 1: it linked at
 * 1 Gbit/s on that port with a cable in it [measured].
 * Taken from a community tree for this board and not yet seen on a unit
 * [static: naoki66's an7581-xr1710g.dts]: that MDIO 8 is the jack called wan
 * and MDIO 5 the one called lan2, that the second 1 GbE port is switch port 2,
 * and that both PHYs' lanes are inverted both ways, as on the W1700K.
 */
#define XR1710G_MDIO_PHY_10G_A          8u      /* reset: GPIO 31 */
#define XR1710G_MDIO_PHY_10G_B          5u      /* reset: GPIO 46 */
#define XR1710G_SWITCH_PORT_LAN3        1u

/* ======================================================================== */
/* GPIO assignments (AN7581 GPIO line offsets)                              */
/* ======================================================================== */

/* All [measured: stock's device tree, and its kernel's table of the lines it
 * holds]. Every LED lights when its line is low. */
#define XR1710G_GPIO_RESET_BUTTON       0u      /* active-low */
#define XR1710G_GPIO_FAN_ALERT          11u     /* [static], see the board monitor */
#define XR1710G_GPIO_LED_STATUS_GREEN   17u
#define XR1710G_GPIO_LED_STATUS_BLUE    19u
#define XR1710G_GPIO_LED_STATUS_WHITE   20u
#define XR1710G_GPIO_LED_STATUS_RED     29u
#define XR1710G_GPIO_PHY_10G_A_RESET    31u     /* MDIO 8 */
#define XR1710G_GPIO_LED_LAN0_YELLOW    33u
#define XR1710G_GPIO_LED_LAN1_YELLOW    34u
/* The W1700K's GPS power line. The loader drives it low first of all on every
 * board, which lights this LED; once it has named the board an XR1710G it
 * drives the line high again (`board_xr1710g` in its environment). */
#define XR1710G_GPIO_LED_LAN0_GREEN     43u
#define XR1710G_GPIO_LED_LAN1_GREEN     44u
#define XR1710G_GPIO_PHY_10G_B_RESET    46u     /* MDIO 5 */

/*
 * Hardware-revision straps. Read as stock leaves the pads, without forcing them
 * to GPIO as the W1700K's procedure does: GPIO 30 = 0 and GPIO 18 = 1, which by
 * the W1700K's formula (2 * GPIO30 + GPIO18) is major revision 1, the label's
 * "HW 1.0"; GPIO 45 = 1 [measured]. GPIO 44, the W1700K's minor LSB, is an LED
 * here, so no minor revision can be read. Nothing in this tree reads the straps.
 */

/* ======================================================================== */
/* NAND and what stock keeps in it                                          */
/* ======================================================================== */

/*
 * The W1700K's, byte for byte in everything this project depends on
 * [measured]: stock's partition string
 * "2m[bootloader],2m[uenv],2m[dsd],64m[tclinux],64m[tclinux_slave],312m[system]",
 * the identity block at 0x400000 and the MT7996 calibration 0x5000 into it
 * (16 KiB, beginning 90 79, both crystal trim bytes set). The second firmware
 * slot holds a copy of the first slot's image, identical over its 41.5 MiB;
 * stock Linux lists that slot's kernel as 0x2100 bytes all the same, which is
 * its own bookkeeping, not the flash.
 * The identity block holds: serial_number, manufacturer, wan_mac, lan_mac,
 * fan_id, admin_username, admin_password, wifi_ssid, wifi_passphrase,
 * wifi_guest_passphrase and wifi_wps_pin; a backup therefore holds the unit's
 * credentials. Serial numbers begin "XR1710G", which is how the loader
 * tells this board (platform/boards.py). The calibration's own MAC fields
 * carry MediaTek's prefix 00:0c:43, not the unit's.
 * A raw read of the whole chip differed from the corrected one in 148 single
 * bits, never two in one 512-byte ECC step.
 */

#endif /* HW_AN7581_XR1710G_BOARD_H */
