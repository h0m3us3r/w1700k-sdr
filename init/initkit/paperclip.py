# SPDX-License-Identifier: MIT
"""The paperclip key, from the host: send a public key to a unit that is waiting for one.

A unit has one slot for a login key that only a hand on the unit can change (the reset button;
kernel/w1700k_sdr/keyd.h). Held ten seconds and let go, the button opens the slot for three
minutes: the unit takes one line, a public key, on TCP port 7722 of its management address,
checks it, keeps it, and answers. This is the other end of that.

The unit's answer also carries its ssh host key. It came over a cable to a unit someone is
holding a paperclip to, which is as good an introduction as a host key gets: the login is tried
against exactly that key, and the line to keep is printed.
"""
import base64
import binascii
import hashlib
from pathlib import Path
import socket
import subprocess
import tempfile
import time

PORT = 7722
KEY_TYPES = ('ssh-ed25519', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521', 'ssh-rsa')
WAIT_SECONDS = 180


class KeyProblem(ValueError):
    """Not a public key, or not taken; the message says which."""


def public_key(line):
    """(type, base64 body) of one line of a public key file, checked as the unit checks it: a
    type the unit's ssh server takes, a body that is base64 and begins with that same type."""
    if isinstance(line, bytes):
        line = line.decode('ascii', 'replace')
    words = line.split()
    if 'PRIVATE KEY' in line:
        raise KeyProblem('that is a private key: give the .pub file')
    if len(words) < 2 or words[0] not in KEY_TYPES:
        raise KeyProblem('not an SSH public key (ssh-ed25519, ecdsa-sha2-nistp256/384/521 or ssh-rsa)')
    kind, body = words[0], words[1]
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise KeyProblem('not an SSH public key: its body is not base64 (cut short?)')
    name = kind.encode()
    if raw[:4] != len(name).to_bytes(4, 'big') or raw[4:4 + len(name)] != name or len(raw) < len(name) + 8:
        raise KeyProblem('not an SSH public key: its body is not a key of the type it names')
    if kind == 'ssh-ed25519' and len(raw) != 4 + 11 + 4 + 32:
        raise KeyProblem('not an SSH public key: an Ed25519 key of the wrong length (cut short?)')
    return kind, body


def fingerprint(body):
    """The SHA256 fingerprint ssh shows for a key with this base64 body."""
    return 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(body)).digest()).decode().rstrip('=')


def default_key():
    for name in ('id_ed25519.pub', 'id_ecdsa.pub', 'id_rsa.pub'):
        path = Path('~/.ssh').expanduser() / name
        if path.is_file():
            return path
    raise KeyProblem('no public key: name one, or create ~/.ssh/id_ed25519.pub (ssh-keygen -t ed25519)')


def send(address, line, port=PORT, wait=WAIT_SECONDS, connect=socket.create_connection,
         clock=time.monotonic, sleep=time.sleep, say=print):
    """Send the key once the unit is waiting; what it answered, as {name: value}."""
    deadline, told = clock() + wait, False
    while True:
        try:
            link = connect((address, port), timeout=5)
            break
        except OSError as error:
            if clock() >= deadline:
                raise KeyProblem(f'the unit at {address} did not open its slot in {wait:.0f} s ({error}). '
                                'Hold the reset button ten seconds, until the LED pulses blue, and let go')
            if not told:
                say('key: waiting for the unit. Hold its reset button with a paperclip: the LED fills for five seconds, '
                    'pulses, and after ten seconds pulses blue. Let go then.')
                told = True
            sleep(0.5)
    with link:
        link.sendall(line.encode() + b'\n')
        answer = b''
        while not answer.endswith(b'\n') and len(answer) < 4096:
            piece = link.recv(4096)
            if not piece:
                break
            answer += piece
    words = answer.decode('ascii', 'replace').split()
    if not words or words[0] != 'ok':
        raise KeyProblem('the unit did not take the key: ' + (' '.join(words[1:] if words[:1] == ['error:'] else words)
                                                              or 'it gave no answer'))
    return dict(word.split('=', 1) for word in words[1:] if '=' in word)


def login(address, host_line, key_path, run=subprocess.run):
    """Log in as root with the key just sent, trusting only the host key the unit gave. Returns
    None when it works, else what ssh said."""
    with tempfile.TemporaryDirectory(prefix='w1700k-key-') as temporary:
        known = Path(temporary) / 'known_hosts'
        known.write_text(host_line + '\n')
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', f'UserKnownHostsFile={known}',
                   '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'StrictHostKeyChecking=yes']
        private = Path(str(key_path)[:-4]) if str(key_path).endswith('.pub') else None
        if private is not None and private.is_file():
            command += ['-o', 'IdentitiesOnly=yes', '-i', str(private)]
        result = run(command + [f'root@{address}', 'true'], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return None if result.returncode == 0 else (result.stderr.strip() or f'ssh ended with status {result.returncode}')


def run(address, key_path=None, port=PORT, wait=WAIT_SECONDS, known_hosts=None, say=print, **how):
    """Put the public key at `key_path` into the unit's slot. 0 when the unit took it and a login
    with it works."""
    key_path = Path(key_path).expanduser() if key_path else default_key()
    try:
        text = key_path.read_text()
    except OSError as error:
        raise KeyProblem(f'cannot read {key_path}: {error.strerror}')
    kind, body = public_key(text.strip().splitlines()[0] if text.strip() else '')
    say(f'key: {key_path} ({kind}, {fingerprint(body)})')
    answer = send(address, f'{kind} {body}', port=port, wait=wait, say=say,
                  **{name: how[name] for name in ('connect', 'clock', 'sleep') if name in how})
    say('key: the unit has it, switched on')
    if answer.get('persist') != '1':
        say("key: this unit's loader does not hand the slot to the system, so the key works until the unit "
            'restarts. Update the unit (init/flash --ssh) and it stays')
    host = answer.get('hostkey', '')
    if ':' not in host:
        say('key: the unit named no host key; log in with ssh and check its fingerprint yourself')
        return 0
    host_kind, host_body = host.split(':', 1)
    public_key(f'{host_kind} {host_body}')
    host_line = f'{address} {host_kind} {host_body}'
    say(f"key: the unit's host key is {host_kind} {fingerprint(host_body)}")
    if known_hosts:
        path = Path(known_hosts).expanduser()
        lines = path.read_text().splitlines() if path.is_file() else []
        if host_line not in lines:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a') as out:
                out.write(host_line + '\n')
        say(f'key: written to {path}')
    else:
        say(f'key: to have ssh know the unit: echo "{host_line}" >> ~/.ssh/known_hosts')
    problem = login(address, host_line, key_path, **{name: how[name] for name in ('run',) if name in how})
    if problem:
        say(f'key: the unit took the key, but a login with it did not work: {problem}')
        return 1
    say(f'key: login works: ssh root@{address}')
    return 0
