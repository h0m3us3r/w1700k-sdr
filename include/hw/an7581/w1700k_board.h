/* SPDX-License-Identifier: MIT */
/*
 * Gemtek W1700K board wiring around the AN7581: parts, console, PCIe links to
 * the MT7996, Ethernet ports and PHYs, GPIO assignments, the SPI-NAND part
 * and its partition maps, the per-unit identity/calibration window, and the
 * boot chain.
 *
 * ADDRESS SPACES:
 *   W1700K_GPIO_*        AN7581 GPIO line offsets (scu.h numbering)
 *   W1700K_MDIO_*        addresses on the built-in switch's MDIO bus
 *   W1700K_NAND_*        byte offsets in the vendor-LOGICAL NAND view: every
 *                        stock and project flash access translates them
 *                        through the vendor BBT shift and BMT substitution
 *                        exactly once (snfi.h). They equal physical offsets
 *                        only where no factory-bad block precedes them.
 *   W1700K_LOAD_*        AN7581 physical DRAM addresses
 * Wiring is [static] from the board device tree unless tagged. Board
 * variants: FCC ID MXF-W1700K (HW labels 1.0, 1.1) and MXF-W1700K2 (HW 2.1).
 * The W1700K2 differs in its 10 GbE PHYs (below), its fan controller
 * (marked NCT7512S) and, on some units, its SPI-NAND part. One W1700K2 with
 * the W25N04KV has been installed and streamed; its RTL8261CE PHYs and NAND
 * are [measured] there, its fan controller is not, and no W25N04LV unit has
 * been read.
 */
#ifndef HW_AN7581_W1700K_BOARD_H
#define HW_AN7581_W1700K_BOARD_H

#include "../hw_bits.h"

/* ======================================================================== */
/* Parts                                                                    */
/* ======================================================================== */

/*
 * SoC: Airoha AN7581GT.
 * DRAM: two ESMT M16U8G16512A DDR4, 8 Gbit x16 each = 2 GiB, inferred
 *   32-bit bus [measured size; markings read from a board photograph].
 * Flash: Winbond W25N04KV, 512 MiB SPI-NAND [measured geometry]; a W25N04LV
 *   on some W1700K2s (below).
 * Radio: MediaTek MT7996 on PCIe (two host interfaces, below).
 * 10GbE PHYs: two Realtek RTL8261N (10GBASE-T) on the W1700K, two RTL8261CE
 *   on the W1700K2 [measured on one unit].
 * Board monitor: Nuvoton fan/thermal controller on I2C0 at 0x2e (marked
 *   NCT7511Y; NCT7512S on the W1700K2; the device tree binds the NCT7802
 *   driver, whose device-tree probe checks no chip ID).
 */
#define W1700K_DRAM_BYTES               0x80000000u
#define W1700K_I2C0_HWMON_ADDR          0x2eu
#define W1700K_I2C0_BUS_HZ              100000u

/* ======================================================================== */
/* Console                                                                  */
/* ======================================================================== */

/* UART1 (AN7581_UART1_BASE), Linux ttyS0, 115200 8N1, on the dedicated
 * uart1_txd/uart1_rxd pads. The reference board tree also enables UART2 and
 * HSUART3; what they connect to is not established. */
#define W1700K_CONSOLE_BAUD             115200u

/* ======================================================================== */
/* PCIe: the MT7996                                                         */
/* ======================================================================== */

/*
 * Primary host interface: PCIe0 in x2 mode (PCIe0 + PCIe1 MACs), PERST# on
 * the pcie_reset0 pad (open-drain), domain 0, device 0000:01:00.0, ID
 * 14c3:7990, BAR0 0x20000000 (2 MiB), Gen3 x2 [measured].
 * Second host interface of the same chip (a second WFDMA data path, not a
 * second radio): PCIe2 over the USB1 PHY serdes, PERST# on pcie_reset2
 * (open-drain), domain 2, device 0002:01:00.0, ID 14c3:7991, BAR0 0x28000000
 * (2 MiB) [measured]; Gen2 x1 per a stock boot record.
 * Streaming limits and the ASPM hazard: pcie.h.
 */
#define W1700K_MT7996_VENDOR_ID         0x14c3u
#define W1700K_MT7996_DEVICE_ID         0x7990u
#define W1700K_MT7996_HIF2_DEVICE_ID    0x7991u
#define W1700K_MT7996_PCI_DOMAIN        0u
#define W1700K_MT7996_HIF2_PCI_DOMAIN   2u
#define W1700K_MT7996_PCI_BUS           1u
#define W1700K_MT7996_BAR0              0x20000000u
#define W1700K_MT7996_HIF2_BAR0         0x28000000u
#define W1700K_MT7996_BAR0_SIZE         0x00200000u

