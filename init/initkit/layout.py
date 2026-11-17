# SPDX-License-Identifier: MIT
"""The U-Boot commands that lay out and write the unit's NAND.

Pure functions from the layout in platform/partitions.py to command strings, so
the exact sequences can be tested without a unit. All offsets are vendor-logical:
stock's `flash` commands and our loader's `mtd`/`ubi` commands translate them
through the vendor bad-block tables identically.
"""
import re

from .config import partitions

STOCK_STAGE = 0x89000000     # where stock receives and reads the loader
LOAD = 0x81800000            # where our loader receives images (its loadaddr)
READBACK = 0x94000000        # a second buffer, so a readback is never the staged copy
ERASE_STEP = 0x1000000       # one `mtd erase` per 16 MiB keeps each command bounded
LOADER_OFFSET, LOADER_SLOT = partitions.GEOMETRY['loader']


def loader_extent(size):
    """The loader FIT's length rounded up to whole erase blocks of every part
    (256 KiB), so one extent serves the W25N04KV and the W25N04LV."""
    extent = -(-size // partitions.ALIGNMENT) * partitions.ALIGNMENT
    if extent > LOADER_SLOT:
        raise ValueError(f'the loader ({size} bytes) does not fit its {LOADER_SLOT}-byte slot')
    return extent


def stock_bootcmd(extent):
    """What stock U-Boot runs at power-on once installed: load and boot our loader.

    Both addresses are explicit: stock's `flash read` with no destination reads
    to a default address that a bare `bootm` would not use.
    """
    return (f'flash read {LOADER_OFFSET:#x} {extent:#x} {STOCK_STAGE:#x}; '
            f'bootm {STOCK_STAGE:#x}')


def bootcmd_extent(bootcmd):
    """The loader length an installed stock bootcmd reads, or None if it is stock's own."""
    prefix = f'flash read {LOADER_OFFSET:#x} '
    if not bootcmd or not bootcmd.startswith(prefix):
        return None
    extent = int(bootcmd[len(prefix):].split()[0], 16)
    return extent if bootcmd == stock_bootcmd(extent) else None


def loader_write(extent):
    """At stock, with the padded loader at STOCK_STAGE: write the slot and read it back."""
    return [f'flash erase {LOADER_OFFSET:#x} {extent:#x}',
            f'flash write {LOADER_OFFSET:#x} {extent:#x} {STOCK_STAGE:#x}',
            f'flash read {LOADER_OFFSET:#x} {extent:#x} {READBACK:#x}',
            f'cmp.b {STOCK_STAGE:#x} {READBACK:#x} {extent:#x}']


def save_bootcmd(extent, current):
    """At stock: make it boot our loader, keeping its own bootcmd as bootcmd_orig.

    `current` is stock's environment. A bootcmd that already loads our loader is
    not saved as the original.
    """
    commands = []
    original = current.get('bootcmd')
    if original and bootcmd_extent(original) is None:
        commands.append(f"setenv bootcmd_orig '{original}'")
    commands += [f"setenv bootcmd '{stock_bootcmd(extent)}'", 'saveenv']
    return commands


def erase(partition):
    """Erase a whole partition in bounded steps."""
    _, length = partitions.GEOMETRY[partition]
    return [f'mtd erase {partition} {offset:#x} {min(ERASE_STEP, length - offset):#x}'
            for offset in range(0, length, ERASE_STEP)]


def create_system_volumes(nand):
    """The `system` UBI volumes of `nand`, in id order."""
    commands = []
    for volume_id, (name, kind, lebs) in enumerate(partitions.system_volumes(nand)):
        size = partitions.ENV_BYTES if name.startswith('ubootenv') else lebs * nand.leb
        commands.append(f"ubi create {name} {size:#x} {'s' if kind == 'static' else 'd'} "
                        f'{volume_id}')
    return commands


def create_durable_volume():
    return ['ubi create durable - d 0']


def write_volume(volume, name, size, digest, fit=False):
    """Receive `name`, check it, write it to `volume`, read it back and check again.

    Returns (command, expected-output regex) pairs.
    """
    steps = [(f'tftpboot {LOAD:#x} {name}', rf'Bytes transferred = {size}\b'),
             (f'hash sha256 {LOAD:#x} {size:#x}', digest)]
    if fit:
        steps.append((f'iminfo {LOAD:#x}', r'sha256\+'))
    steps += [(f'ubi write {LOAD:#x} {volume} {size:#x}', rf'{size} bytes written'),
              (f'ubi read {READBACK:#x} {volume} {size:#x}', rf'Read {size} bytes'),
              (f'hash sha256 {READBACK:#x} {size:#x}', digest)]
    return steps


def recovery_bootargs():
    """Kernel arguments for the recovery FIT: root from RAM, no UBI attached."""
    return 'console=ttyS0,115200 earlycon panic=5 ro rdinit=/init'


LISTED = re.compile(r'0x([0-9a-f]+)-0x([0-9a-f]+) : "([^"]+)"')
LISTED_DEVICE = re.compile(r'^\s*\* (\S+)', re.M)


def listed(text):
    """{partition: (offset, length)} from a loader's `mtd list`. The chip itself (`* spi-nand0`),
    listed by its own name before its partitions, is left out."""
    devices = set(LISTED_DEVICE.findall(text))
    return {name: (int(start, 16), int(end, 16) - int(start, 16))
            for start, end, name in LISTED.findall(text) if name not in devices}


def differing(found, wanted):
    """The partitions that are not the same in both, by name."""
    return sorted(name for name in set(found) | set(wanted) if found.get(name) != wanted.get(name))


def laid_out_differently(names):
    """What a tool says of a unit whose flash is divided otherwise than this build's images
    expect, before it has written anything. A root built for another layout looks for its
    durable storage in the wrong place, a kernel for its root, and a loader would write there."""
    return ("the unit's flash is not laid out as these images expect (" + ', '.join(names)
            + '): it was installed from another version. Install it again over the serial '
            'console (init/install --force); init/README.md says what else an older '
            'installation needs')
