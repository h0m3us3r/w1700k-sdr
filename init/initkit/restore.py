# SPDX-License-Identifier: MIT
"""Return an installed unit to its stock operating state from its backup.

    1. stop stock U-Boot and boot the restorer from RAM through it, as the
       installer boots the dumper, so a unit whose installed loader no longer
       starts is restored all the same
    2. (the restorer lifts the switch's rate limit; it then receives at ~20 MiB/s)
    3. check that the unit is the backup's
    4. send the backup gzip-compressed, unpack it into RAM and check that RAM
       holds the backup exactly (its SHA-256)
    5. check that the vendor partitions nothing here writes (`bootloader`,
       `calibration`) already hold the backup's bytes
    6. on the device, compare every block of stock's images, then of stock's
       environment (`uenv`, last, so an interrupted run still boots our loader),
       with RAM, and erase, write and read back only the blocks that differ,
       through the vendor BBT/BMT with the chip's ECC on (`nandsync`)
    7. compare everything written once more: no block may differ
    8. reset, and watch stock U-Boot start stock's kernel

With `stock_only` (init/restore --stock-slot) only steps 1 to 5 and then the
`stock` partition are done: stock's second firmware slot and its own UBI behind
it, which nothing of ours occupies. That puts back what an installation under
the layout before November 2026 overwrote there (its `durable` reached 18 MiB
into the second slot), leaves our system as it is, and boots it again.

The backup is the vendor-logical data the dumper read (backup.py), so this puts
back every byte stock firmware reads. The chip's own state (bad blocks, the
reserve pool) stays as the vendor tables describe it now. Nothing is read back
to the host, and NAND blocks that already hold the backup are not rewritten.
"""
import gzip
import re

from . import backup, config, nand as parts, stock
from . import console as unit
from .config import partitions
from .console import StepFailed, command
from .log import sha256_file
from .session import open_session

RESTORER = config.IMAGES / 'w1700k-restorer.itb'
BUFFER = 0x90000000                 # the restorer's backup buffer
PACKED = 0xac000000                 # and where the compressed backup lands
PACKED_NAME = 'backup.gz'
# `bootloader` and `calibration` are never written; they must already match the
# backup.
KEPT = (partitions.GEOMETRY['bootloader'], partitions.GEOMETRY['calibration'])
STOCK_KERNEL = re.compile(r'Starting kernel')
SYNCED = re.compile(r'nandsync: (\d+) blocks, (\d+) (?:rewritten|differ)')


def writes(size):
    """(offset, length) of what may be written, in order: stock's images over
    ours, then its environment last."""
    first = partitions.GEOMETRY['loader'][0]
    return ((first, size - first), partitions.GEOMETRY['uenv'])


def stock_span(size):
    """(offset, length) of the `stock` partition within a backup of `size` bytes."""
    first = partitions.GEOMETRY['stock'][0]
    return (first, size - first)


def plan_is_safe(size):
    """The writable spans and the kept partitions tile the backup exactly once."""
    covered = sorted(writes(size) + KEPT)
    edges = [start for start, _ in covered] + [covered[-1][0] + covered[-1][1]]
    return (edges[0] == 0 and edges[-1] == size and
            all(a + b == c for (a, b), c in zip(covered, edges[1:])))


def nandsync(console, offset, length, check, block):
    """Blocks (of `block` bytes) examined and blocks that differed (rewritten
    unless `check`)."""
    verb = 'nandsync.check' if check else 'nandsync'
    text = command(console, f'{verb} spi-nand0 {BUFFER + offset:#x} {offset:#x} {length:#x}',
                   limit=3600, heartbeat=30)
    counts = SYNCED.search(text)
    if not counts or int(counts.group(1)) != length // block:
        raise StepFailed(f'{verb} at {offset:#x} did not report every block:\n{text[-600:]}')
    return int(counts.group(2))


