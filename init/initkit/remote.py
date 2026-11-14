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
import re
import shlex
import subprocess
import tempfile
import time

from . import bundle, config, provision
from .console import StepFailed

SCRIPT = config.ROOT / 'platform/rootfs/overlay/usr/sbin/system-update'
STAGING = '/work/system-update.squashfs'      # in RAM on the unit
LOADER_STAGING = '/work/system-update.loader'
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
    check_layout(link)

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


MTD_LINE = re.compile(r'^mtd\d+: ([0-9a-f]+) [0-9a-f]+ "([^"]+)"$', re.M)


def check_layout(link, expected=None):
    """The unit's flash is divided as these images expect (`expected`: {partition: (offset,
    size)}, default this tree's). A root built for another layout looks for its durable storage
    in the wrong place, and a loader for another would write there."""
    expected = expected or config.partitions.GEOMETRY
    code, text = link.call('cat /proc/mtd', timeout=30)
    if code:
        raise StepFailed(f'the unit did not list its flash partitions: {text.strip()[-200:]}')
    found = {name: int(size, 16) for size, name in MTD_LINE.findall(text)}
    wanted = {name: size for name, (_, size) in expected.items()}
    if found != wanted:
        differ = sorted(name for name in set(found) | set(wanted) if found.get(name) != wanted.get(name))
        raise StepFailed("the unit's flash is not laid out as these images expect (" + ', '.join(differ)
                         + '): it was installed from another version. Install it again over the serial '
                         'console (init/install --force); init/README.md says what else an older '
                         'installation needs')


