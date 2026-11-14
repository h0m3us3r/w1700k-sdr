#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the restorer that writes a unit's backup back, returning it to stock.

    platform/restorer/build.py    -> platform/build/images/w1700k-restorer.itb

Stock U-Boot receives this FIT over TFTP and boots it from RAM, as it does the
dumper, so our installed loader is not needed. It receives a backup (the
vendor-logical data the dumper read, 446 MiB, gzip-compressed), unpacks it into
RAM, and `nandsync` compares it with the NAND block by block, erasing, writing
and reading back only the blocks that differ, through the same vendor BBT/BMT
translation and replacement engine as the loader with the chip's ECC on. Its
write range is wider than the loader's, from stock's `uenv` to the end of
`stock`, which takes in stock's images under and behind ours; `init/restore` never
writes `calibration`, and checks it. There is no UBI, no saved environment and
no autoboot.
"""
import gzip
from pathlib import Path
import sys

UNIT = Path(__file__).resolve().parent
DUMPER = UNIT.parent / 'dumper'
LOADER = UNIT.parent / 'loader'
BMT = UNIT.parent / 'bmt'
sys.path.insert(0, str(UNIT.parent))
from buildlib import BUILD, IMAGES, LOGS, kconfig, main_guard, require, write_if_changed
import partitions
import uboot

FIT = IMAGES / 'w1700k-restorer.itb'
DEFCONFIG = 'an7581_w1700k_restorer_defconfig'
# The dumper's device tree (the chip as a whole, no partitions) and the loader's
# BMT binding with the shared translation and replacement engine.
FILES = {
    f'configs/{DEFCONFIG}': UNIT / DEFCONFIG,
    'defenvs/an7581_w1700k_restorer_env': UNIT / 'an7581_w1700k_restorer_env',
    'dts/upstream/src/arm64/airoha/an7581-w1700k-dumper.dts':
        DUMPER / 'an7581-w1700k-dumper.dts',
    'arch/arm/dts/an7581-w1700k-dumper-u-boot.dtsi':
        DUMPER / 'an7581-w1700k-dumper-u-boot.dtsi',
    'drivers/mtd/nand/airoha_bmt_layer.c': LOADER / 'airoha_bmt_layer.c',
    'drivers/mtd/nand/airoha_bmt_layer.h': LOADER / 'airoha_bmt_layer.h',
    **{f'drivers/mtd/nand/{name}': BMT / name
       for name in ('airoha_bmt.c', 'airoha_bmt.h', 'airoha_bmt_write.c',
                    'airoha_bmt_write.h', 'airoha_bmt_transaction.c',
                    'airoha_bmt_transaction.h', 'airoha_bmt_mtd_write.h')},
}
# The backup buffer, and where the compressed backup lands before `unzip`
# (init/restore's PACKED).
BUFFER = 0x90000000
PACKED = 0xac000000
BACKUP_BYTES = partitions.GEOMETRY['reserved-bmt'][0]
ENABLED = ('AN7581_W1700K_SDR', 'POSITION_INDEPENDENT', 'LINUX_KERNEL_IMAGE_HEADER',
           'OF_SEPARATE', 'ENV_IS_NOWHERE', 'CMD_MTD', 'CMD_HASH', 'SHA256',
           'CMD_TFTPBOOT', 'MTD_SPI_NAND', 'AIROHA_SNFI_SPI', 'AIROHA_ETH',
           'AIROHA_BMT', 'AIROHA_BMT_WRITE', 'CMD_UNZIP', 'CMD_NANDSYNC')
DISABLED = ('MTD_SPI_NAND_READ_ONLY', 'MTD_UBI', 'CMD_UBI', 'AUTOBOOT', 'CMD_SAVEENV',
            'CMD_EDITENV', 'CMD_MTD_MARKBAD', 'CMD_MTD_NAND_WRITE_TEST', 'CMD_MTD_OTP',
            'BOOTCOUNT_LIMIT', 'USE_PREBOOT', 'ENV_IS_IN_MTD', 'ENV_IS_IN_UBI')
REQUIRED = ('airoha_bmt_attach', 'airoha_bmt_attached', 'airoha_bmt_translate',
            'airoha_bmt_transaction_recover', 'airoha_bmt_publish', 'do_mtd_io',
            'do_mtd_erase', 'do_unzip', 'do_nandsync', 'airoha_bmt_vendor_pool',
            'w25n04lv_ooblayout_ecc')
ABSENT = ('env_save', 'do_mtd_markbad', 'do_bmtdetach')
ABSENT_PREFIXES = ('ubi_', 'ubiblock_', 'do_ubi')
# The loader's translation and transaction patches, and the dumper's reports
# (`mtd list` access flags and translation, probe failures) and corrected-page
# read.
PATCHES = (LOADER / 'patches/0002-mtd-nand-airoha-bmt-translation-layer.patch',
           LOADER / 'patches/0003-mtd-nand-vendor-bmt-transactions.patch',
           DUMPER / 'patches/0003-cmd-mtd-report-runtime-access-flags.patch',
           DUMPER / 'patches/0004-spi-airoha-snfi-report-nfi-init-failure.patch',
           DUMPER / 'patches/0005-mtd-report-why-a-dm-mtd-device-did-not-probe.patch',
           DUMPER / 'patches/0008-cmd-mtd-report-translation-and-read-through-corrected-pages.patch')


def patches():
    return [uboot.BOARD_PATCH, *uboot.SHARED_PATCHES, *PATCHES,
            *sorted((UNIT / 'patches').glob('*.patch'))]


def main():
    src = uboot.prepare('restorer', patches(), FILES, partitions.policy_header('uenv', 'stock'))
    uboot.mkimage(src)
    out = uboot.build('restorer', src, DEFCONFIG)
    config = kconfig(out / '.config')
    uboot.require_config('restorer', config, ENABLED, DISABLED)
    require(int(config['CONFIG_SYS_LOAD_ADDR'], 0) == BUFFER,
            'restorer: the load address is not the backup buffer')
    image, dtb, linked = uboot.check_image('restorer', out, config, BUFFER)
    require(BUFFER + BACKUP_BYTES <= PACKED and PACKED + BACKUP_BYTES <= uboot.RAM_END,
            'restorer: the backup and its compressed copy do not fit RAM')
    missing = [name for name in REQUIRED if name not in linked]
    require(not missing, 'restorer: not linked: ' + ', '.join(missing))
    present = sorted(name for name in ABSENT if name in linked)
    present += sorted(name for name in linked if name.startswith(ABSENT_PREFIXES))
    require(not present, 'restorer: linked but not wanted: ' + ', '.join(present[:8]))

    payload = BUILD / 'restorer/u-boot.bin.gz'
    write_if_changed(payload, gzip.compress(image, 9, mtime=0))
    IMAGES.mkdir(parents=True, exist_ok=True)
    its = uboot.stock_fit('W1700K NAND restorer (gzip)', 'restorer', payload, 'gzip',
                          out / 'u-boot.dtb')
    uboot.make_fit(its, FIT, out / 'scripts/dtc', LOGS / 'restorer.log')
    print(f'restorer: {FIT} ({FIT.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    main_guard(main)
