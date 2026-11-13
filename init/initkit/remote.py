# SPDX-License-Identifier: MIT
"""Update an installed unit's root over the network alone (init/flash --rootfs --ssh).

Linux on the unit never writes its own system flash, so the new root is left in
durable storage with the running one beside it (platform/rootfs/overlay/usr/sbin/
system-update, sent along and run over ssh), and the unit is rebooted: its loader
writes the new root, reads it back and boots it. Once it is up and its storage
checks out it is confirmed. A new root that never answers is undone by a power
cycle: the loader finds it unconfirmed and puts the previous one back.

The loader must be one that applies staged updates; an older one is updated once
over the serial console (init/flash --loader).
"""
import hashlib
from pathlib import Path
import shlex
import subprocess
import time

from . import config, provision
from .console import StepFailed

SCRIPT = config.ROOT / 'platform/rootfs/overlay/usr/sbin/system-update'
STAGING = '/work/system-update.squashfs'      # in RAM on the unit
DOWN_SECONDS = 90           # for a rebooting unit to stop answering
UP_SECONDS = 240            # for it to answer again: the loader's write, then the boot
POWER_CYCLE_SECONDS = 900   # for the operator to power-cycle a unit that did not come back


class Link:
    """Commands on the unit over ssh, as root, against the host key init/provision recorded."""

    def __init__(self, settings, directory, authorized_key=None):
        self.address = settings.device_ip
        self.argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                     '-o', f'UserKnownHostsFile={Path(directory) / provision.KNOWN_HOSTS}',
                     '-o', 'StrictHostKeyChecking=yes']
        private = Path(authorized_key).expanduser().with_suffix('') if authorized_key else None
        if private and private.is_file():       # the key the root is built to accept
            self.argv += ['-i', str(private)]
        self.argv.append(f'root@{self.address}')

    def call(self, command, data=None, timeout=120):
        """(exit status, stdout); 255 with '' when the unit does not answer."""
        try:
            result = subprocess.run(self.argv + [command], input=data, capture_output=True,
                                    timeout=timeout, **({} if data is not None else
                                                        {'stdin': subprocess.DEVNULL}))
        except subprocess.TimeoutExpired:
            return 255, ''
        text = result.stdout.decode(errors='replace')
        if result.returncode not in (0, 255):
            text += result.stderr.decode(errors='replace')
        return result.returncode, text


def tool(link, *arguments, timeout=180):
    """Run system-update on the unit from this tree's copy; (exit status, its output)."""
    command = 'sh -s -- ' + ' '.join(shlex.quote(str(a)) for a in arguments)
    return link.call(command, data=SCRIPT.read_bytes(), timeout=timeout)


def status(link):
    """system-update status as a dict; None when the unit does not answer."""
    code, text = tool(link, 'status')
    if code == 255:
        return None
    if code:
        raise StepFailed(f'the unit could not report its state: {text.strip()[-300:]}')
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def wait(link, answering, seconds, clock, sleep):
    """True once the unit answers (or, with answering False, stops answering)."""
    deadline = clock() + seconds
    while clock() < deadline:
        code, _ = link.call('true', timeout=10)
        if (code == 0) == answering:
            return True
        sleep(2)
    return False


def run(settings, serial=None, authorized_key=None, link=None, clock=time.monotonic, sleep=time.sleep):
    config.require_images(config.ROOTFS)
    serial = config.pick_unit(settings.data_dir, serial)
    directory = config.unit_dir(settings.data_dir, serial)
    image, _ = provision.provision(directory, authorized_key, address=settings.device_ip)
    data = Path(image).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    link = link or Link(settings, directory, authorized_key)

    state = status(link)
    if state is None:
        raise StepFailed(f'the unit does not answer over ssh at {link.address}; '
                         'without it, update over the serial console (init/flash --rootfs)')
    print(f'flash: the unit runs root {state["running"][:12]}')
    if state.get('boot') != 'installed':
        raise StepFailed('the unit runs from RAM, not from its installed root')
    if state['running'] == digest and state.get('staged') == digest:
        print('  rootfs: already written, by a run that did not get to confirm it')
        return confirm(link, digest)       # (its storage was checked when it booted)
    if state.get('staged') or state.get('tried') or state.get('failed'):
        print('  clearing an earlier update that was left staged')
        cancel(link)
    if state['running'] == digest:
        print('  rootfs: already current')
        return 0
    if state.get('loader') != 'applies':
        raise StepFailed('the loader on this unit does not apply staged updates: flash it '
                         'once over the serial console (init/flash --loader)')

    print(f'  rootfs: sending {Path(image).name} ({len(data)} bytes)')
    code, text = link.call(f'cat > {STAGING}.part && mv {STAGING}.part {STAGING}', data=data, timeout=300)
    if code:
        raise StepFailed(f'the image did not reach the unit: {text.strip()[-300:]}')
    code, text = tool(link, 'stage', STAGING, digest, timeout=600)
    if code:
        raise StepFailed(f'the unit did not stage it: {text.strip()[-300:]}')
    print('  rootfs: staged; rebooting, and the loader writes it')
    link.call('reboot', timeout=20)
    if not wait(link, False, DOWN_SECONDS, clock, sleep):
        cancel(link)
        raise StepFailed('the unit did not reboot; nothing was written')

    if not wait(link, True, UP_SECONDS, clock, sleep):
        print(f'  the unit has not answered for {UP_SECONDS} s.\n'
              '  POWER-CYCLE THE UNIT NOW: its loader then puts the previous root back.\n'
              f'  waiting up to {POWER_CYCLE_SECONDS // 60} min')
        if not wait(link, True, POWER_CYCLE_SECONDS, clock, sleep):
            raise StepFailed('the unit does not answer; reach it over the serial console '
                             '(init/flash --detect-only)')
    # Its storage check first: it holds only while nothing has written flash since the boot,
    # and asking for the state opens durable storage.
    storage = link.call('/usr/sbin/verify-storage', timeout=120)
    state = status(link)
    if state is None:
        raise StepFailed('the unit stopped answering again')
    if state['running'] == digest:
        return confirm(link, digest, storage)
    failed = state.get('failed') == digest
    cancel(link)
    if failed:
        raise StepFailed('the new root was written but not kept: the loader has put the '
                         f'previous root ({state["running"][:12]}) back, and it runs')
    raise StepFailed(f'the loader did not write the new root; the previous one ({state["running"][:12]}) '
                     'runs. The serial console shows why (init/flash --detect-only)')


def confirm(link, digest, storage=None):
    """The new root runs: with its storage check passed (`storage`, verify-storage's status
    and output from before anything else touched the unit), have it kept."""
    if storage is not None and (storage[0] or 'W1700K_STORAGE_READY' not in storage[1]):
        raise StepFailed('the new root runs but its storage check fails; it is left unconfirmed, '
                         f'and the next reboot puts the previous root back: {storage[1].strip()[-300:]}')
    code, text = tool(link, 'confirm')
    if code:
        raise StepFailed(f'the new root runs but could not be confirmed: {text.strip()[-300:]}')
    print(f'flash: done; root {digest[:12]} runs and is confirmed')
    return 0


def cancel(link):
    code, text = tool(link, 'cancel')
    if code:
        raise StepFailed(f'a staged update could not be cleared: {text.strip()[-300:]}')
