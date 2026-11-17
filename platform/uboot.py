# SPDX-License-Identifier: MIT
"""Shared steps of the U-Boot builds (loader, dumper, restorer) and of mkimage."""
from pathlib import Path
import shutil
import struct

from buildlib import (BUILD, CROSS_COMPILE, LOGS, MKIMAGE, SRC, apply_patches, build_env,
                      copy_if_changed, jobs, mark_prepared, output, patch_stamp, prepared,
                      require, run, sources, write_if_changed)
import fetch
import partitions

ENTRY = 0x80200000           # where stock and our loader place the payload
FDT_LOAD = 0x82000000
RAM_END = 0x100000000
SHARED_PATCHES = sorted((Path(__file__).resolve().parent / 'patches').glob('*.patch'))
# The board support patch (entry address, single 2 GiB DRAM bank) every image uses.
BOARD_PATCH = (Path(__file__).resolve().parent /
               'loader/patches/0001-w1700k-sdr-entry-and-dram.patch')


def prepare(name, patches, files, policy=None):
    """A U-Boot tree with `patches` applied, `files` ({dest: source}) copied in and
    the BMT write policy header (`policy`, default our loader's) generated."""
    src = SRC / f'u-boot-{name}'
    stamp = patch_stamp(patches) + sources()['u-boot']['sha256']
    if not prepared(src, stamp):
        print(f'{name}: preparing the U-Boot source tree')
        fetch.extract(fetch.uboot_archive(), src)
        apply_patches(src, patches, LOGS / f'{name}.log')
        mark_prepared(src, stamp)
    for dest, source in files.items():
        copy_if_changed(source, src / dest)
    write_if_changed(src / 'drivers/mtd/nand/airoha_bmt_policy.h',
                     policy or partitions.policy_header())
    return src


def build(name, src, defconfig):
    """Configure and build u-boot.bin and its control DTB; returns the output dir."""
    out = BUILD / name
    make = ['make', '-C', src, f'O={out}', f'CROSS_COMPILE={CROSS_COMPILE}']
    log = LOGS / f'{name}.log'
    env = build_env()
    run(make + [defconfig], env=env, log=log)
    run(make + [f'-j{jobs()}', 'u-boot.bin', 'u-boot.dtb'], env=env, log=log)
    return out


def mkimage(src):
    """mkimage and dtc from the pinned U-Boot, built for the host once for each U-Boot pinned:
    the tools of an earlier pin are not the ones this build says it makes its images with."""
    out = MKIMAGE.parents[1]
    log = LOGS / 'mkimage.log'
    pin = sources()['u-boot']['sha256'] + '\n'
    made_from = out / 'u-boot.pin'
    if not MKIMAGE.is_file() or not made_from.is_file() or made_from.read_text() != pin:
        if out.is_dir():
            shutil.rmtree(out)
        make = ['make', '-C', src, f'O={out}']
        env = build_env()
        run(make + ['tools-only_defconfig'], env=env, log=log)
        run([src / 'scripts/config', '--file', out / '.config', '--disable',
             'TOOLS_LIBCRYPTO', '--disable', 'TOOLS_MKEFICAPSULE', '--disable',
             'TOOLS_KWBIMAGE'], env=env, log=log)
        run(make + ['olddefconfig'], env=env, log=log)
        run(make + [f'-j{jobs()}', 'tools-only'], env=env, log=log)
        made_from.write_text(pin)
    return MKIMAGE


def make_fit(its, fit, dtc_dir, log):
    """Compile an image tree source into `fit` with mkimage; dtc comes from `dtc_dir`."""
    source = BUILD / 'its' / (fit.stem + '.its')
    write_if_changed(source, its)
    run([MKIMAGE, '-f', source, fit], env=build_env([dtc_dir]), log=log)
    return fit


def symbols(elf):
    """{name: address} of the defined symbols of a linked ELF."""
    found = {}
    for line in output([CROSS_COMPILE + 'nm', '-n', elf]).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] not in 'Uw':
            found[parts[2]] = int(parts[0], 16)
    return found


def check_image(name, out, config, limit):
    """The linked layout: ARM64 Image header, entry branch and runtime extent.

    Stock jumps to ENTRY with the payload decompressed there; the runtime
    (code, BSS and the early stack) must end below `limit`, where the caller
    stages the next image.
    """
    image = (out / 'u-boot.bin').read_bytes()
    nodtb = (out / 'u-boot-nodtb.bin').read_bytes()
    dtb = (out / 'u-boot.dtb').read_bytes()
    linked = symbols(out / 'u-boot')
    require(image == nodtb + dtb and len(nodtb) == linked['_end'] - ENTRY,
            f'{name}: the control DTB is not appended at _end')
    require(image[56:60] == b'ARM\x64', f'{name}: no ARM64 Image header')
    offset, size, flags = struct.unpack_from('<QQQ', image, 8)
    stack = int(config['CONFIG_SYS_INIT_SP_BSS_OFFSET'], 0)
    expected = linked['__bss_start'] - ENTRY + max(linked['__bss_end'] - linked['__bss_start'],
                                                   stack)
    require(offset == 0 and flags == 0xa and size == expected,
            f'{name}: Image header does not describe the linked runtime')
    require(ENTRY + size <= limit, f'{name}: runtime reaches {ENTRY + size:#x}, past {limit:#x}')
    branch = struct.unpack_from('<I', image)[0]
    displacement = branch & 0x3ffffff
    displacement -= (displacement & 0x2000000) << 1
    require(branch & 0xfc000000 == 0x14000000 and
            ENTRY + 4 * displacement == linked['reset'],
            f'{name}: the first instruction does not branch to reset')
    return image, dtb, linked


def require_config(name, config, enabled=(), disabled=()):
    missing = [key for key in enabled if config.get('CONFIG_' + key) != 'y']
    present = [key for key in disabled if config.get('CONFIG_' + key, 'n') != 'n']
    require(not missing, f'{name}: required options are off: {", ".join(missing)}')
    require(not present, f'{name}: forbidden options are on: {", ".join(present)}')


def stock_fit(description, node, payload, compression, dtb_path):
    """A FIT stock U-Boot can boot: one ARM64 'kernel' at ENTRY plus our control DTB.

    Stock U-Boot 2014.04 verifies one SHA-1 hash per image and fails on an
    algorithm it does not know, so every image carries exactly that.
    """
    return f'''/dts-v1/;
/ {{
    description = "{description}";
    #address-cells = <1>;
    images {{
        {node} {{
            data = /incbin/("{payload}");
            type = "kernel"; arch = "arm64"; os = "linux"; compression = "{compression}";
            load = <{ENTRY:#x}>; entry = <{ENTRY:#x}>;
            hash-1 {{ algo = "sha1"; }};
        }};
        fdt {{
            data = /incbin/("{dtb_path}");
            type = "flat_dt"; arch = "arm64"; compression = "none";
            load = <{FDT_LOAD:#x}>;
            hash-1 {{ algo = "sha1"; }};
        }};
    }};
    configurations {{
        default = "conf-uboot";
        conf-uboot {{ kernel = "{node}"; fdt = "fdt"; }};
    }};
}};
'''
