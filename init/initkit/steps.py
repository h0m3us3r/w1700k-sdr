# SPDX-License-Identifier: MIT
"""The NAND-writing steps that installation and updates are made of.

Each step names the prompt it runs at, checks every command's status and output,
and reads back what it wrote before the next step relies on it. A failure stops
the run at once; every step can be repeated.
"""
from pathlib import Path
import shutil

from . import config, console as unit, layout, nand as parts, stock
from .console import StepFailed, command, verify_fit
from .log import sha256_file


def serve(session, path, name=None):
    """Make `path` available to both TFTP servers; returns (name, bytes, sha256)."""
    name = name or Path(path).name
    shutil.copyfile(path, session.served / name)
    return name, Path(path).stat().st_size, sha256_file(path)


def loader_network(session):
    settings = session.settings
    command(session.console, f'setenv ipaddr {settings.device_ip}; '
                             f'setenv serverip {settings.host_ip}; '
                             f'setenv tftpdstp {settings.transfer_port}', limit=20)


def padded_loader(session):
    """The loader FIT padded with 0xff to whole erase blocks; returns (path, extent)."""
    data = config.LOADER.read_bytes()
    extent = layout.loader_extent(len(data))
    path = session.log.dir / 'w1700k-loader.bin'
    path.write_bytes(data + b'\xff' * (extent - len(data)))
    return path, extent


def write_loader_slot(session):
    """At stock: write our loader into the `loader` partition, then boot that copy.

    The copy read back from NAND is compared with what was sent, its hashes are
    checked by stock, and it is booted to its prompt, so nothing that later
    makes stock boot from the slot can point at a loader that does not start.
    Returns the slot length written.
    """
    console = session.console
    console.expect_prompt('stock', timeout=10)
    image, extent = padded_loader(session)
    stock.stage(console, session.log, session.settings, image, session.served)
    for line in layout.loader_write(extent):
        command(console, line, limit=300)
    verify_fit(console, layout.READBACK)
    stock.boot(console, 'loader', layout.READBACK, interrupt=True)
    # What is written here is the loader from now on: a second loader delivered earlier
    # (init/flash --loader --ssh), or whatever stock left in that megabyte, must not be
    # started in its place.
    command(console, f"mtd erase loader-b 0x0 {config.partitions.GEOMETRY['loader-b'][1]:#x}", limit=120)
    session.log.event('loader_written', extent=extent)
    return extent


def save_bootcmd(session, extent):
    """At stock: boot our loader from the slot at power-on, and check it was saved."""
    console = session.console
    environment = stock.environment(console)
    original = environment.get('bootcmd', '')
    if "'" in original:
        raise StepFailed(f'cannot preserve a stock bootcmd containing a quote: {original!r}')
    for line in layout.save_bootcmd(extent, environment):
        command(console, line, limit=120)
    saved = stock.environment(console).get('bootcmd')
    if saved != layout.stock_bootcmd(extent):
        raise StepFailed(f'stock saved bootcmd {saved!r}')
    session.log.event('bootcmd_saved', bootcmd=saved)


def volume_matches(session, volume, size, digest):
    """True when `volume` already holds exactly these bytes."""
    try:
        command(session.console, f'ubi read {layout.READBACK:#x} {volume} {size:#x}',
                limit=300)
        text = command(session.console, f'hash sha256 {layout.READBACK:#x} {size:#x}',
                       limit=120)
    except StepFailed:
        return False
    return digest in text


def write_volume(session, volume, path, fit=False, skip_current=False):
    """At the loader, with the UBI device attached read-write: write one volume."""
    name, size, digest = serve(session, path, f'{volume}.img')
    if skip_current and volume_matches(session, volume, size, digest):
        print(f'  {volume}: already current')
        return False
    print(f'  {volume}: writing {Path(path).name} ({size} bytes)')
    for line, expect in layout.write_volume(volume, name, size, digest, fit=fit):
        command(session.console, line, expect=expect, limit=600, heartbeat=30)
    session.log.event('volume_written', volume=volume, bytes=size, sha256=digest)
    return True


def attach(session, partition):
    command(session.console, f'ubi part.rw {partition}', expect='selected read-write',
            strict=False, limit=900)


def detach(session):
    command(session.console, 'ubi detach', limit=60)


def loader_nand(session):
    """At the loader: the NAND part, from `mtd list`."""
    listing = session.console.must_run('mtd list', limit=30)
    session.log.text('mtd-list.txt', listing)
    return parts.identify(listing)


def format_system(session, fit, rootfs, nand):
    """At the loader: erase `system`, create its volumes, write the FIT and root."""
    console = session.console
    console.expect_prompt('loader', timeout=10)
    detach(session)
    for line in layout.erase('system'):
        command(console, line, limit=300, heartbeat=30)
    attach(session, 'system')
    for line in layout.create_system_volumes(nand):
        command(console, line, expect='Creating', limit=300)
    loader_network(session)
    write_volume(session, 'fit', fit, fit=True)
    write_volume(session, 'rootfs', rootfs)
    detach(session)


def format_durable(session, nand):
    """At the loader: erase `durable` and write the part's empty UBIFS volume to it."""
    console = session.console
    console.expect_prompt('loader', timeout=10)
    detach(session)
    for line in layout.erase('durable'):
        command(console, line, limit=300, heartbeat=30)
    attach(session, 'durable')
    for line in layout.create_durable_volume():
        command(console, line, expect='Creating', limit=300)
    loader_network(session)
    write_volume(session, 'durable', config.durable(nand))
    detach(session)


def boot_installed(session):
    """Boot Linux from NAND and check its storage; returns the startup status."""
    status = unit.boot_linux(session.console, session.log)
    command(session.console, '/usr/sbin/verify-storage', expect='W1700K_STORAGE_READY',
            limit=120)
    return status


def boot_recovery(session):
    """At the loader: boot the recovery FIT from RAM, leaving NAND untouched."""
    console = session.console
    loader_network(session)
    name, size, digest = serve(session, config.RECOVERY_FIT)
    command(console, f'tftpboot {layout.LOAD:#x} {name}',
            expect=rf'Bytes transferred = {size}\b', limit=600, heartbeat=30)
    command(console, f'hash sha256 {layout.LOAD:#x} {size:#x}', expect=digest, limit=120)
    verify_fit(console, layout.LOAD, 'sha256')
    command(console, f"setenv bootargs '{layout.recovery_bootargs()}'", limit=20)
    return unit.boot_linux(console, session.log, f'bootm {layout.LOAD:#x}#conf-sdr')
