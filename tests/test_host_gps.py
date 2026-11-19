# SPDX-License-Identifier: MIT
"""host/w1700k/gps.py: the GPS receiver from the host. The unit's `w1700k-gps link` is stood in for
by a small program on this machine that is a receiver's port on its stdin and stdout: one that
sends NMEA as a receiver does (made-up sentences, with a made-up position), one that says nothing,
one that sends something else, and one that ends with a reason."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import reduce
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))
from w1700k import gps, portlink      # noqa: E402


def sentence(body):
    return f'${body}*{reduce(lambda a, b: a ^ b, body.encode(), 0):02X}\r\n'


# One second of a receiver with a fix. The position is nowhere: 12 34.5678 N, 123 45.6789 E.
SECOND = ''.join(map(sentence, (
    'GNGGA,120000.000,1234.5678,N,12345.6789,E,1,17,0.8,12.3,M,0.0,M,,',
    'GNGSA,A,3,01,02,03,04,05,06,07,08,,,,,1.5,0.8,1.2,1',
    'GPGSV,2,1,08,01,45,090,40,02,30,180,38,03,60,270,42,04,15,010,30,1',
    'GNRMC,120000.000,A,1234.5678,N,12345.6789,E,0.00,0.00,191126,,,A,V',
    'PAIR001,864,0')))
TALKER = f'''
import sys, time
out = sys.stdout.buffer
for _ in range(50):
    out.write({SECOND!r}.encode()); out.write(b'$GNGGA,broken*00\\r\\n'); out.flush()
    time.sleep(0.05)
'''
NO_FIX = f'''
import sys, time
out = sys.stdout.buffer
for _ in range(50):
    out.write({sentence('GNGGA,,,,,,0,00,99.9,,M,,M,,') + sentence('GNGSA,A,1,,,,,,,,,,,,,99.9,99.9,99.9,1')!r}.encode()); out.flush()
    time.sleep(0.05)
'''
SILENT = "import sys\nsys.stdin.buffer.read()\n"
OTHER = "import sys, time\nfor _ in range(40):\n    sys.stdout.buffer.write(bytes(range(128, 200)) + b'\\n'); sys.stdout.buffer.flush(); time.sleep(0.05)\n"
REFUSED = "import sys\nsys.stderr.write('w1700k-gps: the receiver is switched off: nothing would be heard (w1700k-gps on)\\n'); sys.exit(1)\n"
ECHO = "import os\nwhile True:\n    data = os.read(0, 4096)\n    if not data:\n        break\n    os.write(1, data.upper())\n"


def process(program):
    return subprocess.Popen([sys.executable, '-c', program], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)


class StandInUnit:
    """What gps.py uses of a Unit: the commands it ran, and a program for the link."""

    def __init__(self, program=TALKER, said='gps=on\nstart=on\nport=on\n'):
        self.program, self.said, self.ran = program, said, []

    def tool(self, name):
        return '/usr/sbin/' + name

    def check(self, command):
        self.ran.append(command)
        return self.said

    def popen_bytes(self, command):
        self.ran.append(command)
        return process(self.program)


class Receiver(unittest.TestCase):
    def link(self, program):
        link = gps.Link(process(program))
        self.addCleanup(link.close)
        return link

    def test_a_sentence_is_one_whose_checksum_holds(self):
        good = sentence('GNGGA,120000.000,1234.5678,N,12345.6789,E,1,17,0.8,12.3,M,0.0,M,,').encode()
        self.assertEqual(gps.fields(good)[0], 'GNGGA')
        self.assertEqual(gps.fields(good.strip()), gps.fields(good), 'with or without its line ending')
        for bad in (good.replace(b'1234', b'1235', 1), good[1:], good[:-5] + b'\r\n', b'', b'$*', b'$GNGGA*ZZ',
                    b'\xff\xfe$GNGGA,1*00', '$GNGGA,é*00'.encode()):
            with self.subTest(bad=bad):
                self.assertIsNone(gps.fields(bad))

    def test_check_says_that_it_talks_and_has_a_fix_and_never_where(self):
        result = gps.check(self.link(TALKER), seconds=0.6)
        self.assertEqual(set(result['sentences']), {'GGA', 'GSA', 'GSV', 'RMC', 'PAIR001'})
        self.assertGreaterEqual(result['sentences']['GGA'], 3)
        self.assertEqual((result['fix'], result['satellites']), ('3d', 17))
        self.assertGreaterEqual(result['bad'], 3, 'a line whose checksum does not hold is counted, not believed')
        said = json.dumps(result)
        for part in ('1234', '5678', '12345', '6789'):
            self.assertNotIn(part, said, 'the position is not in what it says')
        self.assertEqual(set(result), {'sentences', 'bad', 'fix', 'satellites'})

    def test_listening_from_the_middle_of_a_sentence_is_not_a_bad_line(self):
        """The first thing heard is usually the end of a sentence: it is passed over. The same
        fragment anywhere later, or a first line that is something else, is still counted."""
        tail = SECOND.split('\n')[0][20:] + '\n'
        start = f"import sys\nsys.stdout.buffer.write(({tail!r} + {SECOND!r}).encode()); sys.stdout.buffer.flush(); sys.stdin.buffer.read()\n"
        self.assertEqual(gps.check(self.link(start), seconds=0.3)['bad'], 0)
        later = f"import sys\nsys.stdout.buffer.write(({SECOND!r} + {tail!r} + {SECOND!r}).encode()); sys.stdout.buffer.flush(); sys.stdin.buffer.read()\n"
        self.assertEqual(gps.check(self.link(later), seconds=0.3)['bad'], 1)
        noise = f"import sys\nsys.stdout.buffer.write((b'\\x80\\x81 noise\\n' + {SECOND!r}.encode())); sys.stdout.buffer.flush(); sys.stdin.buffer.read()\n"
        self.assertEqual(gps.check(self.link(noise), seconds=0.3)['bad'], 1)

    def test_a_receiver_without_a_fix_says_so(self):
        result = gps.check(self.link(NO_FIX), seconds=0.4)
        self.assertEqual((result['fix'], result['satellites']), ('none', 0))

    def test_silence_something_else_and_a_refusal_are_each_said_plainly(self):
        with self.assertRaisesRegex(gps.GpsError, 'said nothing in 0.3 s'):
            gps.check(self.link(SILENT), seconds=0.3)
        with self.assertRaisesRegex(gps.GpsError, 'is not NMEA: another speed, or another protocol'):
            gps.check(self.link(OTHER), seconds=0.4)
        with self.assertRaisesRegex(gps.GpsError, 'the receiver is switched off'):
            gps.check(self.link(REFUSED), seconds=2)

    def test_a_reason_that_comes_a_moment_after_the_output_closes_is_still_the_reason(self):
        """A command that is refusing closes its output before it has ended (seen on a slower
        machine, where the reason was lost and only "the link ended" was said)."""
        slow = ("import os, sys, time\nos.close(1)\ntime.sleep(0.4)\n"
                "sys.stderr.write('w1700k-gps: the receiver is switched off: nothing would be heard (w1700k-gps on)\\n')\n"
                "sys.exit(1)\n")
        for _ in range(3):
            with self.assertRaisesRegex(gps.GpsError, 'the receiver is switched off'):
                gps.check(self.link(slow), seconds=2)

    def test_nothing_is_written_to_the_receiver_to_check_it(self):
        heard = Path(tempfile.mkdtemp()) / 'heard'
        self.addCleanup(__import__('shutil').rmtree, heard.parent)
        link = self.link(f"import sys, time\nsys.stdout.buffer.write({SECOND!r}.encode()); sys.stdout.buffer.flush()\n"
                         f"open({str(heard)!r}, 'wb').write(sys.stdin.buffer.read())\n")
        gps.check(link, seconds=0.3)
        link.close()
        self.assertEqual(heard.read_bytes(), b'')


class OnAUnit(unittest.TestCase):
    def test_status_and_the_switch_are_the_units_own_command(self):
        unit = StandInUnit()
        self.assertEqual(gps.status(unit), {'gps': 'on', 'start': 'on', 'port': 'on'})
        unit.said = 'gps=off\nstart=off\n'
        self.assertEqual(gps.switch(unit, False, keep=True), {'gps': 'off', 'start': 'off'})
        gps.switch(unit, True)
        self.assertEqual(unit.ran, ['/usr/sbin/w1700k-gps status', '/usr/sbin/w1700k-gps off --keep', '/usr/sbin/w1700k-gps on'])

    def test_the_link_is_the_units_link_command_at_a_speed_the_port_has(self):
        unit = StandInUnit()
        link = gps.open_link(unit)
        self.addCleanup(link.close)
        self.assertEqual(gps.check(link, seconds=0.3)['fix'], '3d')
        gps.open_link(unit, 115200).close()
        self.assertEqual(unit.ran, ['/usr/sbin/w1700k-gps link --baud 921600', '/usr/sbin/w1700k-gps link --baud 115200'])
        with self.assertRaisesRegex(ValueError, 'baud: one of 115200, 230400, 460800, 921600'):
            gps.open_link(unit, 9600)
        self.assertEqual(len(unit.ran), 2)

    @unittest.skipUnless(hasattr(os, 'openpty'), 'no pseudo-terminals')
    def test_the_pty_is_the_port_under_a_name_that_stays_and_goes_when_it_ends(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, directory)
        name = directory / 'gps'
        name.symlink_to('/dev/pts/999999')              # left by a run that was killed: replaced
        unit = StandInUnit(program=ECHO)
        named, failed = [], []

        def run():
            try:
                gps.pty(unit, announce=named.append, name=name)
            except gps.GpsError as error:
                failed.append(str(error))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not named and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(named, [str(name)])
        self.assertTrue(os.readlink(name).startswith('/dev/'))
        fd = os.open(name, os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(fd, b'$pair002*38\r\n')
            got = b''
            while len(got) < 13 and time.monotonic() < deadline:
                got += os.read(fd, 64)
            self.assertEqual(got, b'$PAIR002*38\r\n', 'what is written goes to the receiver, and what it says comes back')
        finally:
            os.close(fd)

    @unittest.skipUnless(hasattr(os, 'openpty'), 'no pseudo-terminals')
    def test_a_name_that_is_somebody_elses_file_is_not_replaced(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, directory)
        name = directory / 'notes'
        name.write_text('mine\n')
        with self.assertRaises(FileExistsError):
            gps.pty(StandInUnit(program=ECHO), announce=lambda path: None, name=name, rounds=1)
        self.assertEqual(name.read_text(), 'mine\n')
        link = directory / 'gps'
        gps.pty(StandInUnit(program=ECHO), announce=lambda path: None, name=link, rounds=1)
        self.assertFalse(os.path.lexists(link), 'and its own name is gone when it ends')


class CommandLine(unittest.TestCase):
    """`w1700k gps ...` with the unit's ssh stood in for by a program of the same name."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, self.dir)
        talker = self.dir / 'talker.py'
        talker.write_text(TALKER)
        ssh = self.dir / 'ssh'
        ssh.write_text(f'''#!/bin/sh
for last; do :; done
echo "$last" >> {self.dir}/ran
case "$*" in *"-O exit"*) exit 0 ;; esac
case "$last" in
*"w1700k-gps status") printf 'gps=on\\nstart=on\\nport=on\\n' ;;
*"w1700k-gps on --keep") printf 'gps=on\\nstart=on\\n' ;;
*"w1700k-gps off") printf 'gps=off\\nstart=on\\n' ;;
*"w1700k-gps link --baud 921600") exec {sys.executable} {talker} ;;
*) echo "unexpected: $last" >&2; exit 1 ;;
esac
''')
        ssh.chmod(0o755)
        config = self.dir / 'config.json'
        config.write_text('{}')
        self.env = dict(os.environ, PATH=f'{self.dir}:{os.environ["PATH"]}', W1700K_CONFIG=str(config))

    def w1700k(self, *words):
        return subprocess.run([sys.executable, str(ROOT / 'host/bin/w1700k'), 'gps', *words], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def test_status_on_off_and_check(self):
        result = self.w1700k('status')
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {'gps': 'on', 'start': 'on', 'port': 'on'}),
                         result.stderr)
        self.assertEqual(json.loads(self.w1700k('on', '--keep').stdout), {'gps': 'on', 'start': 'on', 'port': 'on'})
        self.assertEqual(json.loads(self.w1700k('off').stdout)['gps'], 'off')
        result = self.w1700k('check', '--seconds', '0.5')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((json.loads(result.stdout)['fix'], json.loads(result.stdout)['satellites']), ('3d', 17))
        self.assertNotIn('1234', result.stdout + result.stderr)
        ran = (self.dir / 'ran').read_text()
        self.assertIn('w1700k-gps link --baud 921600', ran)
        self.assertNotIn('mt7996', ran, 'the receiver of ours is not involved: no controller is started for this')

    @unittest.skipUnless(hasattr(os, 'openpty'), 'no pseudo-terminals')
    def test_the_pty_names_its_device_and_ends_when_told_to(self):
        import signal
        name = self.dir / 'gps0'
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig):
                bridge = subprocess.Popen([sys.executable, str(ROOT / 'host/bin/w1700k'), 'gps', 'pty', '--name', str(name)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env,
                                          preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
                self.addCleanup(bridge.kill)
                self.addCleanup(bridge.stderr.close)
                self.addCleanup(bridge.stdout.close)
                self.assertEqual(bridge.stdout.readline().strip(), str(name))
                fd = os.open(name, os.O_RDONLY | os.O_NOCTTY)
                got, deadline = b'', time.monotonic() + 5
                while got.count(b'\n') < 3 and time.monotonic() < deadline:
                    got += os.read(fd, 256)
                os.close(fd)
                self.assertEqual(gps.fields(got.split(b'\n')[0])[0], 'GNGGA', 'as the receiver sent it')
                bridge.send_signal(sig)
                self.assertEqual(bridge.wait(timeout=10), 0, bridge.stderr.read())
                self.assertFalse(os.path.lexists(name))

    def test_what_the_unit_refuses_is_said_and_is_an_error(self):
        (self.dir / 'ssh').write_text('#!/bin/sh\ncase "$*" in *"-O exit"*) exit 0 ;; esac\n'
                                      'echo "w1700k-gps-line: this board (xr1710g) has no GPS receiver; nothing done" >&2\nexit 1\n')
        for words in (('status',), ('check',)):
            result = self.w1700k(*words)
            self.assertEqual(result.returncode, 1)
            self.assertIn('has no GPS receiver', result.stderr)


class OneLink(unittest.TestCase):
    def test_the_radio_and_the_receiver_are_reached_the_same_way(self):
        from w1700k import efr32
        self.assertTrue(issubclass(gps.Link, portlink.Link) and issubclass(efr32.Link, portlink.Link))
        self.assertTrue(issubclass(gps.GpsError, portlink.LinkError) and issubclass(efr32.RadioError, portlink.LinkError))


if __name__ == '__main__':
    unittest.main()