/* ======================================================================== */
/* Ethernet                                                                 */
/* ======================================================================== */

/*
 * GDM1 -> internal 10 Gb/s fixed link (full duplex, pause) to the built-in
 *   switch's CPU port 6; Linux eth0 (management). Switch user ports: port 1
 *   = "lan3" (internal PHY at MDIO 9), port 2 = "lan4" (internal PHY at
 *   MDIO 10), both 1 Gb/s. Ports 3/4 (PHYs 11/12) are not enabled by the
 *   board tree. "eth0 at 10000" is this internal link, not a 10G jack.
 * GDM2 -> PON PCS -> USXGMII -> RTL8261N at MDIO 8 -> 10GBASE-T jack the
 *   reference names "wan"; Linux eth1. Links at 10 Gb/s [measured].
 * GDM4 -> ETH PCS -> USXGMII -> RTL8261N at MDIO 5 -> 10GBASE-T jack
 *   "lan2"; Linux eth2 (no link tested).
 * Both RTL8261Ns: Clause 45, on the switch's MDIO bus, TX and RX lane
 *   polarity inverted, reset GPIO active-low with 200 ms assert and 200 ms
 *   deassert, LED 2 yellow / LED 3 green driven by the PHY. Their interrupt
 *   lines (PHY 5 -> GPIO 22, PHY 8 -> GPIO 23, active-low) are left unused
 *   by the reference device tree.
 * The PHYs need the "rtl8261n.bin" patch (1,437 records) at their first
 * probe [measured].
 * W1700K2: RTL8261CEs (PHY ID 0x001cc899, sub-model 0x00) at the same MDIO
 *   addresses, with the same reset GPIOs and the same inverted lanes [measured
 *   on one unit; the 2.1's stock boot log names "rtl8261ce" and swaps the
 *   SerDes P/N]. Their driver loads "rtl_nic/rtl8261c.bin" (Realtek,
 *   linux-firmware) when the PHY is first configured and applies the lane
 *   polarity from the device tree itself [static]. The kernel picks the driver
 *   by PHY ID, so one device tree and one kernel serve both variants.
 * HAZARD: if the first PHY probe fails (patch not yet available), the error
 *   path holds both PHYs in reset (GPIO 31/46 low) and they never bind again
 *   until reboot [measured]. Build the patch into the kernel image.
 */
#define W1700K_GDM_MGMT                 1u
#define W1700K_GDM_10G_A                2u      /* "wan", eth1 */
#define W1700K_GDM_10G_B                4u      /* "lan2", eth2 */
#define W1700K_SWITCH_CPU_PORT          6u
#define W1700K_SWITCH_PORT_LAN3         1u
#define W1700K_SWITCH_PORT_LAN4         2u
#define W1700K_MDIO_PHY_LAN3            9u
#define W1700K_MDIO_PHY_LAN4            10u
#define W1700K_MDIO_PHY_10G_A           8u      /* GDM2 */
#define W1700K_MDIO_PHY_10G_B           5u      /* GDM4 */
#define W1700K_PHY_RESET_ASSERT_US      200000u
#define W1700K_PHY_RESET_DEASSERT_US    200000u

/* ======================================================================== */
/* GPIO assignments (AN7581 GPIO line offsets)                              */
/* ======================================================================== */

#define W1700K_GPIO_RESET_BUTTON        0u      /* active-low */
/* MDIO pins (mux "mdio" = GPIO 1/2); GPIO 2 is also configured output-high. */
#define W1700K_GPIO_MDIO_MDC            1u
#define W1700K_GPIO_MDIO_MDIO           2u
/* Front LEDs, all active-low. The four status emitters share one lens; each lights its colour
 * when its pin is low, and all four dim under the SoC's waveform generators (GPIO flash mode,
 * map entry 8 + (gpio & 7): kernel/w1700k_sdr/led.c) [measured by eye on a unit]. */
#define W1700K_GPIO_LED_LAN3_GREEN      9u
#define W1700K_GPIO_LED_LAN3_YELLOW     10u
#define W1700K_GPIO_LED_STATUS_GREEN    17u
#define W1700K_GPIO_LED_STATUS_BLUE     19u
#define W1700K_GPIO_LED_STATUS_WHITE    20u
#define W1700K_GPIO_LED_LAN4_GREEN      27u
#define W1700K_GPIO_LED_LAN4_YELLOW     28u
#define W1700K_GPIO_LED_STATUS_RED      29u
/* 10G PHY resets, active-low. */
#define W1700K_GPIO_PHY_10G_A_RESET     31u     /* MDIO 8, GDM2 */
#define W1700K_GPIO_PHY_10G_B_RESET     46u     /* MDIO 5, GDM4 */
/* 10G PHY interrupts (unused). */
#define W1700K_GPIO_PHY_10G_B_IRQ       22u
#define W1700K_GPIO_PHY_10G_A_IRQ       23u

