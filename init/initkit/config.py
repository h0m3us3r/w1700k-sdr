# SPDX-License-Identifier: MIT
"""Installer settings, file locations and the built images it uses.

Settings come from ~/.config/w1700k/install.json (see init/install.example.json),
then from command-line flags. Everything kept per unit (its NAND backup, SSH host
key and network settings) lives in the unit directory, named by the unit's serial:
$XDG_DATA_HOME/w1700k/units/<serial>/ (default ~/.local/share/w1700k/...).
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'platform'))
import partitions  # noqa: E402  the NAND layout shared with the platform build

IMAGES = ROOT / 'platform/build/images'
LOADER = IMAGES / 'w1700k-loader.itb'
LOADER_B = IMAGES / 'w1700k-loader-b.itb'      # the same build, for `loader-b` (updates over ssh)
UPDATE = IMAGES / 'w1700k-update.tar'           # the second loader and the root, for init/flash --bundle
DUMPER = IMAGES / 'w1700k-dumper.itb'
SYSTEM_FIT = IMAGES / 'w1700k-system.itb'
RECOVERY_FIT = IMAGES / 'w1700k-recovery.itb'
ROOTFS = IMAGES / 'rootfs.squashfs'
# One empty durable UBIFS per NAND part (platform/partitions.py NANDS).
DURABLES = tuple(IMAGES / nand.durable_image for nand in partitions.NANDS)
# unsquashfs and mksquashfs, built by Buildroot, make the per-unit root.
SQUASHFS_TOOLS = ROOT / 'platform/build/buildroot/host/bin'

CONFIG_FILE = Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / \
    'w1700k/install.json'
DATA_DIR = Path(os.environ.get('XDG_DATA_HOME', '~/.local/share')).expanduser() / 'w1700k'

DEFAULTS = {
    # The USB serial adapter on the unit's console header.
    'uart': '/dev/ttyUSB0',
    # The host's address on the link to the unit's management port, and the
    # address the unit uses there (set in RAM only while U-Boot runs).
    'host_ip': '192.168.1.10',
    'device_ip': '192.168.1.1',
    # Stock U-Boot can only fetch from UDP 69. Unprivileged runs listen on this
    # port behind the redirect that init/host/tftp-forward installs; runs as root
    # listen on 69 itself.
    'stock_port': 7071,
    # How stock U-Boot gets our first image: 'tftp' (it fetches from UDP 69), or
    # 'serial' (its `loadb` over the console: slower, but no port 69, no root and
    # no redirect).
    'stock_transport': 'tftp',
    # Our U-Boot fetches from this unprivileged port (its `tftpdstp`).
    'transfer_port': 7070,
    # The public key allowed to log in as root over SSH.
    'authorized_key': None,
    'data_dir': str(DATA_DIR),
}


def load(path=CONFIG_FILE):
    settings = dict(DEFAULTS)
    if Path(path).is_file():
        loaded = json.loads(Path(path).read_text())
        unknown = sorted(set(loaded) - set(DEFAULTS))
        if unknown:
            raise SystemExit(f'{path}: unknown setting(s): {", ".join(unknown)}')
        settings.update(loaded)
        if settings['stock_transport'] not in ('tftp', 'serial'):
            raise SystemExit(f'{path}: stock_transport is "tftp" or "serial", not '
                             f'{settings["stock_transport"]!r}')
    if os.geteuid() == 0:
        settings['stock_port'] = 69
    return settings


def add_arguments(parser, settings):
    """The connection flags every tool shares, with the loaded settings as defaults."""
    parser.add_argument('--uart', default=settings['uart'],
                        help='serial console device (default: %(default)s)')
    parser.add_argument('--host-ip', default=settings['host_ip'],
                        help="this host's address on the unit's link (default: %(default)s)")
    parser.add_argument('--device-ip', default=settings['device_ip'],
                        help='address the unit uses in U-Boot (default: %(default)s)')
    parser.add_argument('--stock-port', type=int, default=settings['stock_port'],
                        help='UDP port serving stock U-Boot (default: %(default)s)')
    parser.add_argument('--stock-transport', choices=('tftp', 'serial'),
                        default=settings['stock_transport'],
                        help='how stock U-Boot gets our first image: tftp, which it '
                             'fetches from UDP 69, or serial, its loadb over the console '
                             '(slower; needs neither port 69 nor root) '
                             '(default: %(default)s)')
    parser.add_argument('--transfer-port', type=int, default=settings['transfer_port'],
                        help='UDP port serving our U-Boot (default: %(default)s)')
    parser.add_argument('--data-dir', type=Path, default=Path(settings['data_dir']),
                        help='where unit directories and run logs are kept '
                             '(default: %(default)s)')


def unit_dir(data_dir, serial):
    return Path(data_dir).expanduser() / 'units' / serial


def known_units(data_dir):
    units = Path(data_dir).expanduser() / 'units'
    return sorted(path.name for path in units.iterdir() if path.is_dir()) \
        if units.is_dir() else []


def pick_unit(data_dir, serial=None):
    """The unit to work on: the one named, or the only one known."""
    if serial:
        path = unit_dir(data_dir, serial)
        if not path.is_dir():
            raise SystemExit(f'no unit directory {path}; install the unit with init/install')
        return serial
    units = known_units(data_dir)
    if len(units) != 1:
        raise SystemExit('name the unit with --unit SERIAL; known units: '
                         + (', '.join(units) or 'none'))
    return units[0]


def durable(nand):
    return IMAGES / nand.durable_image


def require_images(*paths):
    missing = [str(path) for path in paths if not Path(path).is_file()]
    if missing:
        raise SystemExit('missing build output: ' + ', '.join(missing) + '; run `make`')
