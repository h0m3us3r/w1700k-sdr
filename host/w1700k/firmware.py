# SPDX-License-Identifier: MIT
"""Our firmware set built by `make -C firmware`, described by its firmware.json: the ROM patch, the
WM, the DSP image and the WA, in download order, and the ROM they are written for."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

SCHEMA = 4
ROLES = ('patch', 'wm', 'dsp', 'wa')


class FirmwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class Image:
    role: str
    path: Path
    size: int
    sha256: str
    flags: tuple

    def data(self):
        data = self.path.read_bytes()
        if len(data) != self.size or hashlib.sha256(data).hexdigest() != self.sha256:
            raise FirmwareError(f'{self.path} does not match firmware.json; rebuild with make -C firmware')
        return data


@dataclass(frozen=True)
class Rom:
    """The radio's boot ROM as the set knows it: where to read it, and what it then hashes to."""
    base: int
    size: int
    sha256: tuple                 # of the `size` bytes at `base`, for each ROM the set is written for


@dataclass(frozen=True)
class Firmware:
    containers: tuple             # Images in download order
    rom: Rom


def _image(entry, directory):
    try:
        role, name, size, digest = entry['role'], entry['file'], entry['size'], entry['sha256']
        flags = tuple(entry.get('flags', ()))
    except (KeyError, TypeError) as exc:
        raise FirmwareError(f'firmware.json: malformed entry ({exc})')
    if '/' in name or not isinstance(size, int) or len(digest) != 64:
        raise FirmwareError(f'firmware.json: malformed entry for {role}')
    return Image(role, directory / name, size, digest, flags)


def parse(manifest, directory):
    """A Firmware from a decoded firmware.json whose files live in `directory`."""
    if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA:
        raise FirmwareError(f'firmware.json: expected schema {SCHEMA}; rebuild with make -C firmware')
    directory = Path(directory)
    try:
        containers = tuple(_image(e, directory) for e in manifest['containers'])
    except (KeyError, TypeError) as exc:
        raise FirmwareError(f'firmware.json: {exc}')
    if tuple(c.role for c in containers) != ROLES:
        raise FirmwareError(f'firmware.json: containers must be {", ".join(ROLES)} in that order')
    return Firmware(containers, _rom(manifest.get('rom')))


def _rom(entry):
    try:
        base, size, digests = entry['base'], entry['size'], tuple(entry['sha256'])
    except (KeyError, TypeError) as exc:
        raise FirmwareError(f'firmware.json: malformed rom ({exc})')
    if not (isinstance(base, int) and isinstance(size, int) and size > 0 and base % 4 == 0 and size % 4096 == 0 and digests
            and all(isinstance(d, str) and len(d) == 64 for d in digests)):
        raise FirmwareError('firmware.json: malformed rom')
    return Rom(base, size, digests)


def load(directory):
    path = Path(directory) / 'firmware.json'
    if not path.is_file():
        raise FirmwareError(f'{path} not found; build the firmware with make -C firmware')
    try:
        manifest = json.loads(path.read_text())
    except ValueError as exc:
        raise FirmwareError(f'{path}: {exc}')
    return parse(manifest, path.parent)
