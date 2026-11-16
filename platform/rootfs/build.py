#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the cross toolchain and the unit's root filesystem with Buildroot.

    platform/rootfs/build.py toolchain   # configure Buildroot, build toolchain and packages
    platform/rootfs/build.py image       # add the driver and clients, build rootfs.squashfs

Buildroot is only the cross-build tool: the unit runs musl, BusyBox init,
Dropbear and the SDR programs from a read-only SquashFS. `toolchain` also builds
the host tools the kernel and U-Boot builds use (bison, flex, mtd-utils).
`image` stages the overlay with the kernel module and the clients built by
`make -C platform/linux module reader`, then lets Buildroot repack the image.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

UNIT = Path(__file__).resolve().parent
sys.path.insert(0, str(UNIT.parent))
from buildlib import (BUILD, BUILDROOT_OUT, DOWNLOADS, HOST_BIN, LOGS, PLATFORM, ROOT,
                      ROOTFS_IMAGE, REPRODUCIBLE_ENV, BuildError, kconfig, main_guard,
                      require, run, write_if_changed)
import fetch
import partitions

OVERLAY = BUILD / 'rootfs-overlay'
DEFCONFIG = BUILDROOT_OUT / 'w1700k_defconfig'
MODULE = ROOT / 'kernel/w1700k_sdr/build/w1700k_sdr.ko'
CLIENT_DIR = ROOT / 'kernel/w1700k_sdr/build'
CLIENTS = ('mt7996-probe', 'mt7996-download', 'mt7996-stream', 'mt7996-ctl', 'w1700k-led',
           'w1700k-key', 'w1700k-gps')
LICENSE = ROOT / 'kernel/w1700k_sdr/LICENSE.mt76'
# Host tools that the kernel and U-Boot builds take from Buildroot.
HOST_PACKAGES = ('host-bison', 'host-flex')
REQUIRED = ('BR2_aarch64=y', 'BR2_REPRODUCIBLE=y', 'BR2_TOOLCHAIN_BUILDROOT_MUSL=y',
            'BR2_INIT_BUSYBOX=y', 'BR2_TARGET_ROOTFS_SQUASHFS=y',
            'BR2_TARGET_ROOTFS_SQUASHFS4_ZSTD=y', 'BR2_PACKAGE_DROPBEAR=y',
            'BR2_PACKAGE_HOST_MTD=y')
LOG = LOGS / 'rootfs.log'


def install_shim():
    """A directory holding a GNU `install`, for hosts whose default is uutils.

    Buildroot rejects the uutils implementation; this selects GNU coreutils for
    the Buildroot build only, without touching system alternatives.
    """
    for name in ('gnuinstall', 'ginstall', 'install'):
        found = shutil.which(name)
        if found and 'GNU coreutils' in subprocess.run(
                [found, '--version'], capture_output=True, text=True).stdout:
            break
    else:
        raise BuildError('Buildroot needs GNU coreutils `install` '
                         '(install, ginstall or gnuinstall)')
    shim = BUILD / 'host-shim'
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / 'install'
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path(found).resolve())
    return shim


def buildroot_make(*targets):
    env = os.environ.copy()
    env.update(REPRODUCIBLE_ENV)
    env['PATH'] = str(install_shim()) + os.pathsep + env['PATH']
    # Buildroot refuses to run with these set in the environment.
    for name in ('PERL_MM_OPT', 'CROSS_COMPILE', 'ARCH'):
        env.pop(name, None)
    run(['make', '-C', BUILDROOT_OUT, *targets], env=env, log=LOG)


def fw_env_config():
    """/etc/fw_env.config, derived from the loader's environment location.

    A file that disagrees with U-Boot would corrupt the redundant pair, so it is
    generated from the loader defconfig rather than maintained beside it.
    """
    config = kconfig(PLATFORM / 'loader/an7581_w1700k_sdr_defconfig')
    require(config.get('CONFIG_ENV_IS_IN_UBI') == 'y' and
            config.get('CONFIG_ENV_REDUNDANT') == 'y',
            'the loader environment is not a redundant UBI pair')
    size = int(config['CONFIG_ENV_SIZE'], 0)
    primary = config['CONFIG_ENV_UBI_VOLUME'].strip('"')
    secondary = config['CONFIG_ENV_UBI_VOLUME_REDUND'].strip('"')
    return ('# Generated from the loader defconfig; must match U-Boot.\n'
            '# <device> <offset> <size>\n'
            f'/dev/ubi0:{primary} 0x0 {size:#x}\n'
            f'/dev/ubi0:{secondary} 0x0 {size:#x}\n')