/*
 * Board hardware-revision straps [static from the stock firmware]:
 * major = 2 * GPIO30 + GPIO18, minor = 2 * GPIO45 + GPIO44, read after
 * forcing the four pads to GPIO (chip SCU FORCE_GPIO_EN/_H, scu.h). Labels
 * seen: 1.0 and 1.1 (W1700K), 2.1 (W1700K2).
 */
#define W1700K_GPIO_HWREV_MAJOR_MSB     30u
#define W1700K_GPIO_HWREV_MAJOR_LSB     18u
#define W1700K_GPIO_HWREV_MINOR_MSB     45u
#define W1700K_GPIO_HWREV_MINOR_LSB     44u

/* ======================================================================== */
/* SPI-NAND part                                                            */
/* ======================================================================== */

/*
 * W25N04KV geometry [measured]: 2048-byte pages + 128-byte OOB, 64 pages per
 * 128 KiB eraseblock, 4096 blocks, 512 MiB.
 * Some W1700K2 units carry a W25N04LV instead (ID 0xef 0x8b 0x23) [static,
 * never read on a unit]: see W1700K_NAND_LV_* below.
 * Bus: up to 50 MHz, commands 1-bit, reads 2-bit [static].
 * ECC: on-die, 8 bits per 512-byte step; the SoC's ECC and AutoFDM are off
 * [static]. ECC-protected free OOB ("AUTO"): 4 x 14 bytes at OOB offsets 2,
 * 18, 34 and 50 (56 bytes) [static]. OOB bytes 0/1 are the vendor bad
 * markers, 2-3 the vendor remap back-reference (snfi.h). Parity bytes are
 * not valid for new data: regenerate ECC, never copy it.
 * Up to 4 partial programs per page, pages within a block in ascending order
 * [static, part datasheet].
 * A raw whole-chip image (main area, then all OOB) is 570,425,344 bytes =
 * 536,870,912 + 33,554,432.
 * Raw (ECC-off) reads do not repeat: two whole-chip raw reads differed by
 * ~10 single-bit flips (~1 per 429 Mbit), each alone in its 512-byte ECC
 * step, none in OOB [measured]. Compare raw images per ECC step against the
 * part's strength, not by hash.
 * Raw reads also carry the chip's stable errors, about 50 wrong bits per chip
 * that the on-die ECC corrects on every normal read; uncorrected, a raw image
 * fails stock's own kernel SHA-1 and squashfs checks [measured]. On-die ECC:
 * step k's 13 parity bytes at OOB 64 + 16k, an affine function of its 512 data
 * bytes; step 3 also covers its spare, where page 0 of a written block keeps a
 * counter at OOB 60-63 [measured]. So init/ backups are not raw images: they
 * hold the vendor-logical data read with the ECC on, 0 to the reserve
 * (446 MiB).
 */
#define W1700K_NAND_PAGE_BYTES          2048u
#define W1700K_NAND_OOB_BYTES           128u
#define W1700K_NAND_PAGES_PER_BLOCK     64u
#define W1700K_NAND_BLOCK_BYTES         0x20000u
#define W1700K_NAND_BLOCKS              4096u
#define W1700K_NAND_CHIP_BYTES          0x20000000u
#define W1700K_NAND_ECC_STEP_BYTES      512u
#define W1700K_NAND_ECC_BITS            8u
#define W1700K_NAND_OOB_FREE(n)         (2u + 16u * (n)) /* n = 0..3, 14 bytes each */
#define W1700K_NAND_OOB_FREE_LEN        14u
#define W1700K_NAND_PARTIAL_PROGRAMS    4u
#define W1700K_NAND_SPI_MAX_HZ          50000000u
#define W1700K_NAND_RAW_IMAGE_BYTES     570425344u

/*
 * W25N04LV geometry [static: OpenWrt PR #25532 from a 2.1 owner, Rockchip's
 * and Nuvoton's winbond tables; never read on a unit by this project]:
 * 4096-byte pages + 256-byte OOB, 64 pages per 256 KiB eraseblock, 2048
 * blocks, one plane, 512 MiB; 8-bit on-die ECC per 512-byte step with the
 * W25N04KV's status bits. Spare layout taken as the W25N04KV's doubled: eight
 * 16-byte groups, user bytes 2-15 of each free and ECC-protected (112 bytes),
 * step k's 13 parity bytes at OOB 128 + 16k. A raw whole-chip image is the
 * same 570,425,344 bytes.
 */
