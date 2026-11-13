# SPDX-License-Identifier: MIT
"""Host configuration: ~/.config/w1700k/config.json (or $W1700K_CONFIG). Every key is optional.

    {"unit": {"host": "192.168.1.1", "user": "root", "ssh_key": null,
              "bin_dir": "/usr/sbin", "module": "/lib/modules/w1700k_sdr.ko"},
     "eeprom": "~/.config/w1700k/eeprom.bin",
     "firmware_dir": "<repo>/firmware/build",
     "export": {"interface": "eth1", "src_ip": "10.77.0.1", "dst_ip": "10.77.0.10",
                "dst_mac": null, "port": 7700, "ports": 8, "link_mbps": null, "packing": null,
                "piece": null}}

unit.bin_dir and unit.module point at the installed clients and driver (production rootfs);
after `w1700k push` set them to /tmp/w1700k and /tmp/w1700k/w1700k_sdr.ko. export.dst_mac null
means the MAC of the local interface that holds dst_ip. export.link_mbps caps what a stream may
put on the link, in Mbit/s, below the unit port's negotiated speed (a USB adapter that carries less
than its Ethernet side links at); null: the negotiated speed. export.packing ("raw", "half",
"full", or "16", "14", "8", "7" bits per I/Q pair) sets every stream's packing by hand; null:
chosen from the sample rate and the link. export.piece sends each slice as datagrams of at most
that many of its bytes (1456 fits a 1500-byte MTU) for a path without jumbo frames; null: one
datagram per slice.
"""
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PATH = Path('~/.config/w1700k/config.json')
ENV_PATH = 'W1700K_CONFIG'


# raw, half, full: the radio's lossless schedules (firmware/stream/blob.py; stream.PACKED_SHARE has
# each one's share). 16, 14, 8, 7: bits per I/Q pair, requantised on the unit (stream.REDUCED).
PACKINGS = ('raw', 'half', 'full', '16', '14', '8', '7')


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class UnitConfig:
    host: str = '192.168.1.1'
    user: str = 'root'
    ssh_key: str | None = None
    known_hosts: str | None = None    # the unit's host key (init/provision writes one); default: ssh's own
    bin_dir: str = '/usr/sbin'
    module: str = '/lib/modules/w1700k_sdr.ko'


@dataclass(frozen=True)
class ExportConfig:
    interface: str = 'eth1'
    src_ip: str = '10.77.0.1'
    dst_ip: str = '10.77.0.10'
    dst_mac: str | None = None
    port: int = 7700
    ports: int = 8
    link_mbps: int | None = None      # None: the unit port's negotiated speed
    packing: str | None = None        # None: chosen from the sample rate and the link
    piece: int | None = None          # None: a slice per datagram; else at most this many of its bytes


@dataclass(frozen=True)
class Config:
    unit: UnitConfig = field(default_factory=UnitConfig)
    eeprom: Path | None = None        # None: see eeprom_path()
    firmware_dir: Path = REPO / 'firmware' / 'build'
    export: ExportConfig = field(default_factory=ExportConfig)


def _section(cls, values, name):
    if not isinstance(values, dict):
        raise ConfigError(f'{name}: expected an object')
    known = {f.name: f.type for f in fields(cls)}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ConfigError(f'{name}: unknown keys {unknown}')
    out = cls()
    for key, value in values.items():
        default = getattr(out, key)
        if value is not None and default is not None and not isinstance(value, type(default)):
            raise ConfigError(f'{name}.{key}: expected {type(default).__name__}')
        out = replace(out, **{key: value})
    return out


def parse(data):
    """A Config from the decoded JSON object, validated."""
    if not isinstance(data, dict):
        raise ConfigError('the configuration must be a JSON object')
    unknown = sorted(set(data) - {'unit', 'eeprom', 'firmware_dir', 'export'})
    if unknown:
        raise ConfigError(f'unknown keys {unknown}')
    config = Config()
    if 'unit' in data:
        config = replace(config, unit=_section(UnitConfig, data['unit'], 'unit'))
    if 'export' in data:
        config = replace(config, export=_section(ExportConfig, data['export'], 'export'))
    for key in ('eeprom', 'firmware_dir'):
        if key in data:
            if not isinstance(data[key], str):
                raise ConfigError(f'{key}: expected a path string')
            config = replace(config, **{key: Path(data[key]).expanduser()})
    _validate(config)
    return config


