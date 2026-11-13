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
from initkit import config, provision, remote  # noqa: E402
from initkit.console import StepFailed  # noqa: E402

OLD = hashlib.sha256(b'the root that runs').hexdigest()


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
        return [c.split()[3] if c.startswith('sh -s') else c.split()[0] for c in unit.calls if c != 'true']

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


if __name__ == '__main__':
    unittest.main()