def run_joint(settings, serial=None, authorized_key=None, bundle_path=None, link=None,
              clock=time.monotonic, sleep=time.sleep):
    """The loader and the root in one go, with one reboot: from the update archive at
    `bundle_path` (init/flash --bundle), or from this build's images. Both are staged, the
    first loader writes and starts the second, which writes the root, and once the system is
    up on both they are confirmed together. A part that is already what the unit has is left
    out; what is not confirmed is undone by the next boot, each part as it is alone."""
    serial = config.pick_unit(settings.data_dir, serial)
    directory = config.unit_dir(settings.data_dir, serial)
    layout = None
    with tempfile.TemporaryDirectory(prefix='w1700k-update-') as temporary:
        if bundle_path is not None:
            manifest, files = bundle.read(bundle_path)
            layout, loader_data = manifest['partitions'], files[bundle.LOADER]
            generic = Path(temporary) / bundle.ROOT
            generic.write_bytes(files[bundle.ROOT])
        else:
            config.require_images(config.ROOTFS, config.LOADER_B)
            loader_data, generic = config.LOADER_B.read_bytes(), None
        image, _ = provision.provision(directory, authorized_key, address=settings.device_ip, generic=generic)
    root_data = Path(image).read_bytes()
    root, loader = hashlib.sha256(root_data).hexdigest(), hashlib.sha256(loader_data).hexdigest()
    link = link or Link(settings, directory, authorized_key)

    state = status(link)
    if state is None:
        raise StepFailed(f'the unit does not answer over ssh at {link.address}; '
                         'without it, update over the serial console (init/flash --system --loader)')
    if state.get('boot') != 'installed':
        raise StepFailed('the unit runs from RAM, not from its installed root')
    if state.get('loader') != 'applies' or state.get('second_loader', 'unsupported') == 'unsupported':
        raise StepFailed('the loader on this unit does not take updates of both: flash it once over '
                         'the serial console (init/flash --loader)')
    check_layout(link, layout)
    print(f'flash: the unit runs root {state["running"][:12]}, second loader '
          f'{state["second_loader"][:12] or "absent"}')
    need_root, need_loader = state['running'] != root, state['second_loader'] != loader
    # Something still staged is an earlier run's; the loader's own notes without it are those of
    # an update that was settled, and go with the next boot.
    if state.get('staged') or state.get('loader_staged'):
        ours = (state.get('staged') in ('', root) and state.get('loader_staged') in ('', loader)
                and (state.get('staged') or state.get('loader_staged')))
        if ours and not need_root and not (state.get('loader_staged') and state.get('loader_booted') != loader) \
                and not need_loader:
            print('  already written, by a run that did not get to confirm it')
            return confirm_joint(link, root if state.get('staged') else None,
                                 loader if state.get('loader_staged') else None)
        print('  clearing an earlier update that was left staged')
        cancel(link)
    if not need_root and not need_loader:
        print('  loader and rootfs: already current')
        return 0

    for wanted, name, data, path, verb in ((need_loader, 'loader', loader_data, LOADER_STAGING, 'stage-loader'),
                                           (need_root, 'rootfs', root_data, STAGING, 'stage')):
        if not wanted:
            print(f'  {name}: already current')
            continue
        print(f'  {name}: sending {len(data)} bytes')
        code, text = link.call(f'cat > {path}.part && mv {path}.part {path}', data=data, timeout=300)
        if code:
            raise StepFailed(f'the {name} image did not reach the unit: {text.strip()[-300:]}')
        code, text = tool(link, verb, path, hashlib.sha256(data).hexdigest(), timeout=600)
        if code:
            cancel(link)
            raise StepFailed(f'the unit did not stage the {name}: {text.strip()[-300:]}')
    print('  staged; rebooting once, and the loader does the writing')
    link.call('reboot', timeout=20)
    if not wait(link, False, DOWN_SECONDS, clock, sleep):
        cancel(link)
        raise StepFailed('the unit did not reboot; nothing was written')
    if not wait(link, True, UP_SECONDS, clock, sleep):
        print(f'  the unit has not answered for {UP_SECONDS} s.\n'
              '  POWER-CYCLE THE UNIT NOW: its first loader then undoes what was not confirmed.\n'
              f'  waiting up to {POWER_CYCLE_SECONDS // 60} min')
        if not wait(link, True, POWER_CYCLE_SECONDS, clock, sleep):
            raise StepFailed('the unit does not answer; reach it over the serial console '
                             '(init/flash --detect-only)')
    storage = link.call('/usr/sbin/verify-storage', timeout=120)
    state = status(link)
    if state is None:
        raise StepFailed('the unit stopped answering again')
    root_ok = not need_root or state['running'] == root
    loader_ok = not need_loader or (state.get('second_loader') == loader and state.get('loader_booted') == loader)
    if root_ok and loader_ok:
        return confirm_joint(link, root if need_root else None, loader if need_loader else None, storage)
    cancel(link)
    what = [text for ok, text in ((loader_ok, 'the new loader did not start, or was removed again'),
                                  (root_ok, f'the new root does not run (root {state["running"][:12]} does)'))
            if not ok]
    raise StepFailed('; '.join(what) + '. Nothing was confirmed: the unit is on what it had, or will be '
                     'after its next reboot. The serial console shows why (init/flash --detect-only)')


def confirm_joint(link, root, loader, storage=None):
    """The system is up on what was staged: with its storage check passed, have it kept."""
    if storage is not None and (storage[0] or 'W1700K_STORAGE_READY' not in storage[1]):
        raise StepFailed('the update runs but the storage check fails; it is left unconfirmed, and the '
                         f'next reboot undoes it: {storage[1].strip()[-300:]}')
    code, text = tool(link, 'confirm')
    if code:
        raise StepFailed(f'the update runs but could not be confirmed: {text.strip()[-300:]}')
    parts = [f'{name} {digest[:12]}' for name, digest in (('loader', loader), ('root', root)) if digest]
    print('flash: done; ' + ' and '.join(parts)
          + (' run and are confirmed' if len(parts) > 1 else ' runs and is confirmed'))
    return 0


