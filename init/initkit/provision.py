# SPDX-License-Identifier: MIT
"""The unit's own root filesystem: the built root plus its SSH keys and network.

The built rootfs.squashfs is the same for every unit and lets nobody log in.
This adds, for one unit, the public key allowed to log in as root, the unit's
SSH host key (made once and kept in the unit directory, so the host key does
not change between updates) and, if present, the unit's own network.conf. The
result is written to UBI volume `rootfs` in place of the generic image.

    unit directory (see config.py)
      dropbear_ed25519_host_key   generated on first use, or imported
      network.conf                optional; replaces /etc/w1700k/network.conf
      rootfs.squashfs             the unit's root, rebuilt by every install/update
"""
import base64
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from . import config
from .log import private_file

HOST_KEY = 'dropbear_ed25519_host_key'
KNOWN_HOSTS = 'known_hosts'
NETWORK = 'network.conf'
IMAGE = 'rootfs.squashfs'
# The options Buildroot uses for the generic image (128 KiB blocks, zstd).
MKSQUASHFS_OPTIONS = ['-noappend', '-processors', '1', '-b', '128K', '-comp', 'zstd',
                      '-Xcompression-level', '22', '-all-root', '-repro-time', '0',
                      '-no-progress', '-exit-on-error']
KEY_TYPES = (b'ssh-ed25519', b'ecdsa-sha2-nistp256', b'ecdsa-sha2-nistp384',
             b'ecdsa-sha2-nistp521', b'ssh-rsa')


class ProvisionError(ValueError):
    """An input key or file is unusable."""


def _string(data, offset):
    length, = struct.unpack_from('>I', data, offset)
    return data[offset + 4:offset + 4 + length], offset + 4 + length


def _wire(*fields):
    return b''.join(struct.pack('>I', len(field)) + field for field in fields)


def dropbear_from_openssh(text):
    """Convert an unencrypted OpenSSH Ed25519 private key to Dropbear's format.

    Dropbear stores string("ssh-ed25519") + string(seed || public key).
    """
    lines = [line.strip() for line in text.strip().splitlines()]
    if lines[:1] != ['-----BEGIN OPENSSH PRIVATE KEY-----'] or \
            lines[-1:] != ['-----END OPENSSH PRIVATE KEY-----']:
        raise ProvisionError('not an OpenSSH private key')
    blob = base64.b64decode(''.join(lines[1:-1]))
    magic = b'openssh-key-v1\0'
    if not blob.startswith(magic):
        raise ProvisionError('not an openssh-key-v1 private key')
    offset = len(magic)
    cipher, offset = _string(blob, offset)
    _kdf, offset = _string(blob, offset)
    _options, offset = _string(blob, offset)
    if cipher != b'none':
        raise ProvisionError('the host key must not have a passphrase')
    count, = struct.unpack_from('>I', blob, offset)
    offset += 4
    for _ in range(count):
        _public, offset = _string(blob, offset)
    private, offset = _string(blob, offset)
    cursor = 8                                   # two copies of a check integer
    kind, cursor = _string(private, cursor)
    if kind != b'ssh-ed25519':
        raise ProvisionError('the host key must be Ed25519')
    public, cursor = _string(private, cursor)
    secret, cursor = _string(private, cursor)
    if len(public) != 32 or len(secret) != 64 or secret[32:] != public:
        raise ProvisionError('malformed Ed25519 key')
    return _wire(b'ssh-ed25519', secret)


def host_public_key(dropbear_key):
    """The OpenSSH public key line of a Dropbear Ed25519 private key."""
    kind, offset = _string(dropbear_key, 0)
    secret, _ = _string(dropbear_key, offset)
    if kind != b'ssh-ed25519' or len(secret) != 64:
        raise ProvisionError('not a Dropbear Ed25519 key')
    return b'ssh-ed25519 ' + base64.b64encode(_wire(b'ssh-ed25519', secret[32:]))


def fingerprint(public_line):
    blob = base64.b64decode(public_line.split()[1])
    return 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')


def generate_host_key():
    """A new Ed25519 host key, made with ssh-keygen and converted for Dropbear."""
    if not shutil.which('ssh-keygen'):
        raise ProvisionError('ssh-keygen (OpenSSH) is needed to make the unit host key')
    with tempfile.TemporaryDirectory(prefix='w1700k-key-') as temporary:
        path = Path(temporary) / 'key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'w1700k',
                        '-f', str(path)], check=True)
        return dropbear_from_openssh(path.read_text())


def host_key(unit_directory, import_from=None):
    """The unit's Dropbear host key: imported, kept from before, or made now."""
    path = Path(unit_directory) / HOST_KEY
    if import_from:
        data = Path(import_from).read_bytes()
        if data.startswith(b'-----BEGIN OPENSSH PRIVATE KEY-----'):
            data = dropbear_from_openssh(data.decode())
        host_public_key(data)                       # validates
        private_file(path, data)
    elif not path.is_file():
        private_file(path, generate_host_key())
    return path.read_bytes()