#define W1700K_NAND_LV_PAGE_BYTES       4096u
#define W1700K_NAND_LV_OOB_BYTES        256u
#define W1700K_NAND_LV_PAGES_PER_BLOCK  64u
#define W1700K_NAND_LV_BLOCK_BYTES      0x40000u
#define W1700K_NAND_LV_BLOCKS           2048u
#define W1700K_NAND_LV_OOB_FREE(n)      (2u + 16u * (n)) /* n = 0..7, 14 bytes each */
#define W1700K_NAND_LV_OOB_PARITY(n)    (128u + 16u * (n)) /* n = 0..7, 13 bytes */

/* Vendor BBT/BMT values for this part (formulas in snfi.h): scan target 327
 * good blocks, ceiling 512, pool clamped to 250, so the reserve starts at
 * block 3846 (0x1e0c0000); the stock kernel publishes 3584 blocks
 * (0x1c000000). A pool above 512 (more than 185 bad blocks in the scanned
 * span) leaves no BMT and the stock boot reports it. */
#define W1700K_BMT_POOL_BLOCKS          250u
#define W1700K_BMT_SYSTEM_BLOCKS        3846u
#define W1700K_BMT_USER_BLOCKS          3584u

/* The W25N04LV under the same vendor rules [inferred: the rule is read from
 * stock U-Boots that do not know the part]: scan target 163, ceiling 256, no
 * clamp in practice, so the pool is 163 plus the factory-bad blocks among the
 * top 163 good ones and differs per unit; the BMT checksum covers that many
 * entries. The stock kernel would publish 1792 blocks (0x1c000000). With at
 * most 40 bad blocks, at least 1845 blocks translate, above the 1784 that
 * reach the end of `scratch` (0x1be00000). */
#define W1700K_BMT_LV_POOL_LEAST        163u
#define W1700K_BMT_LV_USER_BLOCKS       1792u

/* ======================================================================== */
/* NAND partition maps (vendor-logical offsets)                             */
/* ======================================================================== */

/* Stock map (compiled into the stock bootloader as
 * "2m[bootloader],2m[uenv],2m[dsd],64m[tclinux],64m[tclinux_slave],
 * 312m[system]"); everything above system is the vendor BBT/BMT reserve. */
#define W1700K_NAND_BOOTLOADER          0x00000000u /* 2 MiB: tcboot.bin */
#define W1700K_NAND_BOOTLOADER_SIZE     0x00200000u
#define W1700K_NAND_UENV                0x00200000u /* 2 MiB: saved environment */
#define W1700K_NAND_UENV_SIZE           0x00200000u
#define W1700K_NAND_DSD                 0x00400000u /* 2 MiB: identity + calibration */
#define W1700K_NAND_DSD_SIZE            0x00200000u
#define W1700K_NAND_STOCK_TCLINUX       0x00600000u /* 64 MiB */
#define W1700K_NAND_STOCK_TCLINUX_SLAVE 0x04600000u /* 64 MiB */
#define W1700K_NAND_STOCK_SYSTEM        0x08600000u /* 312 MiB, UBI */
#define W1700K_NAND_RESERVE             0x1be00000u
#define W1700K_NAND_RESERVE_SIZE        0x04200000u

/* Project map: the first three windows and the reserve stay vendor-owned and
 * read-only; the chainloaded U-Boot occupies the start of stock tclinux. */
#define W1700K_NAND_LOADER              0x00600000u /* 2 MiB */
#define W1700K_NAND_LOADER_SIZE         0x00200000u
#define W1700K_NAND_SYSTEM              0x00800000u /* UBI: FIT, rootfs, env */
#define W1700K_NAND_SYSTEM_SIZE         0x03000000u
#define W1700K_NAND_DURABLE             0x03800000u /* UBI, writable */
#define W1700K_NAND_DURABLE_SIZE        0x02000000u
#define W1700K_NAND_SCRATCH             0x05800000u
#define W1700K_NAND_SCRATCH_SIZE        0x16600000u

/* OpenWrt's layout for comparison: chainloader 0x600000 (1 MiB), one UBI
 * 0x700000..0x1be00000. Its UBI "factory" volume (EEPROM at +0, 0x1e00
 * bytes; MACs at +0x5000/+0x6000) is a different object from stock dsd. */
