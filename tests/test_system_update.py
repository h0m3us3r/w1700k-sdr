# SPDX-License-Identifier: MIT
"""The unit's own system-update (platform/rootfs/overlay/usr/sbin/system-update), run as it is but
for where it looks: durable storage and the flash are directories, and durable storage can be
made to refuse, as it does while another program has it open, or to lose power part-way.

What is held to here: nothing is decided on a read that was not made, a confirmation cannot be
half undone by a power cut, and cancelling does not quietly keep what the loader already wrote."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'platform/rootfs/overlay/usr/sbin/system-update'
OLD_ROOT, NEW_ROOT = b'hsqs the root that runs', b'hsqs the root to come'
LOADER = b'\xd0\x0d\xfe\xed' + b'W1700K SDR second ARM loader' + bytes(range(256)) * 4

OVERRIDES = '''
installed() { :; }
loader_applies() { :; }
loader_starts_second() { :; }
partition() { echo "@D@/dev/mtd5ro"; }
volume() { echo "@D@/dev/ubi0_1"; }
loader_note() { cat "@D@/notes/$1" 2>/dev/null || :; }
second_loader() { cat "@D@/second" 2>/dev/null || :; }
'''

# durable-run: the command, unless this call is one of those listed in `refuse` (durable storage
# is open elsewhere). Every call is counted.
DURABLE_RUN = '''#!/bin/sh
d=@D@
n=$(($(cat $d/calls 2>/dev/null || echo 0) + 1))
echo $n > $d/calls
if grep -qx "$n" $d/refuse 2>/dev/null || grep -qx all $d/refuse 2>/dev/null; then
	echo "durable-open: durable is already open" >&2
	exit 1
fi
echo "durable-open: /durable mounted from mtd7"
"$@"
'''

# sync: with `cut` there, the power goes at this point: the shell that called it gets no further.
SYNC = '''#!/bin/sh
[ ! -e @D@/cut ] || kill -9 $PPID
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Unit(unittest.TestCase):
    def setUp(self):
        self.dir = d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        for name in ('durable', 'dev', 'notes', 'sys/mtd/mtd5', 'sys/ubi/ubi0_1', 'work', 'bin'):
            (d / name).mkdir(parents=True)
        (d / 'dev/ubi0_1').write_bytes(OLD_ROOT)
        (d / 'sys/mtd/mtd5/size').write_text('786432\n')
        (d / 'sys/ubi/ubi0_1/reserved_ebs').write_text('30\n')
        (d / 'sys/ubi/ubi0_1/usable_eb_size').write_text('126976\n')
        text = SCRIPT.read_text()
        for old, new in (('. /etc/w1700k/storage.conf\n', ''),
                         ('/usr/sbin/durable-run', f'{d}/durable-run'),
                         ('STAGED=/durable/update', f'STAGED={d}/durable/update'),
                         ('df -k /durable', f'df -k {d}/durable'),
                         ('/sys/class/mtd', f'{d}/sys/mtd'), ('/sys/class/ubi', f'{d}/sys/ubi'),
                         ('\t\tsleep 1\n', '\t\tsleep 0\n'),
                         ('case ${1:-} in\nstatus)', OVERRIDES.replace('@D@', str(d)) + 'case ${1:-} in\nstatus)')):
            self.assertIn(old, text)
            text = text.replace(old, new)
        (d / 'system-update').write_text(text)
        for name, body in (('durable-run', DURABLE_RUN), ('bin/sync', SYNC)):
            (d / name).write_text(body.replace('@D@', str(d)))
            (d / name).chmod(0o755)
        self.staged = d / 'durable/update'

    def update(self, *arguments):
        return subprocess.run(['sh', str(self.dir / 'system-update'), *map(str, arguments)], capture_output=True, text=True,
                              env=dict(os.environ, PATH=f'{self.dir}/bin:{os.environ["PATH"]}'))

    def stage(self, verb='stage', data=NEW_ROOT):
        path = self.dir / 'work/image'
        path.write_bytes(data)
        return self.update(verb, path, digest(data))

    def refuse(self, *calls):
        """Durable storage is open elsewhere for these calls of durable-run, counted from the next."""
        (self.dir / 'calls').unlink(missing_ok=True)
        (self.dir / 'refuse').write_text(''.join(f'{call}\n' for call in calls))

    def state(self):
        result = self.update('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split('=', 1) for line in result.stdout.splitlines())

    def written(self, root=True, loader=False):
        """The reboot: the loader wrote what was staged, and it runs."""
        if root:
            (self.dir / 'dev/ubi0_1').write_bytes(NEW_ROOT)
            (self.dir / 'notes/update_tried').write_text(digest(NEW_ROOT))
        if loader:
            (self.dir / 'second').write_text(digest(LOADER))
            (self.dir / 'notes/loader_booted').write_text(digest(LOADER))

    def request(self):
        path = self.staged / 'request'
        return path.read_text() if path.exists() else None


class Staging(Unit):
    def test_a_root_staged_is_a_request_written_last_with_both_roots_beside_it(self):
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.request(), f'update_new={digest(NEW_ROOT)}\nupdate_old={digest(OLD_ROOT)}\n')
        self.assertEqual((self.staged / 'rootfs.new').read_bytes(), NEW_ROOT)
        self.assertEqual((self.staged / 'rootfs.old').read_bytes(), OLD_ROOT)
        self.assertEqual((self.state()['staged'], self.state()['previous']), (digest(NEW_ROOT), digest(OLD_ROOT)))

    def test_what_is_not_a_root_is_not_staged_as_one(self):
        for what, image in (('a second loader', LOADER), ('some file', b'not an image at all')):
            with self.subTest(what):
                result = self.stage(data=image)
                self.assertEqual(result.returncode, 1)
                self.assertIn('is not a SquashFS root', result.stderr)
                self.assertIsNone(self.request())

    def test_a_loader_staged_beside_a_root_keeps_the_roots_request(self):
        self.stage()
        self.assertEqual(self.stage('stage-loader', LOADER).returncode, 0)
        self.assertEqual(self.request(), f'update_new={digest(NEW_ROOT)}\nupdate_old={digest(OLD_ROOT)}\n'
                                         f'loader_new={digest(LOADER)}\n')
        self.assertEqual(self.state()['loader_staged'], digest(LOADER))


class ReadsThatWereNotMade(Unit):
    """Durable storage is open elsewhere now and then: the reset button's service and the tools
    that keep a key or a setting open it, each for a moment."""

    def test_storage_that_is_busy_for_a_moment_is_waited_for(self):
        self.stage()
        self.written()
        self.refuse(1, 2, 3)
        result = self.update('confirm')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'settled on root {digest(NEW_ROOT)}', result.stdout)

    def test_storage_that_stays_closed_decides_nothing(self):
        self.stage()
        self.stage('stage-loader', LOADER)
        self.written(loader=True)
        before = self.request()
        self.refuse('all')
        for verb in ('confirm', 'cancel', 'status'):
            with self.subTest(verb):
                result = self.update(verb)
                self.assertEqual(result.returncode, 1)
                self.assertIn('durable storage could not be read (durable-open: durable is already open); nothing was decided',
                              result.stderr)
                self.assertNotIn('settled', result.stdout + result.stderr)
                self.assertNotIn('staged=', result.stdout, 'not "nothing is staged": no answer')
        self.assertEqual(self.request(), before)

    def test_a_confirmation_is_of_the_request_that_was_read_or_of_none(self):
        """It was two reads, the root's and the loader's. With the first refused and the second
        not, the loader alone was confirmed and the root's request went with it, never compared
        with what runs."""
        self.stage()
        self.stage('stage-loader', LOADER)
        self.written(root=False, loader=True)       # the loader came up; the root is not the staged one
        for refused in ((), (1,), (2,), (1, 2)):
            with self.subTest(refused=refused):
                self.refuse(*refused)
                result = self.update('confirm')
                self.assertEqual(result.returncode, 1)
                self.assertIn(f'the staged root is {digest(NEW_ROOT)}, and {digest(OLD_ROOT)} runs', result.stderr)
                self.assertIn('update_new=', self.request())


class Confirming(Unit):
    def test_confirmed_what_was_staged_is_forgotten(self):
        self.stage()
        self.written()
        self.assertEqual(self.update('confirm').returncode, 0)
        self.assertFalse(self.staged.exists())
        self.assertEqual(self.update('confirm').returncode, 1, 'and there is nothing more to confirm')

    def test_a_root_that_does_not_run_or_a_loader_that_did_not_start_is_not_confirmed(self):
        self.stage()
        self.assertIn('runs', self.update('confirm').stderr)
        self.written()
        self.stage('stage-loader', LOADER)
        self.assertIn('did not start this boot', self.update('confirm').stderr)
        self.assertIsNotNone(self.request())

    def test_the_power_going_while_it_forgets_leaves_no_request(self):
        """The loader acts on the request. It goes first and is written out before the images
        do: removed in whatever order the directory gave them, a cut could leave the request
        with a root already confirmed, and the next boot put the old root back."""
        self.stage()
        self.stage('stage-loader', LOADER)
        self.written(loader=True)
        (self.dir / 'cut').touch()
        self.assertNotEqual(self.update('confirm').returncode, 0)
        (self.dir / 'cut').unlink()
        self.assertIsNone(self.request(), 'whatever else is left')
        self.assertTrue((self.staged / 'rootfs.old').exists(), 'the cut came before the rest was removed')
        state = self.state()
        self.assertEqual((state['staged'], state['loader_staged']), ('', ''))
        self.assertEqual(self.stage(data=NEW_ROOT).stdout.strip(), 'system-update: this root already runs')


class Cancelling(Unit):
    def test_what_is_staged_and_not_written_is_forgotten(self):
        self.stage()
        self.stage('stage-loader', LOADER)
        result = self.update('cancel')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.staged.exists())

    def test_a_root_the_loader_has_written_is_not_kept_by_cancelling(self):
        self.stage()
        self.written()
        result = self.update('cancel')
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'not cancelled: the staged root {digest(NEW_ROOT)} is written and runs, unconfirmed. Restart the unit '
                      'and the previous root is put back, or confirm it', result.stderr)
        self.assertIsNotNone(self.request(), 'the next boot still finds it unconfirmed')

    def test_nor_a_loader(self):
        self.stage('stage-loader', LOADER)
        self.written(root=False, loader=True)
        result = self.update('cancel')
        self.assertEqual(result.returncode, 1)
        self.assertIn('is written to loader-b, unconfirmed. Restart the unit and the first loader removes it, or confirm it',
                      result.stderr)
        self.assertIsNotNone(self.request())

    def test_one_the_loader_put_back_is_cancelled_as_before(self):
        self.stage()
        (self.dir / 'notes/update_tried').write_text(digest(NEW_ROOT))
        (self.dir / 'notes/update_failed').write_text(digest(NEW_ROOT))    # written, did not read back, undone
        self.assertEqual(self.state()['failed'], digest(NEW_ROOT))
        self.assertEqual(self.update('cancel').returncode, 0)
        self.assertFalse(self.staged.exists())

    def test_nor_is_another_staged_over_it(self):
        """The root that runs unconfirmed would become the one to go back to."""
        newer = b'hsqs a root newer still'
        self.stage()
        self.written()
        before = self.request()
        result = self.stage(data=newer)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'not staged: the staged root {digest(NEW_ROOT)} is written and runs, unconfirmed', result.stderr)
        self.assertEqual(self.request(), before)
        self.assertEqual(self.stage('stage-loader', LOADER).returncode, 0, 'a loader beside it is another matter')
        self.written(root=False, loader=True)
        self.assertIn('written to loader-b, unconfirmed', self.stage('stage-loader', LOADER + b'2').stderr)
        self.assertEqual(self.update('confirm').returncode, 0)
        self.assertEqual(self.stage(data=newer).returncode, 0, 'confirmed, the next is staged')

    def test_staging_reads_what_is_staged_and_does_not_go_on_without(self):
        self.stage()
        self.written()
        self.refuse('all')
        result = self.stage(data=b'hsqs another')
        self.assertEqual(result.returncode, 1)
        self.assertIn('durable storage could not be read', result.stderr)

    def test_with_nothing_staged_there_is_nothing_to_cancel_and_that_is_fine(self):
        result = self.update('cancel')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'system-update: nothing is staged'))


if __name__ == '__main__':
    unittest.main()