def authorized_key(path=None):
    """The public key for root logins: the one given, or the user's default."""
    candidates = [Path(path).expanduser()] if path else \
        [Path('~/.ssh').expanduser() / name
         for name in ('id_ed25519.pub', 'id_ecdsa.pub', 'id_rsa.pub')]
    for candidate in candidates:
        if candidate.is_file():
            line = candidate.read_bytes().strip()
            if not line.split(b' ', 1)[0] in KEY_TYPES:
                raise ProvisionError(f'{candidate} is not an SSH public key')
            return line + b'\n'
    raise ProvisionError('no SSH public key for root logins; pass --authorized-key '
                         'or create ~/.ssh/id_ed25519.pub')


def private_files(unit_directory, public_key, dropbear_key):
    """{path in the root: (bytes, mode)} for one unit."""
    files = {
        'root/.ssh/authorized_keys': (public_key, 0o600),
        f'etc/dropbear/{HOST_KEY}': (dropbear_key, 0o600),
        f'etc/dropbear/{HOST_KEY}.pub': (host_public_key(dropbear_key) + b'\n', 0o644),
    }
    network = Path(unit_directory) / NETWORK
    if network.is_file():
        files['etc/w1700k/network.conf'] = (network.read_bytes(), 0o644)
    return files


def _write(tree, name, data, mode):
    """Write one file into the unpacked root, keeping its directories' modes."""
    path = tree / name
    missing = [parent for parent in reversed(path.relative_to(tree).parents)
               if not (tree / parent).exists()]
    for parent in missing:
        (tree / parent).mkdir(mode=0o700 if parent.name in ('.ssh', 'dropbear') else 0o755)
    parent = path.parent
    saved = parent.stat().st_mode & 0o7777
    parent.chmod(saved | 0o700)
    try:
        if path.is_symlink() or path.exists():
            path.unlink()
        path.write_bytes(data)
        path.chmod(mode)
    finally:
        parent.chmod(saved)


def build(unit_directory, public_key, dropbear_key, generic=config.ROOTFS,
          tools=config.SQUASHFS_TOOLS):
    """Write the unit's rootfs.squashfs into its directory and return the path."""
    unsquashfs, mksquashfs = tools / 'unsquashfs', tools / 'mksquashfs'
    for path in (generic, unsquashfs, mksquashfs):
        if not Path(path).exists():
            raise ProvisionError(f'{path} is missing; run `make`')
    unit_directory = Path(unit_directory)
    output = unit_directory / IMAGE
    with tempfile.TemporaryDirectory(prefix='w1700k-root-', dir=unit_directory) as temporary:
        tree = Path(temporary) / 'root'
        subprocess.run([str(unsquashfs), '-no-xattrs', '-quiet', '-no-progress', '-d',
                        str(tree), str(generic)], check=True, stdout=subprocess.DEVNULL)
        # The generic root links /etc/dropbear to a RAM directory for per-boot keys.
        dropbear = tree / 'etc/dropbear'
        if dropbear.is_symlink():
            etc_mode = dropbear.parent.stat().st_mode & 0o7777
            dropbear.parent.chmod(etc_mode | 0o700)
            dropbear.unlink()
            dropbear.mkdir(mode=0o700)
            dropbear.parent.chmod(etc_mode)
        for name, (data, mode) in private_files(unit_directory, public_key,
                                                 dropbear_key).items():
            _write(tree, name, data, mode)
        env = dict(os.environ, TZ='UTC', LC_ALL='C')
        env.pop('SOURCE_DATE_EPOCH', None)
        partial = Path(temporary) / IMAGE
        subprocess.run([str(mksquashfs), str(tree), str(partial), *MKSQUASHFS_OPTIONS],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        if partial.read_bytes()[:4] != b'hsqs':
            raise ProvisionError('mksquashfs did not produce a SquashFS image')
        if partial.stat().st_size > config.partitions.ROOTFS_VOLUME_BYTES:
            raise ProvisionError('the unit root does not fit its volume')
        partial.chmod(0o600)
        partial.replace(output)
    return output


def provision(unit_directory, authorized_key_path=None, import_host_key=None, address='192.168.1.1',
              generic=None):
    """Build the unit's root (from `generic`, default this build's); returns (image path, host
    public key line). Also writes `known_hosts` in the unit's directory, for the host tools'
    `unit.known_hosts`."""
    unit_directory = Path(unit_directory)
    unit_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    dropbear_key = host_key(unit_directory, import_host_key)
    image = build(unit_directory, authorized_key(authorized_key_path), dropbear_key,
                  **({'generic': generic} if generic else {}))
    public = host_public_key(dropbear_key).decode()
    (unit_directory / KNOWN_HOSTS).write_text(f'{address} {public}\n')
    return image, public
