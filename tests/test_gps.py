# SPDX-License-Identifier: MIT
"""The GPS receiver on the unit. Its power line (kernel/w1700k_sdr/gps.c, w1700k-gps-line): it
builds as the unit's programs are built and refuses what it is not asked plainly; what the line
does to the receiver is seen on a unit, not here. And the owner's command, w1700k-gps: the
receiver is off at every boot unless kept on, and its serial port is handed to a host only then
(the loader's part is in tests/test_loader_update.py, the link program's in tests/test_efr32.py)."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_loader_update      # noqa: E402

SOURCE = ROOT / 'kernel/w1700k_sdr/gps.c'
COMMAND = ROOT / 'platform/rootfs/overlay/usr/sbin/w1700k-gps'
CC = os.environ.get('CC', 'cc')


@unittest.skipUnless(shutil.which(CC) and Path('/usr/include/linux/gpio.h').exists(), 'no C compiler or kernel headers')
class Gps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        cls.program = Path(cls.built.name) / 'w1700k-gps-line'
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
                self.assertIn('usage: w1700k-gps-line off | on | status', result.stderr)

    def test_what_is_not_a_gpio_chip_drives_nothing_and_says_so(self):
        for chip in ('/dev/null', '/nonexistent/gpiochip0', __file__):
            for verb in ('off', 'on', 'status'):
                with self.subTest(chip=chip, verb=verb):
                    result = self.gps(verb, '--chip', chip)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('w1700k-gps-line:', result.stderr)
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
        self.assertIn('gps:w1700k-gps-line', (ROOT / 'platform/linux/Makefile').read_text())
        self.assertIn("'w1700k-gps-line'", (ROOT / 'platform/rootfs/build.py').read_text())
        self.assertTrue(os.access(COMMAND, os.X_OK))

    def test_start_up_leaves_it_to_the_loader_and_only_the_owners_command_names_its_port(self):
        """No start-up script switches the receiver on or reads it; the device tree as built leaves
        its port disabled, and the build refuses a tree in which it is not."""
        overlay = ROOT / 'platform/rootfs/overlay'
        for name in ('etc/init.d/rcS', 'etc/inittab'):
            self.assertNotIn('gps', (overlay / name).read_text())
        tree = (ROOT / 'platform/linux/dts/an7581-w1700k-sdr.dts').read_text()
        block = re.search(r'^&hsuart3 \{(.*?)^\};', tree, re.S | re.M).group(1)
        self.assertNotIn('status', block, 'as built the port keeps the status the SoC include gives it: disabled')
        self.assertIn('serial2 = &hsuart3;', tree, "which is the receiver's port wherever it is there")
        self.assertIn("'hsuart3'", re.search(r'^DISABLED = \((.*)\)', (ROOT / 'platform/linux/board_check.py').read_text(),
                                             re.M).group(1))
        named = [path.name for path in overlay.rglob('*') if path.is_file() and b'ttyS2' in path.read_bytes()]
        self.assertEqual(named, ['w1700k-gps'])
        self.assertIn('PORT=${W1700K_GPS_PORT:-/dev/ttyS2}', COMMAND.read_text())

    def test_the_port_is_named_as_the_high_speed_one_so_that_its_speed_is_the_one_asked_for(self):
        """Its divider has twice the clock of the other ports' (kernel patch 0015 goes by the
        compatible), and the clock-frequency is what lets 921600 be asked for."""
        tree = (ROOT / 'platform/linux/dts/an7581-w1700k-sdr.dts').read_text()
        block = re.search(r'^&hsuart3 \{(.*?)^\};', tree, re.S | re.M).group(1)
        self.assertIn('compatible = "airoha,an7581-hsuart", "airoha,en7523-uart";', block)
        self.assertIn('clock-frequency = <14745600>;', block)
        patch = (ROOT / 'platform/linux/patches/0015-serial-8250-airoha-the-speed-asked-for.patch').read_text()
        self.assertIn('device_is_compatible(port->dev, "airoha,an7581-hsuart")', patch)
        self.assertIn('+\t\tquot = 1;', patch)


class Command(unittest.TestCase):
    """w1700k-gps as the unit runs it. The stand-in for the line tool keeps the line's level in a
    file and notes every call; the stand-in for the link tool notes how it was called."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / 'durable').mkdir()
        (self.dir / 'level').write_text('off\n')
        self.cmdline('w1700k.gps=off')
        line = self.dir / 'line'
        line.write_text(f'#!/bin/sh\necho "$*" >> {self.dir}/calls\n[ ! -e {self.dir}/line-fails ] || {{ echo "w1700k-gps-line: no line" >&2; exit 1; }}\n'
                        f'[ "$1" = status ] || echo "$1" > {self.dir}/level\necho "gps=$(cat {self.dir}/level)"\n')
        link = self.dir / 'link'
        link.write_text(f'#!/bin/sh\necho "link $*" >> {self.dir}/calls\ncat\n')
        run = self.dir / 'durable-run'
        run.write_text(f'#!/bin/sh\n[ ! -e {self.dir}/durable-busy ] || {{ echo "durable-open: durable is already open" >&2; exit 1; }}\n'
                       'echo "durable-open: /durable mounted from mtd6"\n"$@"\nstatus=$?\n'
                       'echo "durable-close: /durable closed"\nexit $status\n')
        for script in (line, link, run):
            script.chmod(0o755)
        self.env = dict(os.environ, W1700K_GPS_LINE=str(line), W1700K_GPS_CMDLINE=str(self.dir / 'cmdline'),
                        W1700K_GPS_RUN=str(self.dir / 'run'), W1700K_GPS_DURABLE_RUN=str(run),
                        W1700K_GPS_DIR=str(self.dir / 'durable/gps'), W1700K_GPS_LINK=str(link),
                        W1700K_GPS_PORT_STATUS=str(self.dir / 'port-status'))

    def cmdline(self, *words):
        (self.dir / 'cmdline').write_text(' '.join(('console=ttyS0,115200 ro', 'w1700k.efr32=on') + words) + '\n')

    def port(self, status):
        """The port's status in the device tree this boot was handed (a string with its NUL), or none."""
        path = self.dir / 'port-status'
        path.unlink(missing_ok=True)
        if status is not None:
            path.write_bytes(status.encode() + b'\0')

    def gps(self, *words, stdin=''):
        return subprocess.run(['sh', str(COMMAND), *words], input=stdin, capture_output=True, text=True, env=self.env)

    def said(self, *words):
        result = self.gps(*words)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def calls(self):
        path = self.dir / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def switch(self):
        path = self.dir / 'durable/gps/switch'
        return path.read_text() if path.exists() else None

    def test_status_says_what_it_is_now_what_start_up_does_and_whether_the_port_is_there(self):
        self.port('disabled')
        self.assertEqual(self.said('status'), ['gps=off', 'start=off', 'port=off'])
        self.assertEqual(self.calls(), ['status'], 'the line is read, not driven')
        self.cmdline('w1700k.gps=on')
        (self.dir / 'level').write_text('on\n')
        self.port('okay')
        self.assertEqual(self.said('status'), ['gps=on', 'start=on', 'port=on'], "the radio's word is not the receiver's")
        self.port(None)
        self.assertEqual(self.said('status'), ['gps=on', 'start=on', 'port=off'])
        self.assertIsNone(self.switch(), 'and durable storage is not opened to say so')

    def test_on_and_off_last_until_the_next_start(self):
        self.assertEqual(self.said('on'), ['gps=on', 'start=off'])
        self.assertEqual(self.said('status'), ['gps=on', 'start=off', 'port=off'])
        self.assertEqual(self.said('off'), ['gps=off', 'start=off'])
        self.assertEqual(self.calls(), ['on', 'status', 'off'])
        self.assertIsNone(self.switch())

    def test_keep_writes_the_switch_the_loader_reads(self):
        self.assertEqual(self.said('on', '--keep'), ['gps=on', 'start=on'])
        self.assertEqual(self.switch(), 'gps_keep=on\n')
        self.assertEqual(self.said('status'), ['gps=on', 'start=on', 'port=off'],
                         'this boot knows of it; the port comes with the next one')
        self.assertEqual(self.said('off', '--keep'), ['gps=off', 'start=off'])
        self.assertEqual(self.switch(), 'gps_keep=off\n')
        self.assertEqual(sorted(p.name for p in (self.dir / 'durable/gps').iterdir()), ['switch'], 'no part file left')

    def test_a_switch_that_cannot_be_written_is_said_and_start_up_stays(self):
        (self.dir / 'durable-busy').write_text('')
        result = self.gps('on', '--keep')
        self.assertEqual(result.returncode, 1)
        self.assertIn('the switch could not be written: start-up stays as it was', result.stderr)
        self.assertIsNone(self.switch())
        (self.dir / 'durable-busy').unlink()
        self.assertEqual(self.said('status')[:2], ['gps=on', 'start=off'], 'on now, as asked; off at start-up, as before')

    def test_a_line_that_cannot_be_driven_keeps_nothing(self):
        (self.dir / 'line-fails').write_text('')
        result = self.gps('on', '--keep')
        self.assertEqual(result.returncode, 1)
        self.assertIn('w1700k-gps-line: no line', result.stderr)
        self.assertIsNone(self.switch())

    def test_a_loader_that_reads_no_switch_is_said(self):
        """An older loader says nothing of the receiver on the command line: it switches the
        receiver off at every boot, whatever is kept, until the loader is updated."""
        self.cmdline()
        self.assertEqual(self.said('status'), ['gps=off', 'start=off', 'port=off'])
        result = self.gps('on', '--keep')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.split(), ['gps=on', 'start=off'])
        self.assertIn("this unit's loader does not read the switch yet", result.stderr)
        self.assertEqual(self.switch(), 'gps_keep=on\n', 'kept all the same, for the loader that will')

    def test_on_a_board_without_a_receiver_nothing_is_done_or_kept(self):
        (self.dir / 'line').write_text('#!/bin/sh\necho "w1700k-gps-line: this board (xr1710g) has no GPS receiver; '
                                       'nothing done" >&2\nexit 1\n')
        self.port('okay')
        for words in (('status',), ('on',), ('off', '--keep'), ('on', '--keep'), ('link',)):
            with self.subTest(words=words):
                result = self.gps(*words)
                self.assertEqual(result.returncode, 1)
                self.assertIn('(xr1710g) has no GPS receiver; nothing done', result.stderr)
                self.assertEqual(result.stdout, '')
        self.assertIsNone(self.switch())

    def test_link_is_the_port_at_the_receivers_speed_when_it_runs_and_its_port_is_there(self):
        (self.dir / 'level').write_text('on\n')
        self.port('okay')
        result = self.gps('link', stdin='$PAIR to the receiver\n')
        self.assertEqual((result.returncode, result.stdout), (0, '$PAIR to the receiver\n'),
                         "standard input and output are the link tool's")
        self.assertEqual(self.gps('link', '--baud', '115200', stdin='').returncode, 0)
        self.assertEqual(self.calls(), ['status', 'link --port /dev/ttyS2 --baud 921600',
                                        'status', 'link --port /dev/ttyS2 --baud 115200'], 'the line is read, never driven')
        for baud in ('1000000', '9600', 'fast'):
            with self.subTest(baud=baud):
                result = self.gps('link', '--baud', baud)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{baud} baud is not one the receiver's port reaches (115200, 230400, 460800, 921600)",
                              result.stderr)
        self.assertEqual(len([call for call in self.calls() if call.startswith('link')]), 2)

    def test_link_says_why_there_is_nothing_to_hear(self):
        self.port('okay')
        result = self.gps('link')
        self.assertEqual(result.returncode, 1)
        self.assertIn('the receiver is switched off', result.stderr)
        (self.dir / 'level').write_text('on\n')
        for status in ('disabled', None):
            self.port(status)
            result = self.gps('link')
            self.assertEqual(result.returncode, 1)
            self.assertIn("the receiver's port is not there: the loader enables it where the receiver is kept on", result.stderr)
        self.assertNotIn('link', ' '.join(self.calls()), 'the link tool was not started')

    def test_anything_else_is_usage_and_touches_nothing(self):
        for words in ((), ('toggle',), ('on', '--forever'), ('on', '--keep', 'x'), ('status', '--keep'), ('keep',),
                      ('link', '--port', '/dev/ttyS0'), ('link', '--baud'), ('link', '--baud', '921600', 'x')):
            with self.subTest(words=words):
                result = self.gps(*words)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage: w1700k-gps status | on [--keep] | off [--keep] | link [--baud N]', result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.switch())

    def test_nothing_on_the_unit_reads_or_keeps_what_the_receiver_says(self):
        """Where the unit is, is its owner's to know: the command hands the port over and that is
        all. No sentence is parsed, no file is written but the switch and this boot's note of it."""
        code = '\n'.join(line for line in COMMAND.read_text().splitlines() if not line.lstrip().startswith('#'))
        for word in ('GGA', 'RMC', 'NMEA', 'tee ', '/tmp', 'logger', 'syslog'):
            self.assertNotIn(word, code)
        self.assertEqual(re.findall(r'> ?"?\$?\{?([A-Za-z0-9_./$]+)', code).count('/dev/null'), 0)


