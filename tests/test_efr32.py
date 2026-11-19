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
        block = re.search(r'^&uart2 \{(.*?)^\};', tree, re.S | re.M).group(1)
        self.assertNotIn('status', block, 'as built the port keeps the status the SoC include gives it: disabled')
        self.assertIn("DISABLED = ('pcie1', 'npu', 'uart2', ", (ROOT / 'platform/linux/board_check.py').read_text(),
                      'and the build refuses a tree in which it is not')
        named = [path.name for path in overlay.rglob('*') if path.is_file() and b'ttyS1' in path.read_bytes()]
        self.assertEqual(named, ['w1700k-efr32'], 'the owner\'s command is all that names the port')


LINK_SOURCE = ROOT / 'kernel/w1700k_sdr/port_link.c'


@unittest.skipUnless(shutil.which(CC) and hasattr(os, 'openpty'), 'no C compiler or no pseudo-terminals')
class LinkTool(unittest.TestCase):
    """w1700k-port-link, which hands a serial port of the unit to a host (the radio's here; the
    GPS receiver's the same way, tests/test_gps.py), with a pseudo-terminal for the port."""

    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        cls.program = Path(cls.built.name) / 'w1700k-port-link'
        result = subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', str(LINK_SOURCE), '-o', str(cls.program)],
                                capture_output=True, text=True)
        if result.returncode:
            cls.built.cleanup()
            raise AssertionError('port_link.c does not compile:\n' + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.built.cleanup()

    def setUp(self):
        self.radio, port = os.openpty()                    # the radio's end, and the port the tool opens
        self.port = os.ttyname(port)
        self.addCleanup(os.close, self.radio)
        self.addCleanup(os.close, port)

    def start(self, *words):
        process = subprocess.Popen([str(self.program), '--port', self.port, *words], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(process.kill)
        return process

    def from_fd(self, fd, count, timeout=5.0):
        import select
        got = b''
        while len(got) < count and select.select([fd], [], [], timeout)[0]:
            got += os.read(fd, count - len(got))
        return got

    def test_bytes_go_both_ways_unchanged(self):
        """Every value a byte can have, the ones a terminal would act on among them."""
        every = bytes(range(256)) * 3
        process = self.start()
        process.stdin.write(every)
        self.assertEqual(self.from_fd(self.radio, len(every)), every, 'host to radio')
        os.write(self.radio, every[::-1])
        self.assertEqual(self.from_fd(process.stdout.fileno(), len(every)), every[::-1], 'radio to host')
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0, 'standard input ending ends the link')
        self.assertEqual(process.stderr.read(), b'')

    def test_what_the_radio_says_last_is_still_passed_on(self):
        process = self.start()
        process.stdin.write(b'\x20\x00\x01\x00')
        self.assertEqual(self.from_fd(self.radio, 4), b'\x20\x00\x01\x00')
        process.stdin.close()
        os.write(self.radio, b'\x20\x02\x01\x00\x00\x00')
        self.assertEqual(self.from_fd(process.stdout.fileno(), 6), b'\x20\x02\x01\x00\x00\x00')
        self.assertEqual(process.wait(timeout=5), 0)

    def test_the_port_is_set_to_the_speed_wanted_raw_and_without_flow_control(self):
        """The speed asked for is the speed on the wire (kernel patch 0015; before it the radio's
        port gave half of 230400, and the tool asked for twice)."""
        import termios
        for baud, asked in (('115200', termios.B115200), ('230400', termios.B230400),
                            ('460800', termios.B460800), ('921600', termios.B921600)):
            with self.subTest(baud=baud):
                process = self.start('--baud', baud)
                process.stdin.write(b'x')
                self.assertEqual(self.from_fd(self.radio, 1), b'x')
                attrs = termios.tcgetattr(self.radio)
                self.assertEqual((attrs[4], attrs[5]), (asked, asked))
                self.assertFalse(attrs[3] & (termios.ICANON | termios.ECHO | termios.ISIG))
                self.assertFalse(attrs[2] & (termios.CRTSCTS | termios.PARENB | termios.CSTOPB))
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0)

    def test_a_speed_the_port_does_not_reach_and_anything_else_is_refused(self):
        for words, code, said in ((('--baud', '1000000'), 2, 'is not a speed of these ports (115200, 230400, 460800, 921600)'),
                                  (('--baud', '9600'), 2, 'is not a speed of these ports'),
                                  (('--baud', 'fast'), 2, 'usage: w1700k-port-link'),
                                  (('--baud',), 2, 'usage: w1700k-port-link'),
                                  (('now',), 2, 'usage: w1700k-port-link')):
            with self.subTest(words=words):
                result = subprocess.run([str(self.program), '--port', self.port, *words], stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, code)
                self.assertIn(said, result.stderr)
        result = subprocess.run([str(self.program), '--port', '/nonexistent/ttyS1'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('/nonexistent/ttyS1', result.stderr)
        result = subprocess.run([str(self.program), '--baud', '115200'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, 'it has no port of its own: the command that calls it names one')
        self.assertIn('usage: w1700k-port-link', result.stderr)

    def test_it_is_built_for_the_unit_and_knows_nothing_of_what_is_on_a_port(self):
        self.assertIn('port_link:w1700k-port-link', (ROOT / 'platform/linux/Makefile').read_text())
        self.assertIn("'w1700k-port-link'", (ROOT / 'platform/rootfs/build.py').read_text())
        text = LINK_SOURCE.read_text()
        self.assertNotIn('/dev/ttyS', text.split('*/', 1)[1])
        self.assertIn('PORT=${W1700K_EFR32_PORT:-/dev/ttyS1}', COMMAND.read_text())
        self.assertIn('serial1 = &uart2;', (ROOT / 'platform/linux/dts/an7581-w1700k-sdr.dts').read_text(),
                      'which is the radio\'s port wherever it is there')
        for word in ('0x20', 'hello', 'bgapi', 'BGAPI', 'NMEA', 'nmea', 'GGA'):
            self.assertNotIn(word, text.split('*/', 1)[1])


class Command(unittest.TestCase):
    """w1700k-efr32 as the unit runs it. The stand-in for the line tool keeps the line's level in
    a file and notes every call; the stand-in for the link tool notes how it was called."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / 'durable').mkdir()
        (self.dir / 'level').write_text('off\n')
        self.cmdline('w1700k.efr32=off')
        line = self.dir / 'line'
        line.write_text(f'#!/bin/sh\necho "$*" >> {self.dir}/calls\n[ ! -e {self.dir}/line-fails ] || {{ echo "w1700k-efr32-line: no line" >&2; exit 1; }}\n'
                        f'[ "$1" = status ] || echo "$1" > {self.dir}/level\necho "efr32=$(cat {self.dir}/level)"\n')
        link = self.dir / 'link'
        link.write_text(f'#!/bin/sh\necho "link $*" >> {self.dir}/calls\ncat\n')
        run = self.dir / 'durable-run'
        run.write_text(f'#!/bin/sh\n[ ! -e {self.dir}/durable-busy ] || {{ echo "durable-open: durable is already open" >&2; exit 1; }}\n'
                       'echo "durable-open: /durable mounted from mtd6"\n"$@"\nstatus=$?\n'
                       'echo "durable-close: /durable closed"\nexit $status\n')
        for script in (line, link, run):
            script.chmod(0o755)
        self.env = dict(os.environ, W1700K_EFR32_LINE=str(line), W1700K_EFR32_CMDLINE=str(self.dir / 'cmdline'),
                        W1700K_EFR32_RUN=str(self.dir / 'run'), W1700K_EFR32_DURABLE_RUN=str(run),
                        W1700K_EFR32_DIR=str(self.dir / 'durable/efr32'), W1700K_EFR32_LINK=str(link),
                        W1700K_EFR32_PORT_STATUS=str(self.dir / 'port-status'))

    def cmdline(self, *words):
        (self.dir / 'cmdline').write_text(' '.join(('console=ttyS0,115200 ro', 'w1700k.keyslot=empty') + words) + '\n')

    def port(self, status):
        """The port's status in the device tree this boot was handed (a string with its NUL), or none."""
        path = self.dir / 'port-status'
        path.unlink(missing_ok=True)
        if status is not None:
            path.write_bytes(status.encode() + b'\0')

    def efr32(self, *words, stdin=''):
        return subprocess.run(['sh', str(COMMAND), *words], input=stdin, capture_output=True, text=True, env=self.env)

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

    def test_status_says_what_it_is_now_what_start_up_does_and_whether_the_port_is_there(self):
        self.port('disabled')
        self.assertEqual(self.said('status'), ['efr32=off', 'start=off', 'port=off'])
        self.assertEqual(self.calls(), ['status'], 'the line is read, not driven')
        self.cmdline('w1700k.efr32=on')
        (self.dir / 'level').write_text('on\n')
        self.port('okay')
        self.assertEqual(self.said('status'), ['efr32=on', 'start=on', 'port=on'])
        self.port(None)                                    # a tree with no such node, or no status
        self.assertEqual(self.said('status'), ['efr32=on', 'start=on', 'port=off'])
        self.assertIsNone(self.switch(), 'and durable storage is not opened to say so')

    def test_on_and_off_last_until_the_next_start(self):
        self.assertEqual(self.said('on'), ['efr32=on', 'start=off'])
        self.assertEqual(self.said('status'), ['efr32=on', 'start=off', 'port=off'])
        self.assertEqual(self.said('off'), ['efr32=off', 'start=off'])
        self.assertEqual(self.calls(), ['on', 'status', 'off'])
        self.assertIsNone(self.switch())

    def test_keep_writes_the_switch_the_loader_reads(self):
        self.assertEqual(self.said('on', '--keep'), ['efr32=on', 'start=on'])
        self.assertEqual(self.switch(), 'efr32_keep=on\n')
        self.assertEqual(self.said('status'), ['efr32=on', 'start=on', 'port=off'],
                         'this boot knows of it; the port comes with the next one')
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
        self.assertEqual(self.said('status')[:2], ['efr32=on', 'start=off'], 'on now, as asked; off at start-up, as before')

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
        self.assertEqual(self.said('status'), ['efr32=on', 'start=unheld', 'port=off'])
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
        self.port('okay')
        for words in (('status',), ('on',), ('off', '--keep'), ('on', '--keep'), ('link',)):
            with self.subTest(words=words):
                result = self.efr32(*words)
                self.assertEqual(result.returncode, 1)
                self.assertIn('(xr1710g) is not known to carry the low-power radio; nothing done', result.stderr)
                self.assertEqual(result.stdout, '')
        self.assertIsNone(self.switch())

    def test_link_is_the_port_when_the_radio_runs_and_its_port_is_there(self):
        (self.dir / 'level').write_text('on\n')
        self.port('okay')
        result = self.efr32('link', stdin='to the radio\n')
        self.assertEqual((result.returncode, result.stdout), (0, 'to the radio\n'), 'standard input and output are the link tool\'s')
        result = self.efr32('link', '--baud', '230400', stdin='')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ['status', 'link --port /dev/ttyS1 --baud 115200',
                                        'status', 'link --port /dev/ttyS1 --baud 230400'], 'the line is read, never driven')

    def test_link_takes_only_a_speed_the_port_reaches(self):
        (self.dir / 'level').write_text('on\n')
        self.port('okay')
        self.assertEqual(self.efr32('link', '--baud', '460800', stdin='').returncode, 0)
        for baud in ('921600', '9600', 'fast'):
            with self.subTest(baud=baud):
                result = self.efr32('link', '--baud', baud)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{baud} baud is not one the radio's port reaches (115200, 230400, 460800)", result.stderr)
        self.assertEqual([call for call in self.calls() if call.startswith('link')], ['link --port /dev/ttyS1 --baud 460800'])

    def test_link_says_why_there_is_nobody_to_talk_to(self):
        self.port('okay')
        result = self.efr32('link')
        self.assertEqual(result.returncode, 1)
        self.assertIn('the radio is held in reset', result.stderr)
        (self.dir / 'level').write_text('on\n')
        for status in ('disabled', None):
            self.port(status)
            result = self.efr32('link')
            self.assertEqual(result.returncode, 1)
            self.assertIn("the radio's port is not there: the loader enables it where the radio is kept on", result.stderr)
        self.assertNotIn('link', ' '.join(self.calls()), 'the link tool was not started')

    def test_anything_else_is_usage_and_touches_nothing(self):
        for words in ((), ('toggle',), ('on', '--forever'), ('on', '--keep', 'x'), ('status', '--keep'), ('keep',),
                      ('link', '--port', '/dev/ttyS0'), ('link', '--baud'), ('link', '--baud', '115200', 'x')):
            with self.subTest(words=words):
                result = self.efr32(*words)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage: w1700k-efr32 status | on [--keep] | off [--keep] | link [--baud N]', result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.switch())


class AcrossAReboot(test_loader_update.LowPowerRadio):
    """The whole way round: the command writes the switch, the loader reads it at the next boot,
    holds or lets go, enables the port or does not, and the command then says what the loader did."""

    def setUp(self):
        super().setUp()
        self.command = Command('test_on_and_off_last_until_the_next_start')
        self.command.setUp()
        self.addCleanup(self.command.doCleanups)
        self.command.env['W1700K_EFR32_DIR'] = str(self.model / 'durable/efr32')
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)

    def reboot(self):
        """One boot: the loader's words to the kernel, the line as the loader left it, and the
        port's status in the tree it handed over (disabled as built)."""
        words = self.said()
        (self.command.dir / 'cmdline').write_text(' '.join(words) + '\n')
        (self.command.dir / 'run').unlink(missing_ok=True)                 # /run does not outlive a boot
        (self.command.dir / 'level').write_text('off\n' if self.held() else 'on\n')
        self.command.port('okay' if self.port() == ['/soc/serial@1fbf0300 status=okay'] else 'disabled')
        return self.command.said('status')

    def test_off_until_kept_on_and_off_again_when_kept_off(self):
        self.assertEqual(self.reboot(), ['efr32=off', 'start=off', 'port=off'])
        self.assertEqual(self.command.said('on'), ['efr32=on', 'start=off'])
        self.assertEqual(self.reboot(), ['efr32=off', 'start=off', 'port=off'], 'on alone lasts until the next start')
        self.assertEqual(self.command.said('on', '--keep'), ['efr32=on', 'start=on'])
        self.assertEqual(self.command.said('status'), ['efr32=on', 'start=on', 'port=off'], 'no port before the restart')
        self.assertEqual(self.reboot(), ['efr32=on', 'start=on', 'port=on'])
        self.assertEqual(self.command.efr32('link', stdin='x').stdout, 'x', 'and now there is someone to talk to')
        self.assertEqual(self.reboot(), ['efr32=on', 'start=on', 'port=on'])
        self.assertEqual(self.command.said('off', '--keep'), ['efr32=off', 'start=off'])
        self.assertEqual(self.reboot(), ['efr32=off', 'start=off', 'port=off'])


del test_loader_update          # (its own cases are run from its own file, not again from here)

if __name__ == '__main__':
    unittest.main()
