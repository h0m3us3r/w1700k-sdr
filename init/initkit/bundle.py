# SPDX-License-Identifier: MIT
"""The update archive (w1700k-update.tar, written by platform/images.py): the second loader and
the generic root with a manifest of their digests and of the flash layout they are built for.
Read whole and checked before anything is used: what does not hold together is refused."""
import hashlib
import io
import json
import tarfile

from .console import StepFailed

FORMAT = 1
LOADER, ROOT = 'w1700k-loader-b.itb', 'rootfs.squashfs'
LIMIT = 64 << 20


def read(path):
    """(manifest, {name: bytes}) of the archive at `path`."""
    try:
        with tarfile.open(path, mode='r:') as archive:
            members = {m.name: m for m in archive.getmembers()}
            if set(members) != {'manifest.json', LOADER, ROOT} or not all(m.isfile() for m in members.values()):
                raise StepFailed(f'{path} is not a W1700K update archive: it holds {sorted(members)}')
            if any(m.size > LIMIT for m in members.values()):
                raise StepFailed(f'{path}: a member is larger than any image of a unit')
            data = {name: archive.extractfile(member).read() for name, member in members.items()}
    except (OSError, tarfile.TarError) as error:
        raise StepFailed(f'{path} cannot be read as an update archive: {error}') from None
    try:
        manifest = json.loads(data.pop('manifest.json'))
        listed = manifest['files']
        partitions = {name: tuple(span) for name, span in manifest['partitions'].items()}
        if manifest['format'] != FORMAT:
            raise StepFailed(f'{path} is an update archive of format {manifest["format"]}; these tools '
                             f'read format {FORMAT}')
        for name, content in data.items():
            if hashlib.sha256(content).hexdigest() != listed[name]['sha256'] or len(content) != listed[name]['bytes']:
                raise StepFailed(f'{path}: {name} is not the file its manifest lists')
    except (KeyError, TypeError, ValueError) as error:
        raise StepFailed(f'{path}: the manifest does not read as one ({error!r})') from None
    manifest['partitions'] = partitions
    return manifest, data


def write(path, loader, root, partitions):
    """An archive as platform/images.py writes it (for tests)."""
    files = {LOADER: loader, ROOT: root}
    manifest = {'format': FORMAT, 'partitions': {name: list(span) for name, span in partitions.items()},
                'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                          for name, data in files.items()}}
    files = {'manifest.json': (json.dumps(manifest) + '\n').encode(), **files}
    with tarfile.open(path, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
