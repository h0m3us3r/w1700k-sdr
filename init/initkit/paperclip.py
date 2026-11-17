# SPDX-License-Identifier: MIT
"""The paperclip key, from the host: send a public key to a unit that is waiting for one.

A unit has one slot for a login key that only a hand on the unit can change (the reset button;
kernel/w1700k_sdr/keyd.h). Held ten seconds and let go, the button opens the slot for three
minutes: the unit takes one line, a public key, on TCP port 7722 of its management address,
checks it and holds it, and says which key it holds. It keeps that key only when the button is
then held three seconds; a tap, or a minute without a press, discards it. This is the other end
of that: it sends the key, says whether the key the unit holds is the one it sent, and says what
the hand on the button should do.

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
OUTCOME_MARGIN = 15     # seconds past the unit's own limit to wait for what the button decided


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


def _line(reader):
    """The words of one line of the unit's answer; none when it hung up without one."""
    return reader.readline(4096).decode('ascii', 'replace').split()


def _number(words, name, default):
    for word in words:
        if word.startswith(name + '='):
            try:
                return float(word.split('=', 1)[1])
            except ValueError:
                break
    return default


def _held(words):
    """The fingerprint of the key the unit says it holds ("key=TYPE:BASE64"), and that key."""
    for word in words:
        if word.startswith('key=') and ':' in word:
            kind, body = word[4:].split(':', 1)
            try:
                public_key(f'{kind} {body}')
            except KeyProblem:
                break
            return fingerprint(body), f'{kind} {body}'
    return 'a key it did not name', None


def send(address, line, port=PORT, wait=WAIT_SECONDS, connect=socket.create_connection,
         clock=time.monotonic, sleep=time.sleep, say=print):
    """Send the key once the unit is waiting, and see it through the button; what the unit
    answered when it kept the key, as {name: value}."""
    deadline, told = clock() + wait, False
    while True:
        try:
            link = connect((address, port), timeout=5)
            break
        except OSError as error:
            if clock() >= deadline:
                raise KeyProblem(f'the unit at {address} did not open its slot in {wait:.0f} s ({error}). '
                                'Hold the reset button ten seconds, until the LED pulses blue, and let go. If you '
                                'did, and the LED went on to beat cyan in pairs without this having sent a key, '
                                'the unit is holding a key from somewhere else: tap the button once to discard it')
            if not told:
                say('key: waiting for the unit. Hold its reset button with a paperclip until the LED flashes and '
                    'pulses blue, ten seconds, and let go then.')
                told = True
            sleep(0.5)
    with link, link.makefile('rb') as reader:
        link.sendall(line.encode() + b'\n')
        try:
            words = _line(reader)
        except OSError as error:
            raise KeyProblem(f'the unit did not answer ({error}). If its LED beats cyan in pairs it is holding a '
                             'key, and nothing here says it is yours: tap the button once to discard it, and start '
                             'again')
        if words[:1] == ['busy']:
            theirs, _ = _held(words)
            raise KeyProblem(f'the unit is holding another key, not yours: {theirs}. Somebody else sent one first. '
                             'DO NOT HOLD THE BUTTON. Tap it once: that discards the key (the LED goes out). Then '
                             'find out who else reaches this unit before you start again')
        if words[:1] == ['pending']:
            theirs, held = _held(words)
            if held != line:
                raise KeyProblem(f'the unit says it holds {theirs}, which is not the key sent. DO NOT HOLD THE '
                                 'BUTTON. Tap it once: that discards the key')
            hold, within = _number(words, 'hold', 3), _number(words, 'within', 60)
            say(f'key: the unit holds your key ({theirs}) and has not kept it yet. Its LED beats cyan in pairs.')
            say(f'key: HOLD the reset button {hold:.0f} s, until the LED flashes and pulses green, and let go: that '
                f'keeps it. A tap discards it, and so do {within:.0f} s without a press.')
            link.settimeout(within + OUTCOME_MARGIN)
            try:
                words = _line(reader)
            except OSError as error:
                raise KeyProblem(f'the unit did not say what became of the key ({error}); try ssh root@{address}, '
                                 'and start again if it does not log in')
        elif words[:1] == ['ok']:
            say("key: this unit's software keeps the first key it is sent, without asking the button again. "
                'It kept yours. Update the unit (init/flash --ssh) and it asks')
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
