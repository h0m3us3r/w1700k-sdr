# SPDX-License-Identifier: MIT
"""The GPS receiver's power line (kernel/w1700k_sdr/gps.c, w1700k-gps on the unit): it builds as
the unit's programs are built and refuses what it is not asked plainly. What the line does to the
receiver is seen on a unit, not here; the loader is what switches it off at every boot."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'kernel/w1700k_sdr/gps.c'
CC = os.environ.get('CC', 'cc')


@unittest.skipUnless(shutil.which(CC) and Path('/usr/include/linux/gpio.h').exists(), 'no C compiler or kernel headers')
class Gps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        cls.program = Path(cls.built.name) / 'w1700k-gps'
        result = subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', str(SOURCE), '-o', str(cls.program)],
                                capture_output=True, text=True)
        if result.returncode:
            cls.built.cleanup()
            raise AssertionError('gps.c does not compile:\n' + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.built.cleanup()

    def gps(self, *words):
        return subprocess.run([str(self.program), *words], capture_output=True, text=True)

    def built_for(self, board):
        """The program built to read the board's name from a file, as the loader hands it over in
        the device tree: the name and its NUL, or no file at all (None)."""
        handed = Path(self.built.name) / 'board'
        handed.unlink(missing_ok=True)
        if board is not None:
            handed.write_bytes(board)
        program = Path(self.built.name) / 'w1700k-gps-board'
        subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', f'-DBOARD_NAME="{handed}"',
                        str(SOURCE), '-o', str(program)], check=True, capture_output=True)
        return program

    def test_a_board_without_a_receiver_has_its_line_left_alone(self):
        """The XR1710G runs the same root, and line 43 is a port LED there: whatever is asked, the
        chip is not even opened (a chip that does not exist is not what it complains of)."""
        program = self.built_for(b'xr1710g\0')
        for verb in ('off', 'on', 'status'):
            with self.subTest(verb=verb):
                result = subprocess.run([str(program), verb, '--chip', '/nonexistent/gpiochip0'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('(xr1710g) has no GPS receiver', result.stderr)
                self.assertNotIn('/nonexistent', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_the_w1700k_and_a_board_nobody_named_are_driven_as_asked(self):
        """No name at all is a loader from before boards were told apart, which ran on the W1700K."""
        for board in (b'w1700k\0', b'unknown\0', b'', b'xr1710g2\0', None):
            with self.subTest(board=board):
                result = subprocess.run([str(self.built_for(board)), 'status', '--chip', '/nonexistent/gpiochip0'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('/nonexistent/gpiochip0', result.stderr)

    def test_anything_but_off_on_and_status_is_usage(self):
        for words in ((), ('toggle',), ('off', 'now'), ('on', '--line'), ('on', '--line', 'x'), ('on', '--line', '-1'),
                      ('on', '--line', '43x'), ('status', '--chip'), ('off', '--line', '99999')):
            with self.subTest(words=words):
                result = self.gps(*words)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage: w1700k-gps off | on | status', result.stderr)

    def test_what_is_not_a_gpio_chip_drives_nothing_and_says_so(self):
        for chip in ('/dev/null', '/nonexistent/gpiochip0', __file__):
            for verb in ('off', 'on', 'status'):
                with self.subTest(chip=chip, verb=verb):
                    result = self.gps(verb, '--chip', chip)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('w1700k-gps:', result.stderr)
                    self.assertEqual(result.stdout, '')

    def test_the_loader_switches_the_receiver_off_and_start_up_leaves_the_line_alone(self):
        """Off is the loader's doing, at every boot (tests/test_loader_update.py), so that every
        root has it. A root that also drove the line would undo an owner's `on` kept some other way."""
        self.assertNotIn('w1700k-gps', (ROOT / 'platform/rootfs/overlay/etc/init.d/rcS').read_text())
        self.assertIn('bootcmd=run gps_off; ', (ROOT / 'platform/loader/an7581_w1700k_sdr_env').read_text())

    def test_the_program_and_the_loader_agree_on_which_boards_have_no_receiver(self):
        sys.path.insert(0, str(ROOT / 'platform'))
        import boards
        without = sorted(board.key for board in boards.ALL if board.gps_line is None)
        named = re.search(r'no_receiver\[\] = \{([^}]*)\}', SOURCE.read_text()).group(1)
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', named)), without)

    def test_it_is_built_for_the_unit_and_put_in_its_root(self):
        self.assertIn('gps:w1700k-gps', (ROOT / 'platform/linux/Makefile').read_text())
        self.assertIn("'w1700k-gps'", (ROOT / 'platform/rootfs/build.py').read_text())

    def test_nothing_in_the_root_opens_the_receivers_port(self):
        """The device tree as built leaves the port disabled, and no program of ours names it."""
        overlay = ROOT / 'platform/rootfs/overlay'
        named = [path for path in overlay.rglob('*') if path.is_file() and b'ttyS1' in path.read_bytes()]
        self.assertEqual(named, [])


if __name__ == '__main__':
    unittest.main()