#define W1700K_NAND_OPENWRT_CHAINLOADER 0x00600000u
#define W1700K_NAND_OPENWRT_UBI         0x00700000u

/*
 * Identity/calibration window, stock "dsd" (offsets from W1700K_NAND_DSD)
 * [measured layout]:
 *   +0x0000  newline-separated ASCII "key=value" lines ending in 0xff
 *            padding: serial_number, wan_mac, lan_mac, manufacturer, fan_id,
 *            wifi_ssid, and per-unit credentials (an admin password hash,
 *            Wi-Fi passphrases, a WPS PIN). Any dump or console transcript
 *            of this window is as sensitive as a full NAND image.
 *   +0x1000  DER certificates, 5,080 bytes.
 *   +0x5000  a 7,680-byte (0x1e00) block, the size of an MT7996 EEPROM image
 *            (the stock calibration candidate).
 * Reads go in whole 2048-byte pages. The stock bootloader environment holds
 * no serial, and its ethaddr is a placeholder shared by every unit.
 */
#define W1700K_DSD_KV                   0x0000u
#define W1700K_DSD_CERTS                0x1000u
#define W1700K_DSD_CERTS_BYTES          5080u
#define W1700K_DSD_EEPROM               0x5000u
#define W1700K_DSD_EEPROM_BYTES         0x1e00u

/* ======================================================================== */
/* Boot chain                                                               */
/* ======================================================================== */

/*
 * BootROM (on-chip, loads tcboot.bin [inferred]) -> tcboot.bin at NAND 0
 * (boot device from SCU strap 0xb8 bit 6, scu.h) -> stock U-Boot -> project
 * U-Boot from the loader window -> Linux.
 *
 * tcboot.bin (540,672 bytes in the current stock build) [static]: an ARM32
 * head; BL2 with DRAM init at +0x4420; the BL2 image loader at +0x109e9; a
 * SPI-NAND identifier table at +0x1c7a3 (data, not a stage); six DER
 * certificates; BL31 (TF-A v2.3) at +0x20000; the LZMA-compressed stock U-Boot
 * 2014.04-rc1 at +0x2a400 (text base 0x81e00000). An older build places the
 * U-Boot payload at +0x29c00.
 *
 * Image authentication [static]: stock "flash imgread" verifies an image
 * through BL31, whose root-key check compares a SHA-512 digest held in eFuse;
 * "flash read" (raw, BMT-translated copy) followed by generic "bootm" checks
 * FIT hash nodes only. The chainload uses the second path. The stock U-Boot
 * fails on an unrecognised FIT hash algorithm instead of skipping it, so its
 * FITs carry SHA-1 nodes only. Default stock environment: fdt_high
 * 0xac000000, dram_limit 1024M, sdram_conf 0x00108893; stock U-Boot reports
 * 2 GiB DRAM.
 *
 * Project U-Boot: an ARM64 Linux-image FIT at W1700K_NAND_LOADER, loaded and
 * entered at 0x80200000 with its FDT at 0x82000000; it stages the Linux FIT
 * at 0x81800000 and keeps FDTs below 0x84000000 (under the NPU region).
 */
#define W1700K_TCBOOT_BYTES             540672u
#define W1700K_TCBOOT_BL2_DRAM          0x4420u
#define W1700K_TCBOOT_BL2_LOADER        0x109e9u
#define W1700K_TCBOOT_SNAND_ID_TABLE    0x1c7a3u
#define W1700K_TCBOOT_BL31              0x20000u
#define W1700K_TCBOOT_UBOOT_LZMA        0x2a400u
#define W1700K_LOAD_STOCK_UBOOT_TEXT    0x81e00000u
#define W1700K_LOAD_STOCK_FDT_HIGH      0xac000000u
#define W1700K_LOAD_LOADER_TEXT         0x80200000u
#define W1700K_LOAD_LOADER_FDT          0x82000000u
#define W1700K_LOAD_LINUX_FIT           0x81800000u
#define W1700K_LOAD_FDT_HIGH            0x84000000u

/* ======================================================================== */
/* Thermal                                                                  */
/* ======================================================================== */

/* CPU thermal zone trips: hot 95 C (cooling by CPU frequency), critical
 * 120 C [static]. The zone idles near 41 C and full four-core load moves it
 * by under 1 C [measured]: the A53s are not the board's heat source. */
#define W1700K_THERMAL_CPU_HOT_MC       95000u
#define W1700K_THERMAL_CPU_CRIT_MC      120000u

#endif /* HW_AN7581_W1700K_BOARD_H */