class AcrossAReboot(test_loader_update.GpsSwitch):
    """The whole way round: the command writes the switch, the loader reads it at the next boot,
    switches the receiver on or leaves it off, enables the port or does not, and the command then
    says what the loader did."""

    def setUp(self):
        super().setUp()
        self.command = Command('test_on_and_off_last_until_the_next_start')
        self.command.setUp()
        self.addCleanup(self.command.doCleanups)
        self.command.env['W1700K_GPS_DIR'] = str(self.model / 'durable/gps')

    def reboot(self):
        said, high, port = self.gps()
        (self.command.dir / 'cmdline').write_text(' '.join(said) + '\n')
        (self.command.dir / 'run').unlink(missing_ok=True)                 # /run does not outlive a boot
        (self.command.dir / 'level').write_text('on\n' if high else 'off\n')
        self.command.port('okay' if port else 'disabled')
        return self.command.said('status')

    def test_off_until_kept_on_and_off_again_when_kept_off(self):
        self.assertEqual(self.reboot(), ['gps=off', 'start=off', 'port=off'])
        self.assertEqual(self.command.said('on'), ['gps=on', 'start=off'])
        self.assertEqual(self.reboot(), ['gps=off', 'start=off', 'port=off'], 'on alone lasts until the next start')
        self.assertEqual(self.command.said('on', '--keep'), ['gps=on', 'start=on'])
        self.assertEqual(self.command.said('status'), ['gps=on', 'start=on', 'port=off'], 'no port before the restart')
        self.assertEqual(self.reboot(), ['gps=on', 'start=on', 'port=on'])
        self.assertEqual(self.command.gps('link', stdin='x').stdout, 'x', 'and now there is something to hear')
        self.assertEqual(self.reboot(), ['gps=on', 'start=on', 'port=on'])
        self.assertEqual(self.command.said('off', '--keep'), ['gps=off', 'start=off'])
        self.assertEqual(self.reboot(), ['gps=off', 'start=off', 'port=off'])


del test_loader_update          # (its own cases are run from its own file, not again from here)

if __name__ == '__main__':
    unittest.main()
