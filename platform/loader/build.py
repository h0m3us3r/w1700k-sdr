#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build our U-Boot (the loader) as a FIT that stock U-Boot boots from NAND.

    platform/loader/build.py      -> platform/build/images/w1700k-loader.itb

Stock U-Boot stays the first stage. The installer changes its bootcmd to
`flash read 0x600000 <len> 0x89000000; bootm 0x89000000`, which loads this FIT
from the `loader` partition. The loader translates NAND through the vendor BMT
like stock does, keeps its environment in UBI and boots the system FIT.
"""
import lzma
from pathlib import Path
import struct
import sys

UNIT = Path(__file__).resolve().parent
sys.path.insert(0, str(UNIT.parent))
from buildlib import BUILD, IMAGES, LOGS, kconfig, main_guard, require, write_if_changed
import boards
import partitions
import uboot

FIT = IMAGES / 'w1700k-loader.itb'
DEFCONFIG = 'an7581_w1700k_sdr_defconfig'
# In the FIT's description, where a running unit can read it from its `loader`
# partition: this loader applies a root staged in durable storage (init/flash --ssh).
STAGED_UPDATE = 'staged-update 1'
# The loader comes in two images of one build. The first is what stock boots from
# `loader`, written only over the serial console. The second, for `loader-b`, is
# what an update over the network delivers: the first starts it when it is whole,
# is of this kind (the FIT's description, which the first compares) and was not
# found wanting. They differ in two bytes of the built-in environment, and in
# the description: `loader_slot`, by which each knows which it is, and
# `bootdelay`, since the second counts down one second where the first counts
# five (the first is the one the console tools stop; the second can still be
# stopped by hand).
# SECOND_LOADER in the first's description tells a running unit that its loader
# starts a second one (system-update reads it).
SECOND_LOADER = 'second-loader 1'
SECOND_KIND = 'W1700K SDR second ARM loader'      # loader_b_kind in the environment
SLOT_A, SLOT_B = b'loader_slot=a\0', b'loader_slot=b\0'
DELAY_A, DELAY_B = b'bootdelay=5\0', b'bootdelay=1\0'
FIT_B = IMAGES / 'w1700k-loader-b.itb'
SECOND_AT = 0x8e000000                            # loader_b_at: where the first reads the second
BMT = UNIT.parent / 'bmt'
FILES = {
    f'configs/{DEFCONFIG}': UNIT / DEFCONFIG,
    'defenvs/an7581_w1700k_sdr_env': UNIT / 'an7581_w1700k_sdr_env',
    'dts/upstream/src/arm64/airoha/an7581-w1700k-sdr-loader.dts':
        UNIT / 'an7581-w1700k-sdr-loader.dts',
    'arch/arm/dts/an7581-w1700k-sdr-loader-u-boot.dtsi':
        UNIT / 'an7581-w1700k-sdr-loader-u-boot.dtsi',
    'drivers/mtd/nand/airoha_bmt_layer.c': UNIT / 'airoha_bmt_layer.c',
    'drivers/mtd/nand/airoha_bmt_layer.h': UNIT / 'airoha_bmt_layer.h',
    **{f'drivers/mtd/nand/{name}': BMT / name
       for name in ('airoha_bmt.c', 'airoha_bmt.h', 'airoha_bmt_write.c',
                    'airoha_bmt_write.h', 'airoha_bmt_transaction.c',
                    'airoha_bmt_transaction.h', 'airoha_bmt_mtd_write.h')},
}
# Our own loader stages images at 0x81800000; stock stages this FIT at 0x89000000.
STAGING = 0x81800000
STOCK_STAGING = 0x89000000
# Stock's own LZMA payloads use an 8 MiB dictionary, which its decompressor
# allocates from a small heap; Python would otherwise pick 64 MiB.
LZMA_DICTIONARY = 0x800000
ENABLED = ('AN7581_W1700K_SDR', 'POSITION_INDEPENDENT', 'LINUX_KERNEL_IMAGE_HEADER',
           'OF_SEPARATE', 'FIT', 'SHA256', 'CMD_BOOTM', 'CMD_MTD', 'CMD_UBI',
           'CMD_TFTPBOOT', 'CMD_HASH', 'AUTOBOOT', 'MTD_SPI_NAND', 'AIROHA_BMT',
           'AIROHA_BMT_WRITE', 'MTD_UBI', 'AIROHA_SNFI_SPI', 'AIROHA_ETH',
           'ENV_IS_IN_UBI', 'ENV_REDUNDANT', 'CMD_SAVEENV', 'BOOTCOUNT_LIMIT',
           'BOOTCOUNT_ENV',
           # the staged root update (update_apply in the environment): a file from the
           # durable UBIFS, its name and digest from a text file, and the saved
           # environment erased again once the update is settled
           # (the durable UBIFS compresses with zstd: platform/images.py)
           'CMD_UBIFS', 'ZSTD', 'CMD_IMPORTENV', 'CMD_ENV_EXISTS', 'CMD_ERASEENV', 'HUSH_PARSER',
           # only the update's two state variables come back from a saved environment
           # (patch 0008): the scripts are always this build's
           'ENV_WRITEABLE_LIST')
DISABLED = ('ENV_IS_NOWHERE', 'ENV_IS_IN_MTD', 'BOOTSTD', 'EFI_LOADER', 'MMC', 'PCI',
            'USB', 'CMD_GO')
LINKED = ('airoha_bmt_attach', 'airoha_bmt_translate', 'airoha_bmt_transaction_recover',
          'airoha_bmt_publish', 'airoha_bmt_vendor_pool', 'env_ubi_media_ready', 'ubi_part_ro',
          'ubi_part_rw', 'w25n04lv_ooblayout_ecc')


def patches():
    return sorted((UNIT / 'patches').glob('*.patch')) + uboot.SHARED_PATCHES


def compress(image):
    """LZMA-alone with the vendor's dictionary size and the size in the header.

    Stock U-Boot decompresses this; a streaming compressor would leave the
    'unknown size' marker in the header, which stock is not relied on to handle.
    """
    compressor = lzma.LZMACompressor(
        format=lzma.FORMAT_ALONE,
        filters=[{'id': lzma.FILTER_LZMA1, 'preset': 9 | lzma.PRESET_EXTREME,
                  'dict_size': LZMA_DICTIONARY}])
    payload = bytearray(compressor.compress(image) + compressor.flush())
    require(payload[5:13] == b'\xff' * 8, 'loader: unexpected LZMA header')
    struct.pack_into('<Q', payload, 5, len(image))
    require(lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(bytes(payload)) == image,
            'loader: the LZMA payload does not decompress to u-boot.bin')
    return bytes(payload)


def check_boards(image):
    """One loader for every board in platform/boards.py: the GPS receiver's line is driven low
    first, then the unit is named from its serial, by each board's prefix."""
    require(image.count(b'bootcmd=run gps_off; run board_id; ') == 1,
            'loader: the built-in environment does not switch the GPS receiver off and then '
            'name the board, first of all')
    named = image[image.index(b'board_id='):].split(b'\0', 1)[0].decode()
    missing = [board.key for board in boards.ALL
               if f'"x{board.serial_prefix}"' not in named or f'setenv board {board.key};' not in named]
    require(not missing, 'loader: board_id does not name: ' + ', '.join(missing))


