# SPDX-License-Identifier: MIT
"""init/flash --rootfs --ssh (init/initkit/remote.py) against a simulated unit: what it sends over
ssh and in which order, and what it makes of a unit that takes the new root, does not come back,
or comes back on the old one."""
import argparse
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'init')]
from initkit import bundle, config, provision, remote  # noqa: E402
from initkit.console import StepFailed  # noqa: E402

OLD = hashlib.sha256(b'the root that runs').hexdigest()


def proc_mtd(geometry=None):
    """/proc/mtd of a unit laid out as `geometry` (default: this tree's)."""
    geometry = geometry or config.partitions.GEOMETRY
    return 'dev:    size   erasesize  name\n' + ''.join(
        f'mtd{n}: {size:08x} 00020000 "{name}"\n' for n, (name, (_, size)) in enumerate(geometry.items()))


class Unit:
    """A unit as remote.Link reaches it. `takes`: the loader writes a staged root; `boots`: the
    new root comes up; a root that does not is undone when the operator power-cycles, which the
    simulation does after `patience` unanswered calls."""

    def __init__(self, loader='applies', takes=True, boots=True, storage='W1700K_STORAGE_READY\n', patience=20):
        self.address = '192.168.1.1'
        self.loader, self.takes, self.boots, self.storage, self.patience = loader, takes, boots, storage, patience
        self.running, self.boot = OLD, 'installed'
        self.staged = self.previous = self.tried = self.failed = ''
        self.files, self.calls, self.down, self.unanswered = {}, [], 0, 0
        self.reachable = True

    def call(self, command, data=None, timeout=120):
        if not self.reachable:
            return 255, ''
        if self.down:
            self.unanswered += 1
            if self.down == 'dead' and self.unanswered < self.patience:
                return 255, ''
            if self.down == 'dead':                 # the power cycle: the loader goes back
                self.running, self.tried, self.failed, self.down = self.previous, '', self.staged, 0
            elif self.unanswered > 3:
                self.down = 0
            if self.down:
                return 255, ''
        self.calls.append(command)
        if command == 'true':
            return 0, ''
        if command.startswith('cat > '):
            self.files[command.split()[2]] = data
            return 0, ''
        if command == 'reboot':
            self.reboot()
            return 255, ''
        if command == '/usr/sbin/verify-storage':
            return (0 if 'READY' in self.storage else 1), self.storage
        if command == 'cat /proc/mtd':
            return 0, proc_mtd(getattr(self, 'geometry', None))
        assert command.startswith('sh -s -- ') and data == remote.SCRIPT.read_bytes(), command
        verb, *arguments = command.split()[3:]
        if verb == 'status':
            return 0, ''.join(f'{k}={v}\n' for k, v in (
                ('running', self.running), ('boot', self.boot), ('loader', 'applies' if self.loader == 'applies'
                                                                 else 'serial-only'),
                ('staged', self.staged), ('previous', self.previous), ('tried', self.tried), ('failed', self.failed)))
        if verb == 'stage':
            path, digest = arguments
            moved = self.files.get(path + '.part')
            if moved is not None:
                self.files[path] = moved
            assert hashlib.sha256(self.files[path]).hexdigest() == digest
            self.staged, self.previous = digest, self.running
            return 0, 'system-update: staged\n'
        if verb == 'confirm':
            if self.running != self.staged:
                return 1, 'system-update: the staged root does not run\n'
            self.staged = self.previous = ''
            return 0, 'system-update: settled\n'
        if verb == 'cancel':
            self.staged = self.previous = ''
            return 0, 'system-update: nothing is staged\n'
        raise AssertionError(command)

    def reboot(self):
        self.down, self.unanswered = 'rebooting', 0
        if not self.staged:                         # settled: the loader forgets its note
            self.tried = self.failed = ''
        elif self.tried == self.staged:             # never confirmed: back
            self.running, self.tried, self.failed = self.previous, '', self.staged
        elif self.takes and self.failed != self.staged:
            self.running, self.tried = self.staged, self.staged
            if not self.boots:
                self.down = 'dead'