def boot_restorer(session, state):
    """From any state, through stock U-Boot: the restorer staged into RAM by
    stock's TFTP and booted from there. Our installed loader is not used.
    Stock's own boot is seen, for the vendor pool it reports."""
    console, log = session.console, session.log
    unit.to_stock(console, log, state, fresh=True)
    stock.stage(console, log, session.settings, RESTORER, session.served)
    stock.boot(console, 'restorer')
    log.event('transition', frm='stock', to='restorer')


def check_unit(session, accept_untested=False):
    """At the restorer: the chip is a supported part (run on a unit before, or
    accepted), translated, writable, with the vendor pool stock reported at its
    boot; returns (serial, part)."""
    console, log = session.console, session.log
    settings = session.settings
    command(console, f'setenv ipaddr {settings.device_ip}; '
                     f'setenv serverip {settings.host_ip}; '
                     f'setenv tftpdstp {settings.transfer_port}', limit=20)
    listing = console.must_run('mtd list', limit=30)
    log.text('mtd-list.txt', listing)
    nand = parts.identify(listing)
    parts.require_tested(nand, accept_untested, verb='restore')
    backup.check_translated(listing)
    if 'writeable' not in listing:
        raise StepFailed('the restorer does not report the chip writable:\n' + listing[-600:])
    parts.check_pool(nand, console.view(), listing, verb='restore')
    return backup.read_serial(console), nand


def choose_backup(settings, serial, backup_path=None, expected=None, force=False):
    """The backup to restore and its SHA-256: the one named, or init/install's
    backup of the unit that reported `serial`. Its serial must be the unit's,
    unless `force`, which only reports a mismatch."""
    def mismatch(message):
        if not force:
            raise StepFailed(message + '; nothing was written (--force restores it anyway)')
        print(f'restore: WARNING: {message}; restoring anyway (--force)', flush=True)

    if expected and expected != serial:
        mismatch(f'this unit is {serial}, not {expected}')
    if backup_path is None:
        backup_path = config.unit_dir(settings.data_dir, expected or serial) / backup.BACKUP_NAME
    if not backup_path.is_file():
        raise StepFailed(f'no backup of {expected or serial} at {backup_path}; '
                         'nothing was written')
    if backup_path.stat().st_size != backup.DUMP_BYTES:
        raise StepFailed(f'{backup_path} is not a {backup.DUMP_BYTES}-byte backup; '
                         'nothing was written')
    owner = backup.serial_in_backup(backup_path)
    if owner != serial:
        mismatch(f'this unit is {serial}; {backup_path} is of {owner}')
    return backup_path, sha256_file(backup_path)


def load_backup(session, backup_path, digest):
    """The backup, gzip-compressed over TFTP, unpacked into BUFFER and checked."""
    console, size = session.console, backup.DUMP_BYTES
    packed = gzip.compress(backup_path.read_bytes(), 6, mtime=0)
    (session.served / PACKED_NAME).write_bytes(packed)
    session.log.event('packed', bytes=len(packed))
    print(f'  sending the backup ({len(packed) / 2**20:.0f} MiB compressed)', flush=True)
    command(console, f'tftpboot {PACKED:#x} {PACKED_NAME}',
            expect=rf'Bytes transferred = {len(packed)}\b', limit=900, heartbeat=15)
    command(console, f'unzip {PACKED:#x} {BUFFER:#x} {size:#x}',
            expect=rf'Uncompressed size: {size}\b', limit=300, heartbeat=15)
    command(console, f'hash sha256 {BUFFER:#x} {size:#x}', expect=digest, limit=300,
            heartbeat=15)


def check_kept(console, block, force=False):
    """`bootloader` and `calibration` already hold the backup's bytes. They are
    never written. With `force` (another unit's backup, or a damaged partition)
    a difference, or a read that fails, is only reported."""
    for offset, length in KEPT:
        where = f'the unit\'s {offset:#x}+{length:#x}'
        try:
            differ = nandsync(console, offset, length, check=True, block=block)
        except StepFailed as error:
            if not force:
                raise
            print(f'restore: WARNING: {where} could not be compared ({error}); '
                  'continuing (--force)', flush=True)
            continue
        if differ and not force:
            raise StepFailed(f'{where} differs from the backup there; this tool does not '
                             'write it, so it stops here with nothing written (--force '
                             'restores the rest anyway)')
        if differ:
            print(f'restore: WARNING: {differ} blocks of {where} differ from the backup; '
                  'left as they are (never written), continuing (--force)', flush=True)


