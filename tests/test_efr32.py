# SPDX-License-Identifier: MIT
"""The low-power radio's reset (kernel/w1700k_sdr/efr32.c, w1700k-efr32-line on the unit) and the
owner's command over it (w1700k-efr32, with the switch that lasts). The line tool builds as the
unit's programs are built and refuses what it is not asked plainly; the command is run as the unit
runs it, with a directory for durable storage and a stand-in for the line; and the whole way round
goes through the loader's own scripts (tests/test_loader_update.py). What the line does to the
chip is seen on a unit, not here."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_loader_update

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'kernel/w1700k_sdr/efr32.c'
COMMAND = ROOT / 'platform/rootfs/overlay/usr/sbin/w1700k-efr32'
CC = os.environ.get('CC', 'cc')


@unittest.skipUnless(shutil.which(CC) and Path('/usr/include/linux/gpio.h').exists(), 'no C compiler or kernel headers')
class Line(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        cls.program = Path(cls.built.name) / 'w1700k-efr32-line'
        result = subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', str(SOURCE), '-o', str(cls.program)],
                                capture_output=True, text=True)
        if result.returncode:
            cls.built.cleanup()
            raise AssertionError('efr32.c does not compile:\n' + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.built.cleanup()

    def line(self, *words):
        return subprocess.run([str(self.program), *words], capture_output=True, text=True)

    def built_for(self, board):
        """The program built to read the board's name from a file, as the loader hands it over in
        the device tree: the name and its NUL, or no file at all (None)."""
        handed = Path(self.built.name) / 'board'
        handed.unlink(missing_ok=True)
        if board is not None:
            handed.write_bytes(board)
        program = Path(self.built.name) / 'w1700k-efr32-line-board'
        subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', f'-DBOARD_NAME="{handed}"',
                        str(SOURCE), '-o', str(program)], check=True, capture_output=True)
        return program

    def test_only_a_w1700k_has_its_line_driven_or_read(self):
        """The XR1710G runs the same root and has no such chip, and a unit the loader could not
        name may be one: whatever is asked, the chip is not even opened."""
        for board, shown in ((b'xr1710g\0', 'xr1710g'), (b'unknown\0', 'unknown'), (b'w1700k2\0', 'w1700k2'), (b'\0', 'unnamed')):
            program = self.built_for(board)
            for verb in ('off', 'on', 'status'):
                with self.subTest(board=board, verb=verb):
                    result = subprocess.run([str(program), verb, '--chip', '/nonexistent/gpiochip0'],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f'this board ({shown}) is not known to carry the low-power radio; nothing done',
                                  result.stderr)
                    self.assertNotIn('/nonexistent', result.stderr)
                    self.assertEqual(result.stdout, '')

    def test_a_w1700k_and_a_unit_whose_loader_names_no_board_are_driven_as_asked(self):
        """No name at all is a loader from before boards were told apart, which ran on the W1700K."""
        for board in (b'w1700k\0', None):
            with self.subTest(board=board):
                result = subprocess.run([str(self.built_for(board)), 'status', '--chip', '/nonexistent/gpiochip0'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('/nonexistent/gpiochip0', result.stderr)

    def test_the_program_the_loader_and_the_table_agree_on_which_boards_have_it(self):
        sys.path.insert(0, str(ROOT / 'platform'))
        import boards
        have = sorted(board.key for board in boards.ALL if board.efr32_reset_line is not None)
        named = re.search(r'with_radio\[\] = \{([^}]*)\}', SOURCE.read_text()).group(1)
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', named)), have)
        lines = {board.efr32_reset_line for board in boards.ALL if board.efr32_reset_line is not None}
        self.assertEqual(lines, {24})
        self.assertIn('long line = 24;', SOURCE.read_text())

    def test_anything_but_off_on_and_status_is_usage(self):
        for words in ((), ('reset',), ('off', 'now'), ('on', '--line'), ('on', '--line', 'x'), ('on', '--line', '-1'),
                      ('on', '--line', '24x'), ('status', '--chip'), ('off', '--line', '99999'), ('on', '--keep')):
            with self.subTest(words=words):
                result = self.line(*words)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage: w1700k-efr32-line off | on | status', result.stderr)

    def test_what_is_not_a_gpio_chip_drives_nothing_and_says_so(self):
        for chip in ('/dev/null', '/nonexistent/gpiochip0', __file__):
            for verb in ('off', 'on', 'status'):
                with self.subTest(chip=chip, verb=verb):
                    result = self.line(verb, '--chip', chip)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('w1700k-efr32-line:', result.stderr)
                    self.assertEqual(result.stdout, '')

    def test_on_lets_the_line_go_and_never_drives_it_high(self):
        """The chip ends its own reset through its pull-up; a line of ours driven high would fight
        whatever else may one day hold that reset (a debug probe on the board's header)."""
        text = SOURCE.read_text()
        self.assertIn('GPIO_V2_LINE_FLAG_INPUT', text)
        self.assertEqual(text.count('GPIO_V2_LINE_FLAG_OUTPUT'), 1)
        self.assertIn('attr.values = 0;', text)
        self.assertNotIn('--line 25', text)                # the bootloader line is nobody's to touch here
        self.assertIn('long line = 24;', text)

    def test_it_is_built_for_the_unit_and_put_in_its_root(self):
        self.assertIn('efr32:w1700k-efr32-line', (ROOT / 'platform/linux/Makefile').read_text())
        self.assertIn("'w1700k-efr32-line'", (ROOT / 'platform/rootfs/build.py').read_text())
        self.assertTrue(os.access(COMMAND, os.X_OK))

    def test_start_up_leaves_it_to_the_loader_and_nothing_opens_its_port(self):
        """Held is the loader's doing, at every boot, so that every root has it held. No start-up
        script drives the line, and the device tree as built leaves the radio's port disabled."""
        overlay = ROOT / 'platform/rootfs/overlay'
        for name in ('etc/init.d/rcS', 'etc/inittab'):
            self.assertNotIn('efr32', (overlay / name).read_text())
        self.assertIn('bootcmd=run gps_off; run board_id; run efr32_off; ', (ROOT / 'platform/loader/an7581_w1700k_sdr_env').read_text())
        tree = (ROOT / 'platform/linux/dts/an7581-w1700k-sdr.dts').read_text()
        self.assertNotIn('uart2', tree)
        self.assertIn('Auxiliary UARTs stay disabled', tree)


class Command(unittest.TestCase):
    """w1700k-efr32 as the unit runs it. The stand-in for the line tool keeps the line's level in
    a file and notes every call."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / 'durable').mkdir()
        (self.dir / 'level').write_text('off\n')
        self.cmdline('w1700k.efr32=off')
        line = self.dir / 'line'
        line.write_text(f'#!/bin/sh\necho "$*" >> {self.dir}/calls\n[ ! -e {self.dir}/line-fails ] || {{ echo "w1700k-efr32-line: no line" >&2; exit 1; }}\n'
                        f'[ "$1" = status ] || echo "$1" > {self.dir}/level\necho "efr32=$(cat {self.dir}/level)"\n')
        run = self.dir / 'durable-run'
        run.write_text(f'#!/bin/sh\n[ ! -e {self.dir}/durable-busy ] || {{ echo "durable-open: durable is already open" >&2; exit 1; }}\n'
                       'echo "durable-open: /durable mounted from mtd6"\n"$@"\nstatus=$?\n'
                       'echo "durable-close: /durable closed"\nexit $status\n')
        for script in (line, run):
            script.chmod(0o755)
        self.env = dict(os.environ, W1700K_EFR32_LINE=str(line), W1700K_EFR32_CMDLINE=str(self.dir / 'cmdline'),
                        W1700K_EFR32_RUN=str(self.dir / 'run'), W1700K_EFR32_DURABLE_RUN=str(run),
                        W1700K_EFR32_DIR=str(self.dir / 'durable/efr32'))

    def cmdline(self, *words):
        (self.dir / 'cmdline').write_text(' '.join(('console=ttyS0,115200 ro', 'w1700k.keyslot=empty') + words) + '\n')

    def efr32(self, *words):
        return subprocess.run(['sh', str(COMMAND), *words], capture_output=True, text=True, env=self.env)

    def said(self, *words):
        result = self.efr32(*words)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def calls(self):
        path = self.dir / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def switch(self):
        path = self.dir / 'durable/efr32/switch'
        return path.read_text() if path.exists() else None

    def test_status_says_what_it_is_now_and_what_start_up_does(self):
        self.assertEqual(self.said('status'), ['efr32=off', 'start=off'])
        self.assertEqual(self.calls(), ['status'], 'the line is read, not driven')
        self.cmdline('w1700k.efr32=on')
        (self.dir / 'level').write_text('on\n')
        self.assertEqual(self.said('status'), ['efr32=on', 'start=on'])
        self.assertIsNone(self.switch(), 'and durable storage is not opened to say so')

    def test_on_and_off_last_until_the_next_start(self):
        self.assertEqual(self.said('on'), ['efr32=on', 'start=off'])
        self.assertEqual(self.said('status'), ['efr32=on', 'start=off'])
        self.assertEqual(self.said('off'), ['efr32=off', 'start=off'])
        self.assertEqual(self.calls(), ['on', 'status', 'off'])
        self.assertIsNone(self.switch())

    def test_keep_writes_the_switch_the_loader_reads(self):
        self.assertEqual(self.said('on', '--keep'), ['efr32=on', 'start=on'])
        self.assertEqual(self.switch(), 'efr32_keep=on\n')
        self.assertEqual(self.said('status'), ['efr32=on', 'start=on'], 'this boot knows of it')
        self.assertEqual(self.said('off', '--keep'), ['efr32=off', 'start=off'])
        self.assertEqual(self.switch(), 'efr32_keep=off\n')
        self.assertEqual(sorted(p.name for p in (self.dir / 'durable/efr32').iterdir()), ['switch'], 'no part file left')

    def test_a_switch_that_cannot_be_written_is_said_and_start_up_stays(self):
        (self.dir / 'durable-busy').write_text('')
        result = self.efr32('on', '--keep')
        self.assertEqual(result.returncode, 1)
        self.assertIn('the switch could not be written: start-up stays as it was', result.stderr)
        self.assertIsNone(self.switch())
        (self.dir / 'durable-busy').unlink()
        self.assertEqual(self.said('status'), ['efr32=on', 'start=off'], 'on now, as asked; off at start-up, as before')

    def test_a_line_that_cannot_be_driven_keeps_nothing(self):
        (self.dir / 'line-fails').write_text('')
        result = self.efr32('on', '--keep')
        self.assertEqual(result.returncode, 1)
        self.assertIn('w1700k-efr32-line: no line', result.stderr)
        self.assertIsNone(self.switch())

    def test_a_loader_that_does_not_hold_it_is_said(self):
        """An older loader says nothing on the command line: the radio then runs from power-on,
        whatever the switch says, until the loader is updated."""
        self.cmdline()
        (self.dir / 'level').write_text('on\n')
        self.assertEqual(self.said('status'), ['efr32=on', 'start=unheld'])
        result = self.efr32('off', '--keep')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.split(), ['efr32=off', 'start=unheld'])
        self.assertIn("this unit's loader does not read the switch yet", result.stderr)
        self.assertEqual(self.switch(), 'efr32_keep=off\n', 'kept all the same, for the loader that will')

    def test_on_a_board_without_the_radio_nothing_is_done_or_kept(self):
        """There the line tool refuses, whatever it is asked: the command says what it said and
        writes no switch."""
        (self.dir / 'line').write_text('#!/bin/sh\necho "w1700k-efr32-line: this board (xr1710g) is not known to carry '
                                       'the low-power radio; nothing done" >&2\nexit 1\n')
        for words in (('status',), ('on',), ('off', '--keep'), ('on', '--keep')):
            with self.subTest(words=words):
                result = self.efr32(*words)
                self.assertEqual(result.returncode, 1)
                self.assertIn('(xr1710g) is not known to carry the low-power radio; nothing done', result.stderr)
                self.assertEqual(result.stdout, '')
        self.assertIsNone(self.switch())

    def test_anything_else_is_usage_and_touches_nothing(self):
        for words in ((), ('toggle',), ('on', '--forever'), ('on', '--keep', 'x'), ('status', '--keep'), ('keep',)):
            with self.subTest(words=words):
                result = self.efr32(*words)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage: w1700k-efr32 status | on [--keep] | off [--keep]', result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.switch())


class AcrossAReboot(test_loader_update.LowPowerRadio):
    """The whole way round: the command writes the switch, the loader reads it at the next boot,
    holds or lets go, and the command then says what the loader did."""

    def setUp(self):
        super().setUp()
        self.command = Command('test_status_says_what_it_is_now_and_what_start_up_does')
        self.command.setUp()
        self.addCleanup(self.command.doCleanups)
        self.command.env['W1700K_EFR32_DIR'] = str(self.model / 'durable/efr32')
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)

    def reboot(self):
        """One boot: the loader's words to the kernel, and the line as the loader left it."""
        words = self.said()
        (self.command.dir / 'cmdline').write_text(' '.join(words) + '\n')
        (self.command.dir / 'run').unlink(missing_ok=True)                 # /run does not outlive a boot
        (self.command.dir / 'level').write_text('off\n' if self.held() else 'on\n')
        return self.command.said('status')

    def test_off_until_kept_on_and_off_again_when_kept_off(self):
        self.assertEqual(self.reboot(), ['efr32=off', 'start=off'])
        self.assertEqual(self.command.said('on'), ['efr32=on', 'start=off'])
        self.assertEqual(self.reboot(), ['efr32=off', 'start=off'], 'on alone lasts until the next start')
        self.assertEqual(self.command.said('on', '--keep'), ['efr32=on', 'start=on'])
        self.assertEqual(self.reboot(), ['efr32=on', 'start=on'])
        self.assertEqual(self.reboot(), ['efr32=on', 'start=on'])
        self.assertEqual(self.command.said('off', '--keep'), ['efr32=off', 'start=off'])
        self.assertEqual(self.reboot(), ['efr32=off', 'start=off'])


del test_loader_update          # (its own cases are run from its own file, not again from here)

if __name__ == '__main__':
    unittest.main()
