# SPDX-License-Identifier: MIT
"""host/w1700k/efr32.py: the low-power radio from the host. The unit's `w1700k-efr32 link` is stood
in for by a small program on this machine that is a radio's port on its stdin and stdout: one
that answers BGAPI as the factory firmware does (the bytes are the ones read on a unit), one that
speaks something else, one that says nothing, and one that ends with a reason."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))
from w1700k import efr32      # noqa: E402

# A Bluetooth co-processor as it answered on a unit (stack 6.2.0 build 212): its boot event first,
# unasked, then a response to each of the two commands; anything else gets a result of "not known".
FACTORY = r'''
import sys
out, inp = sys.stdout.buffer, sys.stdin.buffer
out.write(bytes.fromhex('a01201000600020000 00d400000000000101 8471ff4c'.replace(' ', ''))); out.flush()
while True:
    head = inp.read(4)
    if len(head) < 4:
        break
    body = inp.read((head[0] & 7) << 8 | head[1])
    if head[2:] == b'\x01\x00':
        out.write(bytes.fromhex('200201000000'))
    elif head[2:] == b'\x01\x1b':
        out.write(bytes.fromhex('2012011b0000060002000000d400000000008471ff4c'))
    else:
        out.write(bytes([0x20, 2, head[2], head[3], 0x02, 0x00]))
    out.flush()
'''
OTHER = "import sys\nsys.stdout.buffer.write(b'\\x7e\\x00\\x1a\\xc1\\x02\\x0b\\x0a\\x52\\x7e'); sys.stdout.buffer.flush(); sys.stdin.buffer.read()\n"
SILENT = "import sys\nsys.stdin.buffer.read()\n"
REFUSED = "import sys\nsys.stderr.write('w1700k-efr32: the radio is held in reset: nothing would answer (w1700k-efr32 on)\\n'); sys.exit(1)\n"
ECHO = "import os\nwhile True:\n    data = os.read(0, 4096)\n    if not data:\n        break\n    os.write(1, data.upper())\n"


def process(program):
    return subprocess.Popen([sys.executable, '-c', program], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)


class StandInUnit:
    """What efr32.py uses of a Unit: the commands it ran, and a program for the link."""

    def __init__(self, program=FACTORY, said='efr32=on\nstart=on\nport=on\n'):
        self.program, self.said, self.ran = program, said, []

    def tool(self, name):
        return '/usr/sbin/' + name

    def check(self, command):
        self.ran.append(command)
        return self.said

    def popen_bytes(self, command):
        self.ran.append(command)
        return process(self.program)


class Radio(unittest.TestCase):
    def link(self, program):
        link = efr32.Link(process(program))
        self.addCleanup(link.close)
        return link

    def test_the_factory_firmware_says_what_it_is(self):
        """Two commands, neither of which makes it do anything else; the boot event that came
        first, unasked, is passed over."""
        self.assertEqual(efr32.identify(self.link(FACTORY)),
                         {'firmware': 'bgapi', 'stack': '6.2.0', 'build': 212, 'bootloader': '0x00000000',
                          'hardware': None, 'hash': '0x4cff7184'})

    def test_only_hello_and_get_version_are_sent(self):
        self.assertEqual((efr32.command(efr32.SYSTEM, efr32.HELLO), efr32.command(efr32.SYSTEM, efr32.GET_VERSION)),
                         (bytes.fromhex('20000100'), bytes.fromhex('2000011b')))
        source = (ROOT / 'host/w1700k/efr32.py').read_text()
        asked = [line.strip() for line in source.splitlines() if 'link.ask(' in line]
        self.assertEqual(asked, ['hello = link.ask(SYSTEM, HELLO)', 'body = link.ask(SYSTEM, GET_VERSION)'])

    def test_the_boot_event_is_read_with_its_hardware_word(self):
        link = self.link(FACTORY)
        kind, cls, cmd, body = link.message()
        self.assertEqual((kind, cls, cmd), (efr32.EVENT, efr32.SYSTEM, efr32.BOOT))
        self.assertEqual(efr32.version_from_boot(body)['hardware'], '0x0101')

    def test_other_firmware_silence_and_a_refusal_are_each_said_plainly(self):
        with self.assertRaisesRegex(efr32.RadioError, 'is not BGAPI .* other firmware, or another speed'):
            efr32.identify(self.link(OTHER))
        with self.assertRaisesRegex(efr32.RadioError, 'did not answer'):
            self.link(SILENT).ask(efr32.SYSTEM, efr32.HELLO, timeout=0.3)
        refused = self.link(REFUSED)
        refused.process.wait(timeout=5)
        with self.assertRaisesRegex(efr32.RadioError, 'the radio is held in reset'):
            efr32.identify(refused)

    def test_a_command_the_radio_does_not_know_is_not_taken_for_an_answer(self):
        link = self.link(FACTORY)
        self.assertEqual(link.ask(0x20, 0x01), b'\x02\x00', 'its own response, a result that is not zero')
        with self.assertRaisesRegex(efr32.RadioError, 'refused: result 0x0002'):
            efr32.version_from_response(b'\x02\x00' + bytes(16))


class OnAUnit(unittest.TestCase):
    def test_status_and_the_switch_are_the_units_own_command(self):
        unit = StandInUnit(said='efr32=off\nstart=off\nport=off\n')
        self.assertEqual(efr32.status(unit), {'efr32': 'off', 'start': 'off', 'port': 'off'})
        unit.said = 'efr32=on\nstart=on\n'
        self.assertEqual(efr32.switch(unit, True, keep=True), {'efr32': 'on', 'start': 'on'})
        efr32.switch(unit, False)
        self.assertEqual(unit.ran, ['/usr/sbin/w1700k-efr32 status', '/usr/sbin/w1700k-efr32 on --keep',
                                    '/usr/sbin/w1700k-efr32 off'])

    def test_the_link_is_the_units_link_command_at_a_speed_the_port_has(self):
        unit = StandInUnit()
        link = efr32.open_link(unit)
        self.addCleanup(link.close)
        self.assertEqual(efr32.identify(link)['stack'], '6.2.0')
        self.assertEqual(unit.ran, ['/usr/sbin/w1700k-efr32 link --baud 115200'])
        with self.assertRaisesRegex(ValueError, 'baud: one of 115200, 230400'):
            efr32.open_link(unit, 460800)
        self.assertEqual(len(unit.ran), 1)

    @unittest.skipUnless(hasattr(os, 'openpty'), 'no pseudo-terminals')
    def test_the_pty_is_the_port_byte_for_byte_until_the_link_ends(self):
        unit = StandInUnit(program=ECHO)
        named, failed = [], []

        def run():
            try:
                efr32.pty(unit, announce=named.append)
            except efr32.RadioError as error:
                failed.append(str(error))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not named and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(named and named[0].startswith('/dev/'), named)
        fd = os.open(named[0], os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(fd, bytes([0x20, 0x00, 0x01, 0x00, 0x03, 0x0a, 0x0d]) + b'abc')     # control bytes pass as they are
            got = b''
            while len(got) < 10 and time.monotonic() < deadline:
                got += os.read(fd, 64)
            self.assertEqual(got, bytes([0x20, 0x00, 0x01, 0x00, 0x03, 0x0a, 0x0d]) + b'ABC')
        finally:
            os.close(fd)


class CommandLine(unittest.TestCase):
    """`w1700k efr32 ...` with the unit's ssh stood in for by a program of the same name."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, self.dir)
        factory = self.dir / 'factory.py'
        factory.write_text(FACTORY)
        ssh = self.dir / 'ssh'
        # the last argument is the unit's command; `-O exit` closes the shared connection
        ssh.write_text(f'''#!/bin/sh
for last; do :; done
echo "$last" >> {self.dir}/ran
case "$*" in *"-O exit"*) exit 0 ;; esac
case "$last" in
*"w1700k-efr32 status") printf 'efr32=on\\nstart=on\\nport=on\\n' ;;
*"w1700k-efr32 on --keep") printf 'efr32=on\\nstart=on\\n' ;;
*"w1700k-efr32 off") printf 'efr32=off\\nstart=on\\n' ;;
*"w1700k-efr32 link --baud 115200") exec {sys.executable} {factory} ;;
*) echo "unexpected: $last" >&2; exit 1 ;;
esac
''')
        ssh.chmod(0o755)
        config = self.dir / 'config.json'
        config.write_text('{}')
        self.env = dict(os.environ, PATH=f'{self.dir}:{os.environ["PATH"]}', W1700K_CONFIG=str(config))

    def w1700k(self, *words):
        return subprocess.run([sys.executable, str(ROOT / 'host/bin/w1700k'), 'efr32', *words], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def test_status_on_off_and_identify(self):
        import json
        result = self.w1700k('status')
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {'efr32': 'on', 'start': 'on', 'port': 'on'}),
                         result.stderr)
        self.assertEqual(json.loads(self.w1700k('on', '--keep').stdout), {'efr32': 'on', 'start': 'on', 'port': 'on'})
        self.assertEqual(json.loads(self.w1700k('off').stdout)['efr32'], 'off')
        result = self.w1700k('identify')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['stack'], '6.2.0')
        ran = (self.dir / 'ran').read_text()
        self.assertIn('w1700k-efr32 link --baud 115200', ran)
        self.assertNotIn('mt7996', ran, 'the receiver is not involved: no controller is started for this')

    @unittest.skipUnless(hasattr(os, 'openpty'), 'no pseudo-terminals')
    def test_the_pty_names_its_device_and_ends_when_told_to(self):
        """Told to end as a service is (SIGTERM), or interrupted where the interrupt was being
        ignored (a background job): it ends cleanly either way."""
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig):
                bridge = subprocess.Popen([sys.executable, str(ROOT / 'host/bin/w1700k'), 'efr32', 'pty'],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env,
                                          preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
                self.addCleanup(bridge.kill)
                self.addCleanup(bridge.stderr.close)
                self.addCleanup(bridge.stdout.close)
                path = bridge.stdout.readline().strip()
                self.assertTrue(path.startswith('/dev/'), path)
                fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
                os.write(fd, bytes.fromhex('20000100'))
                got, deadline = b'', time.monotonic() + 5
                while len(got) < 24 and time.monotonic() < deadline:       # the boot event, then the answer
                    got += os.read(fd, 64)
                os.close(fd)
                self.assertTrue(got.endswith(bytes.fromhex('200201000000')), got.hex())
                bridge.send_signal(sig)
                self.assertEqual(bridge.wait(timeout=10), 0, bridge.stderr.read())

    def test_what_the_unit_refuses_is_said_and_is_an_error(self):
        (self.dir / 'ssh').write_text('#!/bin/sh\ncase "$*" in *"-O exit"*) exit 0 ;; esac\n'
                                      'echo "w1700k-efr32-line: this board (xr1710g) is not known to carry the low-power radio; nothing done" >&2\nexit 1\n')
        result = self.w1700k('status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('not known to carry the low-power radio', result.stderr)


if __name__ == '__main__':
    unittest.main()