def main():
    partitions.check('platform/loader/an7581-w1700k-sdr-loader.dts', 'loader')
    src = uboot.prepare('loader', patches(), FILES,
                        partitions.policy_header(partitions.WRITES_FROM['loader']))
    uboot.mkimage(src)
    out = uboot.build('loader', src, DEFCONFIG)
    config = kconfig(out / '.config')
    uboot.require_config('loader', config, ENABLED, DISABLED)
    image, dtb, linked = uboot.check_image('loader', out, config, STAGING)
    check_boards(image)
    missing = [name for name in LINKED if name not in linked]
    require(not missing, 'loader: not linked: ' + ', '.join(missing))
    # OpenWrt's mtk_bmt rewrites a vendor BMT it fails to parse.
    require('mtk_bmt_attach' not in linked, 'loader: mtk_bmt is linked')
    # Untranslated access is for the read-only dumper; here a write would land
    # on other blocks than stock reads.
    require('do_bmtdetach' not in linked, 'loader: bmtdetach is linked')

    payload = BUILD / 'loader/u-boot.bin.lzma'
    write_if_changed(payload, compress(image))
    IMAGES.mkdir(parents=True, exist_ok=True)
    its = uboot.stock_fit(f'W1700K SDR persistent ARM loader, {STAGED_UPDATE}, {SECOND_LOADER}', 'loader',
                          payload, 'lzma', out / 'u-boot.dtb')
    uboot.make_fit(its, FIT, out / 'scripts/dtc', LOGS / 'loader.log')
    size = FIT.stat().st_size
    require(size <= partitions.GEOMETRY['loader'][1],
            f'loader: the FIT ({size} bytes) does not fit the loader partition')
    for base in (STAGING, STOCK_STAGING):
        require(not base <= uboot.ENTRY < base + size and not base <= uboot.FDT_LOAD < base + size,
                f'loader: a FIT staged at {base:#x} overlaps its own load addresses')
    print(f'loader: {FIT} ({size} bytes)')

    # The second loader: the same build with `loader_slot=b`.
    require(image.count(SLOT_A) == 1 and SLOT_B not in image,
            'loader: the built-in environment does not say loader_slot=a exactly once')
    require(image.count(DELAY_A) == 1 and DELAY_B not in image,
            'loader: the built-in environment does not say bootdelay=5 exactly once')
    environment = (UNIT / 'an7581_w1700k_sdr_env').read_text().splitlines()
    require(f'loader_b_kind={SECOND_KIND}' in environment and f'loader_b_at={SECOND_AT:#x}' in environment,
            'loader: the environment and build.py disagree about the second loader')
    payload_b = BUILD / 'loader/u-boot-b.bin.lzma'
    write_if_changed(payload_b, compress(image.replace(SLOT_A, SLOT_B).replace(DELAY_A, DELAY_B)))
    its = uboot.stock_fit(SECOND_KIND, 'loader', payload_b, 'lzma', out / 'u-boot.dtb')
    uboot.make_fit(its, FIT_B, out / 'scripts/dtc', LOGS / 'loader.log')
    size = FIT_B.stat().st_size
    require(size <= partitions.GEOMETRY['loader-b'][1],
            f'loader: the second FIT ({size} bytes) does not fit the loader-b partition')
    for base in (STAGING, SECOND_AT):
        require(not base <= uboot.ENTRY < base + partitions.GEOMETRY['loader-b'][1]
                and not base <= uboot.FDT_LOAD < base + partitions.GEOMETRY['loader-b'][1],
                f'loader: a second FIT staged at {base:#x} overlaps its own load addresses')
    print(f'loader: {FIT_B} ({size} bytes)')
    return 0


if __name__ == '__main__':
    main_guard(main)