class Remote(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.image = Path(work.name) / 'rootfs.squashfs'
        self.image.write_bytes(b'hsqs the new root' * 1000)
        self.new = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.settings = argparse.Namespace(device_ip='192.168.1.1', data_dir=Path(work.name))
        self.now = 0.0
        for patcher in (mock.patch.object(config, 'require_images'),
                        mock.patch.object(config, 'pick_unit', return_value='SERIAL'),
                        mock.patch.object(provision, 'provision', return_value=(self.image, 'key')),
                        mock.patch('builtins.print')):
            self.printed = patcher.start()
            self.addCleanup(patcher.stop)

    def run_update(self, unit):
        def sleep(seconds):
            self.now += seconds
        return remote.run(self.settings, link=unit, clock=lambda: self.now, sleep=sleep)

    def said(self):
        return ' '.join(str(call.args[0]) for call in self.printed.call_args_list if call.args)

    def verbs(self, unit):
        return [c.split()[3] if c.startswith('sh -s') else c.split()[0] for c in unit.calls
                if c not in ('true', 'cat /proc/mtd')]

    def test_the_new_root_is_staged_written_by_the_loader_checked_and_confirmed(self):
        unit = Unit()
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual((unit.running, unit.staged), (self.new, ''))
        self.assertEqual(self.verbs(unit),
                         ['status', 'cat', 'stage', 'reboot', '/usr/sbin/verify-storage', 'status', 'confirm'],
                         'the storage check before anything opens durable storage again')
        self.assertIn('runs and is confirmed', self.said())
        unit.reboot()
        self.assertEqual((unit.running, unit.tried), (self.new, ''), 'and it stays')

    def test_a_root_that_already_runs_is_left_alone(self):
        unit = Unit()
        unit.running = self.new
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual(self.verbs(unit), ['status'])
        self.assertIn('already current', self.said())

    def test_a_loader_that_does_not_apply_updates_is_named_before_anything_is_sent(self):
        unit = Unit(loader='old')
        with self.assertRaisesRegex(StepFailed, 'init/flash --loader'):
            self.run_update(unit)
        self.assertEqual(self.verbs(unit), ['status'])

    def test_a_unit_that_does_not_answer(self):
        unit = Unit()
        unit.reachable = False
        with self.assertRaisesRegex(StepFailed, 'does not answer over ssh'):
            self.run_update(unit)

    def test_a_root_that_never_comes_up_is_undone_by_the_power_cycle_it_asks_for(self):
        unit = Unit(boots=False, patience=200)
        with self.assertRaisesRegex(StepFailed, 'previous root .* back, and it runs'):
            self.run_update(unit)
        self.assertIn('POWER-CYCLE THE UNIT NOW', self.said())
        self.assertEqual((unit.running, unit.staged), (OLD, ''), 'the old root runs, and nothing is left staged')

    def test_a_loader_that_wrote_nothing(self):
        unit = Unit(takes=False)
        with self.assertRaisesRegex(StepFailed, 'did not write the new root'):
            self.run_update(unit)
        self.assertEqual((unit.running, unit.staged), (OLD, ''))

    def test_a_new_root_whose_storage_check_fails_is_not_confirmed(self):
        unit = Unit(storage='W1700K_STORAGE_FAILED: ubi0 is not read-only\n')
        with self.assertRaisesRegex(StepFailed, 'storage check fails'):
            self.run_update(unit)
        self.assertEqual(unit.staged, self.new, 'left unconfirmed')
        unit.reboot()
        self.assertEqual(unit.running, OLD, 'so the next boot goes back')

    def test_a_run_that_was_interrupted_before_confirming_is_finished_by_the_next(self):
        unit = Unit()
        unit.running = unit.staged = unit.tried = self.new
        unit.previous = OLD
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual(self.verbs(unit), ['status', 'confirm'])
        self.assertEqual(unit.staged, '')

    def test_an_earlier_update_left_staged_is_cleared_first(self):
        unit = Unit()
        unit.staged, unit.previous, unit.failed = 'f' * 64, OLD, 'f' * 64
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual(self.verbs(unit)[:3], ['status', 'cancel', 'cat'])
        self.assertEqual(unit.running, self.new)



class LoaderUnit:
    """A unit as remote.Link reaches it, for its loader's update. `takes`: the first loader
    writes the staged second loader and starts it; `boots`: the system comes up under it; one
    that does not is removed by the first loader when the operator power-cycles."""

    def __init__(self, second='', takes=True, boots=True, storage='W1700K_STORAGE_READY\n', patience=20):
        self.address = '192.168.1.1'
        self.second, self.takes, self.boots, self.storage, self.patience = second, takes, boots, storage, patience
        self.staged = self.tried = self.booted = self.failed = ''
        self.files, self.calls, self.down, self.unanswered = {}, [], 0, 0

    def call(self, command, data=None, timeout=120):
        if self.down:
            self.unanswered += 1
            if self.down == 'dead' and self.unanswered < self.patience:
                return 255, ''
            if self.down == 'dead':                 # the power cycle: the first loader removes it
                self.second, self.tried, self.booted, self.failed, self.down = '', '', '', self.staged, 0
            elif self.unanswered > 3:
                self.down = 0
            if self.down:
                return 255, ''
        self.calls.append(command)
        if command == 'true':
            return 0, ''
        if command.startswith('cat > '):
            self.files[command.split()[2]] = data
            return 0, ''
        if command == 'reboot':
            self.reboot()
            return 255, ''
        if command == '/usr/sbin/verify-storage':
            return (0 if 'READY' in self.storage else 1), self.storage
        if command == 'cat /proc/mtd':
            return 0, proc_mtd(getattr(self, 'geometry', None))
        assert command.startswith('sh -s -- ') and data == remote.SCRIPT.read_bytes(), command
        verb, *arguments = command.split()[3:]
        if verb == 'status':
            return 0, ''.join(f'{k}={v}\n' for k, v in (
                ('running', OLD), ('boot', 'installed'), ('loader', 'applies'), ('staged', ''), ('previous', ''),
                ('tried', ''), ('failed', ''), ('second_loader', self.second), ('loader_staged', self.staged),
                ('loader_tried', self.tried), ('loader_booted', self.booted), ('loader_failed', self.failed)))
        if verb == 'stage-loader':
            path, digest = arguments
            moved = self.files.get(path + '.part')
            if moved is not None:
                self.files[path] = moved
            assert hashlib.sha256(self.files[path]).hexdigest() == digest
            self.staged = digest
            return 0, 'system-update: staged the loader\n'
        if verb == 'confirm':
            if not self.staged or self.booted != self.staged or self.second != self.staged:
                return 1, 'system-update: the staged loader did not start this boot\n'
            self.staged = ''
            return 0, 'system-update: settled\n'
        if verb == 'cancel':
            self.staged = ''
            return 0, 'system-update: nothing is staged\n'
        raise AssertionError(command)

    def reboot(self):
        self.down, self.unanswered = 'rebooting', 0
        if not self.staged:                         # settled: the notes are forgotten
            self.tried = self.booted = self.failed = ''
        elif self.tried == self.staged:             # never confirmed: removed
            self.second, self.tried, self.booted, self.failed = '', '', '', self.staged
        elif self.takes and self.failed != self.staged:
            self.second, self.tried = self.staged, self.staged
            if self.boots:
                self.booted = self.staged
            else:
                self.down = 'dead'


class RemoteLoader(unittest.TestCase):
    """init/flash --loader --ssh: the second loader, delivered, started and confirmed."""

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.image = Path(work.name) / 'w1700k-loader-b.itb'
        self.image.write_bytes(b'\xd0\x0d\xfe\xed a second loader' * 500)
        self.new = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.settings = argparse.Namespace(device_ip='192.168.1.1', data_dir=Path(work.name))
        self.now = 0.0
        for patcher in (mock.patch.object(config, 'require_images'),
                        mock.patch.object(config, 'pick_unit', return_value='SERIAL'),
                        mock.patch.object(config, 'LOADER_B', self.image),
                        mock.patch('builtins.print')):
            self.printed = patcher.start()
            self.addCleanup(patcher.stop)

    def run_update(self, unit):
        def sleep(seconds):
            self.now += seconds
        return remote.run_loader(self.settings, link=unit, clock=lambda: self.now, sleep=sleep)

    def said(self):
        return ' '.join(str(call.args[0]) for call in self.printed.call_args_list if call.args)

    def verbs(self, unit):
        return [c.split()[3] if c.startswith('sh -s') else c.split()[0] for c in unit.calls
                if c not in ('true', 'cat /proc/mtd')]

    def test_the_new_loader_is_staged_started_checked_and_confirmed(self):
        unit = LoaderUnit()
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual((unit.second, unit.staged), (self.new, ''))
        self.assertEqual(self.verbs(unit), ['status', 'cat', 'stage-loader', 'reboot', '/usr/sbin/verify-storage',
                                            'status', 'confirm'])
        self.assertIn(f'loader {self.new[:12]} runs and is confirmed', self.said())
        unit.reboot()
        self.assertEqual((unit.second, unit.tried), (self.new, ''), 'and it stays')

    def test_a_loader_already_in_place_is_left_alone(self):
        unit = LoaderUnit(second=self.new)
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual(self.verbs(unit), ['status'])
        self.assertIn('already current', self.said())

    def test_a_unit_whose_loader_starts_no_second_one_is_sent_nothing(self):
        unit = LoaderUnit(second='unsupported')
        with self.assertRaisesRegex(StepFailed, 'starts no second loader.*serial console'):
            self.run_update(unit)
        self.assertEqual(self.verbs(unit), ['status'])

    def test_a_loader_under_which_the_system_does_not_come_up_is_removed(self):
        unit = LoaderUnit(boots=False, patience=200)
        with self.assertRaisesRegex(StepFailed, 'not kept: the first loader has removed it'):
            self.run_update(unit)
        self.assertIn('POWER-CYCLE THE UNIT NOW', self.said())
        self.assertEqual((unit.second, unit.staged), ('', ''), 'removed, and the request cleared')

    def test_a_loader_the_first_one_did_not_start_is_reported(self):
        unit = LoaderUnit(takes=False)
        with self.assertRaisesRegex(StepFailed, 'did not start; the unit runs on what it had'):
            self.run_update(unit)
        self.assertEqual(unit.staged, '')

    def test_a_loader_is_not_confirmed_over_a_failed_storage_check(self):
        unit = LoaderUnit(storage='W1700K_STORAGE_FAILED\n')
        with self.assertRaisesRegex(StepFailed, 'storage check fails; it is left unconfirmed'):
            self.run_update(unit)
        self.assertEqual(unit.staged, self.new, 'left for the next reboot to remove')
        unit.reboot()
        self.assertEqual((unit.second, unit.failed), ('', self.new))

    def test_a_run_that_did_not_get_to_confirm_is_finished_by_the_next(self):
        unit = LoaderUnit(second=self.new)
        unit.staged = unit.tried = unit.booted = self.new
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual(self.verbs(unit), ['status', 'confirm'])



class JointUnit:
    """A unit that takes a second loader and a root in one reboot. `loader_takes`, `root_takes`:
    its loaders write and start what is staged."""

    def __init__(self, loader_takes=True, root_takes=True, geometry=None):
        self.address, self.geometry = '192.168.1.1', geometry
        self.loader_takes, self.root_takes = loader_takes, root_takes
        self.running, self.second = OLD, ''
        self.staged = self.previous = self.tried = self.failed = ''
        self.lstaged = self.ltried = self.lbooted = self.lfailed = ''
        self.files, self.calls, self.down, self.unanswered = {}, [], 0, 0

    def call(self, command, data=None, timeout=120):
        if self.down:
            self.unanswered += 1
            if self.unanswered > 3:
                self.down = 0
            else:
                return 255, ''
        self.calls.append(command)
        if command == 'true':
            return 0, ''
        if command == 'cat /proc/mtd':
            return 0, proc_mtd(self.geometry)
        if command.startswith('cat > '):
            self.files[command.split()[2]] = data
            return 0, ''
        if command == 'reboot':
            self.reboot()
            return 255, ''
        if command == '/usr/sbin/verify-storage':
            return 0, 'W1700K_STORAGE_READY\n'
        assert command.startswith('sh -s -- ') and data == remote.SCRIPT.read_bytes(), command
        verb, *arguments = command.split()[3:]
        if verb == 'status':
            return 0, ''.join(f'{k}={v}\n' for k, v in (
                ('running', self.running), ('boot', 'installed'), ('loader', 'applies'), ('staged', self.staged),
                ('previous', self.previous), ('tried', self.tried), ('failed', self.failed),
                ('second_loader', self.second), ('loader_staged', self.lstaged), ('loader_tried', self.ltried),
                ('loader_booted', self.lbooted), ('loader_failed', self.lfailed)))
        if verb in ('stage', 'stage-loader'):
            path, digest = arguments
            self.files[path] = self.files.get(path + '.part', self.files.get(path))
            assert hashlib.sha256(self.files[path]).hexdigest() == digest
            if verb == 'stage':
                self.staged, self.previous = digest, self.running
            else:
                self.lstaged = digest
            return 0, 'system-update: staged\n'
        if verb == 'confirm':
            if (self.staged and self.running != self.staged) or \
                    (self.lstaged and (self.lbooted != self.lstaged or self.second != self.lstaged)):
                return 1, 'system-update: not what was staged\n'
            self.staged = self.previous = self.lstaged = ''
            return 0, 'system-update: settled\n'
        if verb == 'cancel':
            self.staged = self.previous = self.lstaged = ''
            return 0, 'system-update: nothing is staged\n'
        raise AssertionError(command)

    def reboot(self):
        self.down, self.unanswered = 'rebooting', 0
        self.lbooted = ''
        if self.lstaged and self.ltried == self.lstaged:        # never confirmed: removed
            self.second, self.ltried, self.lfailed = '', '', self.lstaged
        elif self.lstaged and self.loader_takes and self.lfailed != self.lstaged:
            self.second = self.ltried = self.lbooted = self.lstaged
        if self.staged and self.tried == self.staged:           # never confirmed: back
            self.running, self.tried, self.failed = self.previous, '', self.staged
        elif self.staged and self.root_takes and self.failed != self.staged:
            self.running = self.tried = self.staged
        if not self.staged and not self.lstaged:                # settled
            self.tried = self.failed = self.ltried = self.lfailed = ''


class RemoteJoint(unittest.TestCase):
    """init/flash --ssh and --bundle: the loader and the root together, with one reboot."""

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root_image = self.work / 'rootfs.squashfs'
        self.root_image.write_bytes(b'hsqs the new root' * 1000)
        self.loader_image = self.work / 'w1700k-loader-b.itb'
        self.loader_image.write_bytes(b'\xd0\x0d\xfe\xed a second loader' * 500)
        self.root = hashlib.sha256(self.root_image.read_bytes()).hexdigest()
        self.loader = hashlib.sha256(self.loader_image.read_bytes()).hexdigest()
        self.settings = argparse.Namespace(device_ip='192.168.1.1', data_dir=self.work)
        self.now = 0.0
        self.provisioned = []

        def provisioned(directory, key, address=None, generic=None):
            self.provisioned.append(Path(generic).read_bytes() if generic else None)
            return self.root_image, 'key'
        for patcher in (mock.patch.object(config, 'require_images'),
                        mock.patch.object(config, 'pick_unit', return_value='SERIAL'),
                        mock.patch.object(config, 'LOADER_B', self.loader_image),
                        mock.patch.object(provision, 'provision', side_effect=provisioned),
                        mock.patch('builtins.print')):
            self.printed = patcher.start()
            self.addCleanup(patcher.stop)

    def run_update(self, unit, bundle_path=None):
        def sleep(seconds):
            self.now += seconds
        return remote.run_joint(self.settings, bundle_path=bundle_path, link=unit, clock=lambda: self.now,
                                sleep=sleep)

    def said(self):
        return ' '.join(str(call.args[0]) for call in self.printed.call_args_list if call.args)

    def verbs(self, unit):
        return [c.split()[3] if c.startswith('sh -s') else c.split()[0] for c in unit.calls
                if c not in ('true', 'cat /proc/mtd')]

    def test_both_are_staged_and_one_reboot_brings_up_both(self):
        unit = JointUnit()
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual((unit.running, unit.second), (self.root, self.loader))
        self.assertEqual(self.verbs(unit), ['status', 'cat', 'stage-loader', 'cat', 'stage', 'reboot',
                                            '/usr/sbin/verify-storage', 'status', 'confirm'])
        self.assertEqual((unit.staged, unit.lstaged), ('', ''))
        self.assertIn(f'loader {self.loader[:12]} and root {self.root[:12]} run and are confirmed', self.said())
        self.assertEqual(self.provisioned, [None], "this build's root, with the unit's keys")

    def test_an_archive_brings_the_same_two_files(self):
        archive = self.work / 'w1700k-update.tar'
        generic = b'hsqs the root as built, without keys'
        bundle.write(archive, self.loader_image.read_bytes(), generic, config.partitions.GEOMETRY)
        unit = JointUnit()
        self.assertEqual(self.run_update(unit, archive), 0)
        self.assertEqual(self.provisioned, [generic], "the archive's root is given the unit's keys")
        self.assertEqual((unit.running, unit.second), (self.root, self.loader))
        self.assertEqual(self.verbs(unit).count('reboot'), 1)

    def test_an_archive_for_another_layout_is_not_sent(self):
        archive = self.work / 'w1700k-update.tar'
        other = dict(config.partitions.GEOMETRY, system=(0x800000, 0x2200000))
        bundle.write(archive, self.loader_image.read_bytes(), b'hsqs', other)
        unit = JointUnit()
        with self.assertRaisesRegex(StepFailed, 'not laid out as these images expect \\(system\\).*init/install --force'):
            self.run_update(unit, archive)
        self.assertEqual(self.verbs(unit), ['status'])

    def test_a_unit_installed_under_an_older_layout_is_not_sent_this_build(self):
        older = {name: span for name, span in config.partitions.GEOMETRY.items() if name != 'loader-b'}
        older.update(loader=(0x600000, 0x200000), system=(0x800000, 0x2200000))
        unit = JointUnit(geometry=older)
        with self.assertRaisesRegex(StepFailed, 'not laid out as these images expect'):
            self.run_update(unit)
        self.assertEqual(self.verbs(unit), ['status'])

    def test_an_archive_that_does_not_hold_together_is_refused(self):
        archive = self.work / 'w1700k-update.tar'
        bundle.write(archive, self.loader_image.read_bytes(), b'hsqs', config.partitions.GEOMETRY)
        data = bytearray(archive.read_bytes())
        at = data.index(b'a second loader')
        data[at] ^= 1
        archive.write_bytes(bytes(data))
        unit = JointUnit()
        with self.assertRaisesRegex(StepFailed, 'is not the file its manifest lists'):
            self.run_update(unit, archive)
        (self.work / 'other.tar').write_bytes(b'not an archive')
        with self.assertRaisesRegex(StepFailed, 'cannot be read as an update archive'):
            self.run_update(unit, self.work / 'other.tar')
        self.assertEqual(unit.calls, [])

    def test_only_what_differs_is_sent(self):
        unit = JointUnit()
        unit.running = self.root
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual(self.verbs(unit), ['status', 'cat', 'stage-loader', 'reboot', '/usr/sbin/verify-storage',
                                            'status', 'confirm'])
        self.assertIn('rootfs: already current', self.said())
        unit.calls.clear()
        self.assertEqual(self.run_update(unit), 0)
        self.assertEqual(self.verbs(unit), ['status'], 'nothing left to do')

    def test_a_part_that_did_not_take_leaves_nothing_confirmed(self):
        unit = JointUnit(root_takes=False)
        with self.assertRaisesRegex(StepFailed, 'the new root does not run.*Nothing was confirmed'):
            self.run_update(unit)
        self.assertEqual((unit.staged, unit.lstaged), ('', ''), 'the request is cleared')
        unit = JointUnit(loader_takes=False)
        with self.assertRaisesRegex(StepFailed, 'the new loader did not start'):
            self.run_update(unit)


if __name__ == '__main__':
    unittest.main()