def run_loader(settings, serial=None, authorized_key=None, link=None, clock=time.monotonic,
               sleep=time.sleep):
    """A newer loader over ssh: the second loader (config.LOADER_B) is staged, the unit's first
    loader writes it to `loader-b` at the reboot and starts it, and once the system is up on
    it, it is confirmed. One that is never confirmed is removed by the first loader at the
    next boot; the loader stock boots is not written here at all."""
    config.require_images(config.LOADER_B)
    serial = config.pick_unit(settings.data_dir, serial)
    directory = config.unit_dir(settings.data_dir, serial)
    data = config.LOADER_B.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    link = link or Link(settings, directory, authorized_key)

    state = status(link)
    if state is None:
        raise StepFailed(f'the unit does not answer over ssh at {link.address}; '
                         'without it, update over the serial console (init/flash --loader)')
    if state.get('boot') != 'installed':
        raise StepFailed('the unit runs from RAM, not from its installed root')
    if state.get('second_loader', 'unsupported') == 'unsupported':
        raise StepFailed('the loader on this unit starts no second loader: flash it once over '
                         'the serial console (init/flash --loader)')
    check_layout(link)
    print(f'flash: the unit\'s second loader is {state["second_loader"][:12] or "absent"}')
    if state['second_loader'] == digest and state.get('loader_staged') == digest:
        print('  loader: already written, by a run that did not get to confirm it')
        return confirm_loader(link, digest)
    if any(state.get(key) for key in ('staged', 'tried', 'failed', 'loader_staged', 'loader_tried',
                                      'loader_failed')):
        print('  clearing an earlier update that was left staged')
        cancel(link)
    if state['second_loader'] == digest:
        print('  loader: already current')
        return 0

    print(f'  loader: sending {config.LOADER_B.name} ({len(data)} bytes)')
    code, text = link.call(f'cat > {LOADER_STAGING}.part && mv {LOADER_STAGING}.part {LOADER_STAGING}',
                           data=data, timeout=300)
    if code:
        raise StepFailed(f'the image did not reach the unit: {text.strip()[-300:]}')
    code, text = tool(link, 'stage-loader', LOADER_STAGING, digest, timeout=600)
    if code:
        raise StepFailed(f'the unit did not stage it: {text.strip()[-300:]}')
    print('  loader: staged; rebooting, and the first loader writes and starts it')
    link.call('reboot', timeout=20)
    if not wait(link, False, DOWN_SECONDS, clock, sleep):
        cancel(link)
        raise StepFailed('the unit did not reboot; nothing was written')
    if not wait(link, True, UP_SECONDS, clock, sleep):
        print(f'  the unit has not answered for {UP_SECONDS} s.\n'
              '  POWER-CYCLE THE UNIT NOW: its first loader then removes the new one.\n'
              f'  waiting up to {POWER_CYCLE_SECONDS // 60} min')
        if not wait(link, True, POWER_CYCLE_SECONDS, clock, sleep):
            raise StepFailed('the unit does not answer; reach it over the serial console '
                             '(init/flash --detect-only)')
    storage = link.call('/usr/sbin/verify-storage', timeout=120)
    state = status(link)
    if state is None:
        raise StepFailed('the unit stopped answering again')
    if state.get('second_loader') == digest and state.get('loader_booted') == digest:
        return confirm_loader(link, digest, storage)
    failed = state.get('loader_failed') == digest
    cancel(link)
    if failed:
        raise StepFailed('the new loader was written but not kept: the first loader has removed '
                         'it, and the unit runs on the first loader alone')
    raise StepFailed('the new loader did not start; the unit runs on what it had. The serial '
                     'console shows why (init/flash --detect-only)')


def confirm_loader(link, digest, storage=None):
    """The system is up on the new loader: with its storage check passed, have it kept."""
    if storage is not None and (storage[0] or 'W1700K_STORAGE_READY' not in storage[1]):
        raise StepFailed('the new loader started the system but its storage check fails; it is left '
                         f'unconfirmed, and the next reboot removes it: {storage[1].strip()[-300:]}')
    code, text = tool(link, 'confirm')
    if code:
        raise StepFailed(f'the new loader runs but could not be confirmed: {text.strip()[-300:]}')
    print(f'flash: done; loader {digest[:12]} runs and is confirmed')
    return 0


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