def sync(session, block, spans=None):
    """Rewrite only the blocks that differ, `uenv` last; then none may differ."""
    console, log = session.console, session.log
    rewritten = 0
    spans = spans or writes(backup.DUMP_BYTES)
    for offset, length in spans:
        print(f'  syncing {offset:#x}+{length:#x} (progress in the console log)', flush=True)
        count = nandsync(console, offset, length, check=False, block=block)
        log.event('synced', offset=offset, length=length, rewritten=count)
        rewritten += count
    print(f'  {rewritten} blocks rewritten; comparing everything again', flush=True)
    for offset, length in spans:
        if nandsync(console, offset, length, check=True, block=block):
            raise StepFailed(f'{offset:#x}+{length:#x} still differs from the backup')
    log.text('bmtstats.txt', console.must_run('bmtstats', limit=20))
    return rewritten


def watch_stock(console, log, limit=180):
    """Reset and let stock U-Boot boot its kernel, transmitting nothing."""
    start = console.mark()
    console.send_async('reset')
    started = console.wait(lambda: STOCK_KERNEL.search(console.view(start)), limit,
                           'stock kernel start')
    log.text('stock-boot.txt', console.view(start))
    return started


def run(settings, backup_path=None, serial=None, boot=True, force=False,
        accept_untested=False, stock_only=False):
    """Restore the connected unit from `backup_path`, or else from init/install's
    backup of the unit (`serial`, when given, must be the connected unit's).
    `stock_only` writes back the `stock` partition alone and boots our system.
    `force` reports, instead of stopping at, a serial that is not the unit's and
    `bootloader`/`calibration` contents that differ from the backup's (still
    never written). A NAND part never run on a unit needs `accept_untested`."""
    config.require_images(RESTORER)
    if not plan_is_safe(backup.DUMP_BYTES):
        raise StepFailed('internal: the restore plan does not tile the backup')
    if backup_path is not None and not backup_path.is_file():
        raise StepFailed(f'no backup at {backup_path}')
    with open_session(settings, 'restore', write=True) as session:
        console, log = session.console, session.log
        print('restore: booting the restorer through stock U-Boot', flush=True)
        boot_restorer(session, unit.identify(console, log))
        found, nand = check_unit(session, accept_untested)
        backup_path, digest = choose_backup(settings, found, backup_path, serial, force)
        log.event('backup', serial=found, path=str(backup_path), sha256=digest, nand=nand.name)
        print(f'restore: unit {found} ({nand.name}), backup {backup_path}', flush=True)
        load_backup(session, backup_path, digest)
        check_kept(console, nand.block, force)
        if stock_only:
            span = stock_span(backup.DUMP_BYTES)
            differ = nandsync(console, *span, check=True, block=nand.block)
            print(f"restore: {differ} blocks of stock's second slot and data differ from the backup"
                  + ('; writing them back' if differ else '; nothing to write'), flush=True)
            rewritten = sync(session, nand.block, (span,)) if differ else 0
            log.event('stock_slot', differed=differ, rewritten=rewritten)
            status = unit.boot_linux(console, log, command_line='reset') if boot else None
            print("restore: done; stock's second slot and data are the backup's"
                  + (f'; the installed system runs ({status})' if boot else
                     '; the unit waits at the restorer prompt'), flush=True)
            return 0
        print('restore: writing back what differs', flush=True)
        sync(session, nand.block)
        print('restore: the NAND holds the backup', flush=True)
        if boot and not watch_stock(console, log):
            raise StepFailed('stock U-Boot did not start its kernel after the restore; '
                             f'see {log.dir}/stock-boot.txt')
    print('restore: done; ' + ('the unit runs its stock firmware' if boot else
          'the unit waits at the restorer prompt (stock boots at the next reset)'),
          flush=True)
    return 0
