#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Package the kernel, device tree and root into the images the installer writes.

    platform/images.py

Outputs in platform/build/images/:

  w1700k-system.itb    installed in UBI volume `fit`: kernel, DTB and a small
                       bootstrap initramfs that mounts `rootfs` (a SquashFS on
                       ubiblock0_3) under a RAM overlay
  rootfs.squashfs      installed in UBI volume `rootfs` (after init/provision
                       adds the unit's keys)
  w1700k-recovery.itb  the same kernel and DTB with the root inside the
                       initramfs, booted from RAM by the loader for recovery
  durable-<part>.ubifs an empty UBIFS for the `durable` partition, one per
                       NAND part (its LEB and page sizes are in the image)

The FITs are parsed only by our loader, so their hashes are SHA-256.
"""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tarfile
import tempfile
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from buildlib import (BUILD, BUILDROOT_OUT, DTB, IMAGES, KERNEL_IMAGE, LINUX_OUT,
                      LOGS, MKIMAGE, PLATFORM, REPRODUCIBLE_ENV, ROOTFS_IMAGE, main_guard,
                      require, run, write_if_changed)
import fdt
import partitions
import uboot

SYSTEM_FIT = IMAGES / 'w1700k-system.itb'
RECOVERY_FIT = IMAGES / 'w1700k-recovery.itb'
ROOTFS = IMAGES / 'rootfs.squashfs'
LOADER_B = IMAGES / 'w1700k-loader-b.itb'    # platform/loader/build.py
UPDATE = IMAGES / 'w1700k-update.tar'
BOOTSTRAP = BUILD / 'bootstrap/bootstrap.cpio.gz'
RECOVERY_INITRD = BUILD / 'bootstrap/recovery.cpio.gz'
BUSYBOX = BUILDROOT_OUT / 'target/bin/busybox'
LOG = LOGS / 'images.log'

# Load addresses inside the 2 GiB DRAM bank, clear of the regions the device
# tree reserves (0x80000000 ATF, 0x84000000 NPU, 0x87000000 and 0x89000000 QDMA).
KERNEL_LOAD = 0x80200000
FIT_LOAD = 0x81800000      # where the loader reads or receives a FIT
FDT_LOAD = 0x83000000
INITRD_LOAD = 0x85000000
INITRD_END = 0x87000000
FIT_END = FDT_LOAD
# A fixed UBIFS identity, so the durable image is reproducible.
DURABLE_UUID = uuid.UUID('d87d243d-7e3c-5e3a-9527-2aa6bc7daf31')


# --------------------------------------------------------------------------
# The bootstrap initramfs
# --------------------------------------------------------------------------

def newc(name, mode, data=b'', inode=1, rdev=(0, 0)):
    """One `newc` cpio record, owned by root, with a zero timestamp."""
    encoded = name.encode() + b'\0'
    fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, rdev[0], rdev[1], len(encoded), 0)
    record = b'070701' + b''.join(f'{value:08x}'.encode() for value in fields) + encoded
    record += bytes(-len(record) % 4) + data
    return record + bytes(-len(data) % 4)


def bootstrap(root=None):
    """busybox, /init and the mount points; with `root`, the SquashFS too."""
    entries = [(name, stat.S_IFDIR | 0o755, b'', (0, 0))
               for name in ('bin', 'dev', 'lower', 'newroot', 'proc', 'ram', 'sbin', 'sys')]
    entries += [
        ('init', stat.S_IFREG | 0o755, (PLATFORM / 'bootstrap/init').read_bytes(), (0, 0)),
        ('bin/busybox', stat.S_IFREG | 0o755, BUSYBOX.read_bytes(), (0, 0)),
        ('bin/sh', stat.S_IFLNK | 0o777, b'busybox', (0, 0)),
        ('sbin/switch_root', stat.S_IFLNK | 0o777, b'../bin/busybox', (0, 0)),
        ('dev/console', stat.S_IFCHR | 0o600, b'', (5, 1)),
    ]
    if root is not None:
        entries.append(('rootfs.squashfs', stat.S_IFREG | 0o444, root, (0, 0)))
    archive = bytearray()
    for inode, (name, mode, data, rdev) in enumerate(sorted(entries), 1):
        archive += newc(name, mode, data, inode, rdev)
    archive += newc('TRAILER!!!', stat.S_IFREG, inode=len(entries) + 1)
    archive += bytes(-len(archive) % 512)
    return gzip.compress(bytes(archive), compresslevel=9, mtime=0)


# --------------------------------------------------------------------------
# FITs
# --------------------------------------------------------------------------

def linux_fit(description, kernel, dtb, initrd):
    def image(name, path, kind, load, extra=''):
        return f'''        {name} {{
            description = "{description} {name}";
            data = /incbin/("{path}");
            type = "{kind}"; arch = "arm64";{extra} compression = "none";
            load = <{load:#x}>;
            hash-1 {{ algo = "sha256"; }};
        }};
'''
    return ('/dts-v1/;\n/ {\n'
            f'    description = "{description}";\n    #address-cells = <1>;\n'
            '    images {\n'
            + image('kernel', kernel, 'kernel', KERNEL_LOAD,
                    f' os = "linux"; entry = <{KERNEL_LOAD:#x}>;')
            + image('fdt', dtb, 'flat_dt', FDT_LOAD)
            + image('ramdisk', initrd, 'ramdisk', INITRD_LOAD, ' os = "linux";')
            + '    };\n    configurations {\n        default = "conf-sdr";\n'
            '        conf-sdr { kernel = "kernel"; fdt = "fdt"; ramdisk = "ramdisk"; };\n'
            '    };\n};\n')


def check_layout(fit, kernel, initrd_bytes):
    """The FIT, the running kernel and the initrd must not overlap each other."""
    require(kernel[56:60] == b'ARM\x64', 'the kernel is not an ARM64 Image')
    text_offset, runtime, flags = struct.unpack_from('<QQQ', kernel, 8)
    require(not flags & 1 and KERNEL_LOAD % 0x200000 == text_offset % 0x200000,
            'the kernel would relocate away from its load address')
    require(KERNEL_LOAD + runtime <= FIT_LOAD,
            f'the running kernel ({runtime} bytes) reaches the FIT staging area')
    require(FIT_LOAD + fit.stat().st_size <= FIT_END,
            f'{fit.name} ({fit.stat().st_size} bytes) overruns its staging area')
    require(INITRD_LOAD + initrd_bytes <= INITRD_END,
            f'the {fit.name} initrd ({initrd_bytes} bytes) overruns its load window')


def check_fit(path, payloads):
    """Each image carries exactly the bytes it was built from."""
    tree = fdt.nodes(path.read_bytes())
    for name, data in payloads.items():
        require(tree[f'/images/{name}']['data'] == data,
                f'{path.name}: the {name} image differs from its input')
        require(fdt.string(tree[f'/images/{name}/hash-1']['algo']) == 'sha256',
                f'{path.name}: the {name} image is not SHA-256 hashed')


def build_fit(path, description, initrd):
    uboot.make_fit(linux_fit(description, KERNEL_IMAGE, DTB, initrd), path,
                   LINUX_OUT / 'scripts/dtc', LOG)
    check_fit(path, {'kernel': KERNEL_IMAGE.read_bytes(), 'fdt': DTB.read_bytes(),
                     'ramdisk': initrd.read_bytes()})
    check_layout(path, KERNEL_IMAGE.read_bytes(), initrd.stat().st_size)


# --------------------------------------------------------------------------
# The durable UBIFS
# --------------------------------------------------------------------------

def _node_crc(node):
    return zlib.crc32(node[8:]) ^ 0xffffffff


def normalize_ubifs(data):
    """Fix the UBIFS UUID and zero every inode timestamp, rewriting node CRCs.

    mkfs.ubifs stamps a random UUID and copies the source directory's ctime into
    the root inode; neither can be chosen from the command line.
    """
    image = bytearray(data)
    magic = struct.pack('<I', 0x06101831)
    require(image[:4] == magic, 'durable: not a UBIFS image')
    image[108:124] = DURABLE_UUID.bytes
    struct.pack_into('<I', image, 4, _node_crc(image[:struct.unpack_from('<I', image, 16)[0]]))
    cursor, inodes = 4, 0
    while (offset := image.find(magic, cursor)) >= 0:
        cursor = offset + 4
        if offset % 8 or offset + 24 > len(image):
            continue
        length = struct.unpack_from('<I', image, offset + 16)[0]
        if image[offset + 20] != 0 or length < 160 or offset + length > len(image):
            continue      # not an inode node
        node = image[offset:offset + length]
        require(struct.unpack_from('<I', node, 4)[0] == _node_crc(node),
                'durable: an inode node has a bad CRC')
        image[offset + 56:offset + 92] = bytes(36)
        struct.pack_into('<I', image, offset + 4, _node_crc(image[offset:offset + length]))
        inodes += 1
    require(inodes, 'durable: the image has no inode')
    return bytes(image)


def durable_image(nand):
    return IMAGES / nand.durable_image


def durable(nand):
    mkfs = BUILDROOT_OUT / 'host/sbin/mkfs.ubifs'
    require(mkfs.is_file(), f'{mkfs} is missing; Buildroot builds it (BR2_PACKAGE_HOST_MTD)')
    with tempfile.TemporaryDirectory(prefix='durable-', dir=BUILD) as temporary:
        tree = Path(temporary) / 'tree'
        tree.mkdir()
        os.utime(tree, (0, 0))
        raw = Path(temporary) / 'durable.ubifs'
        env = os.environ.copy()
        env.update(REPRODUCIBLE_ENV)
        run([mkfs, '-U', '-r', tree, '-m', hex(nand.page), '-e', hex(nand.leb),
             '-c', str(partitions.durable_lebs(nand)), '-x', 'zstd', '-o', raw],
            env=env, log=LOG)
        write_if_changed(durable_image(nand), normalize_ubifs(raw.read_bytes()))


def update_archive(root):
    """w1700k-update.tar: what an installed unit takes over ssh in one go (init/flash --bundle):
    the second loader and the generic root, with a manifest of their digests and of the flash
    layout they are built for. The same bytes from the same sources: no times, no owners."""
    files = {LOADER_B.name: LOADER_B.read_bytes(), ROOTFS.name: root}
    manifest = {'format': 1, 'what': 'W1700K SDR update: second loader and root',
                'partitions': {name: list(span) for name, span in partitions.GEOMETRY.items()},
                'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                          for name, data in files.items()}}
    files = {'manifest.json': (json.dumps(manifest, indent=1) + '\n').encode(), **files}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    write_if_changed(UPDATE, out.getvalue())


def main():
    for path in (KERNEL_IMAGE, DTB, ROOTFS_IMAGE, BUSYBOX, MKIMAGE, LOADER_B):
        require(path.is_file(), f'{path} is missing; build it first (see the top-level Makefile)')
    IMAGES.mkdir(parents=True, exist_ok=True)
    root = ROOTFS_IMAGE.read_bytes()
    require(len(root) <= partitions.ROOTFS_VOLUME_BYTES, 'the root does not fit its volume')
    write_if_changed(ROOTFS, root)
    write_if_changed(BOOTSTRAP, bootstrap())
    write_if_changed(RECOVERY_INITRD, bootstrap(root))
    build_fit(SYSTEM_FIT, 'W1700K SDR system', BOOTSTRAP)
    require(SYSTEM_FIT.stat().st_size <= partitions.FIT_VOLUME_BYTES,
            'the system FIT does not fit its volume')
    build_fit(RECOVERY_FIT, 'W1700K SDR recovery', RECOVERY_INITRD)
    for nand in partitions.NANDS:
        durable(nand)
    update_archive(root)
    for path in (SYSTEM_FIT, ROOTFS, RECOVERY_FIT, UPDATE,
                 *(durable_image(nand) for nand in partitions.NANDS)):
        print(f'images: {path} ({path.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    main_guard(main)