def _validate(config):
    export = config.export
    for key in ('src_ip', 'dst_ip'):
        try:
            ipaddress.IPv4Address(getattr(export, key))
        except ValueError:
            raise ConfigError(f'export.{key}: not an IPv4 address')
    if export.dst_mac is not None and not re.fullmatch(r'([0-9a-f]{2}:){5}[0-9a-f]{2}', export.dst_mac.lower()):
        raise ConfigError('export.dst_mac: expected xx:xx:xx:xx:xx:xx')
    if not 1 <= export.port <= 65535 - export.ports or not 1 <= export.ports <= 256:
        raise ConfigError('export.port/ports out of range')
    if export.link_mbps is not None and (isinstance(export.link_mbps, bool) or not isinstance(export.link_mbps, int)
                                         or export.link_mbps < 1):
        raise ConfigError('export.link_mbps: expected Mbit/s, a positive integer')
    if export.packing is not None and export.packing not in PACKINGS:
        raise ConfigError(f'export.packing: one of {", ".join(PACKINGS)}, or null for automatic')
    if export.piece is not None and (isinstance(export.piece, bool) or not isinstance(export.piece, int)
                                     or not 256 <= export.piece <= 8192):
        raise ConfigError('export.piece: bytes of a slice per datagram, 256..8192 (1456 fits a 1500-byte MTU), '
                          'or null for whole slices')
    if not config.unit.bin_dir.startswith('/') or not config.unit.module.startswith('/'):
        raise ConfigError('unit.bin_dir and unit.module must be absolute unit paths')


def load(path=None):
    """The configuration from `path`, $W1700K_CONFIG or the default location; defaults if absent."""
    path = Path(path or os.environ.get(ENV_PATH) or DEFAULT_PATH).expanduser()
    if not path.is_file():
        return Config()
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise ConfigError(f'{path}: {exc}')
    try:
        return parse(data)
    except ConfigError as exc:
        raise ConfigError(f'{path}: {exc}')


def local_mac_for(ip):
    """The MAC of the local interface holding `ip` (iproute2 JSON), for the export's destination."""
    try:
        out = subprocess.run(['ip', '-j', '-4', 'addr', 'show'], capture_output=True, text=True, check=True,
                             stdin=subprocess.DEVNULL).stdout
        interfaces = json.loads(out)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ConfigError(f'cannot list local addresses ({exc}); set export.dst_mac')
    for interface in interfaces:
        if any(a.get('local') == ip for a in interface.get('addr_info', [])):
            try:
                return Path('/sys/class/net', interface['ifname'], 'address').read_text().strip().lower()
            except (KeyError, OSError) as exc:
                raise ConfigError(f'cannot read the MAC of the interface holding {ip}: {exc}')
    raise ConfigError(f'no local interface holds {ip}; configure it or set export.dst_mac')


DATA_DIR = Path(os.environ.get('XDG_DATA_HOME', '~/.local/share')).expanduser() / 'w1700k'
USER_EEPROM = Path('~/.config/w1700k/eeprom.bin').expanduser()


def eeprom_path(config):
    """The calibration EEPROM to load: `eeprom` if set; else ~/.config/w1700k/eeprom.bin; else
    the one init/install saved, if exactly one unit has one."""
    if config.eeprom:
        return config.eeprom
    if USER_EEPROM.is_file():
        return USER_EEPROM
    found = sorted(DATA_DIR.glob('units/*/eeprom.bin'))
    if len(found) == 1:
        return found[0]
    where = 'several units have one; set "eeprom"' if found else 'run init/install or init/eeprom'
    raise ConfigError(f'no calibration EEPROM: {where} (see host/w1700k/README.md)')