def overlay_files(with_sdr):
    """Every overlay file as {relative path: (bytes, mode)}."""
    files = {}
    templates = UNIT / 'overlay'
    for path in sorted(templates.rglob('*')):
        if path.is_file():
            files[str(path.relative_to(templates))] = (path.read_bytes(),
                                                       path.stat().st_mode & 0o777)
    files['etc/fw_env.config'] = (fw_env_config().encode(), 0o644)
    if with_sdr:
        missing = [path for path in [MODULE, *(CLIENT_DIR / name for name in CLIENTS)]
                   if not path.is_file()]
        require(not missing, 'missing ' + ', '.join(str(p.relative_to(ROOT)) for p in missing)
                + '; run make -C platform/linux module reader')
        files['lib/modules/w1700k_sdr.ko'] = (MODULE.read_bytes(), 0o644)
        for name in CLIENTS:
            files[f'usr/sbin/{name}'] = ((CLIENT_DIR / name).read_bytes(), 0o755)
        files['usr/share/licenses/w1700k_sdr/LICENSE.mt76'] = (LICENSE.read_bytes(), 0o644)
    return files


def stage_overlay(files):
    """Replace the staged overlay, and drop from target/ what the old one installed.

    Buildroot copies the overlay into target/ but never removes a file an earlier
    overlay put there, so a file dropped from the overlay would otherwise stay in
    every later image.
    """
    previous = ({str(p.relative_to(OVERLAY)) for p in OVERLAY.rglob('*') if p.is_file()}
                if OVERLAY.is_dir() else set())
    if OVERLAY.exists():
        shutil.rmtree(OVERLAY)
    for name, (data, mode) in files.items():
        target = OVERLAY / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
    for name in sorted(previous - set(files)):
        stale = BUILDROOT_OUT / 'target' / name
        if stale.is_file() or stale.is_symlink():
            stale.unlink()


def configure():
    """Generate the Buildroot defconfig and apply it when it changed."""
    source = fetch.buildroot()
    seed = (UNIT / 'config.seed').read_text()
    seed += (f'BR2_ROOTFS_OVERLAY="{OVERLAY}"\n'
             f'BR2_PACKAGE_BUSYBOX_CONFIG_FRAGMENT_FILES="{UNIT / "busybox.fragment"}"\n'
             f'BR2_DL_DIR="{DOWNLOADS / "buildroot"}"\n')
    changed = write_if_changed(DEFCONFIG, seed)
    if changed or not (BUILDROOT_OUT / '.config').is_file():
        run(['make', '-C', source, f'O={BUILDROOT_OUT}', f'BR2_DEFCONFIG={DEFCONFIG}',
             'defconfig'], log=LOG)
    config = (BUILDROOT_OUT / '.config').read_text().splitlines()
    dropped = [line for line in REQUIRED if line not in config]
    require(not dropped, 'Buildroot dropped required options: ' + ', '.join(dropped))


def check_image():
    data = ROOTFS_IMAGE.read_bytes()
    require(data[:4] == b'hsqs', f'{ROOTFS_IMAGE} is not a SquashFS image')
    require(struct.unpack_from('<I', data, 12)[0] == 131072 and
            struct.unpack_from('<H', data, 20)[0] == 6,
            'the root is not a 128 KiB-block zstd SquashFS')
    require(len(data) <= partitions.ROOTFS_VOLUME_BYTES,
            f'the root is {len(data)} bytes, over the {partitions.ROOTFS_VOLUME_BYTES}-byte '
            'rootfs volume')
    return len(data)


def current(path, *parts):
    """True when the marker file `path` records the digest of exactly these inputs."""
    return path.is_file() and path.read_text() == digest(*parts)


def digest(*parts):
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part if isinstance(part, bytes) else part.encode())
        hasher.update(b'\0')
    return hasher.hexdigest()


def toolchain():
    configure()
    inputs = (DEFCONFIG.read_text(), (UNIT / 'busybox.fragment').read_text())
    marker = BUILDROOT_OUT / '.w1700k-toolchain'
    if current(marker, *inputs) and (HOST_BIN / 'bison').is_file():
        print('rootfs: toolchain is current')
        return
    if not OVERLAY.is_dir():
        stage_overlay(overlay_files(with_sdr=False))
    buildroot_make()
    buildroot_make(*HOST_PACKAGES)
    marker.write_text(digest(*inputs))
    print(f'rootfs: toolchain and packages built in {BUILDROOT_OUT}')


def image():
    configure()
    files = overlay_files(with_sdr=True)
    inputs = [DEFCONFIG.read_text()] + [name.encode() + mode.to_bytes(2, 'little') + data
                                        for name, (data, mode) in sorted(files.items())]
    marker = BUILDROOT_OUT / '.w1700k-image'
    if not (current(marker, *inputs) and ROOTFS_IMAGE.is_file()):
        stage_overlay(files)
        buildroot_make()
        marker.write_text(digest(*inputs))
    size = check_image()
    print(f'rootfs: {ROOTFS_IMAGE} ({size} bytes)')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('step', choices=('toolchain', 'image'))
    args = parser.parse_args()
    os.umask(0o022)
    {'toolchain': toolchain, 'image': image}[args.step]()
    return 0


if __name__ == '__main__':
    main_guard(main)
