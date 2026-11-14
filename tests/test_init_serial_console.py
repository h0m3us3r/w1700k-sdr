# SPDX-License-Identifier: MIT
"""The boot catcher's fault cases, and the console session over a pseudo-terminal."""
import importlib.util
import os
from pathlib import Path
import pty
import select
import tempfile
import threading
import time
import unittest
from unittest import mock

try:
    import serial
except ImportError:
    serial = None

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('init_uart', ROOT / 'init/initkit/uart.py')
console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console)


class CatcherTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.events = []
        self.catcher = console.BootCatcher(
            lambda data, reason: self.sent.append(data),
            lambda kind, **fields: self.events.append((kind, fields)),
            token='uart_test123')

    def rx(self, data, now=0):
        self.catcher.receive(data, now)
        self.catcher.tick(now)

    def test_fragmented_ansi_countdown_variants(self):
        for prompt in (b'Hit any key to stop autoboot:',
                       b'PRESS\tANY KEY TO INTERRUPT AUTO BOOT:',
                       b'Press key to abort boot:',
                       b'\x1b[33mHit any\r\n key to stop auto\x1b[0mboot:'):
            self.setUp()
            for i, byte in enumerate(prompt):
                self.rx(bytes([byte]), i * .001)
            self.assertEqual(self.catcher.state, 'interrupting')
            self.assertTrue(self.sent)
            self.assertEqual(set(self.sent), {b'    '})

    def test_banner_waits_for_countdown_before_retries(self):
        self.rx(b'\r\nU-Boot 2014.04-rc1 (build)\r\n')
        for now in (.06, .12, .18):
            self.catcher.tick(now)
        self.assertEqual(self.sent, [])
        self.rx(b'Now running in RAM - U-Boot at: ffdb7000\r\nusxgmii_pcs_int en 1\r\n', .19)
        self.assertEqual(self.sent, [])
        self.rx(b'USXGMII_10G exit\r\nHit any key to stop autoboot: 4', .2)
        self.assertEqual(self.sent, [b'    '])
        self.rx(b'\r\nECNT> ', .21)
        self.catcher.tick(.25)
        self.assertEqual(len(self.sent), 1)  # Prompt stops keying immediately.
        self.catcher.tick(.32)
        self.assertEqual(self.sent[-1], b'echo uart_test123\r')
        self.assertEqual(self.catcher.state, 'verifying')

    def test_corrupt_countdown_insert_delete_substitute(self):
        original = b'Hit any key to stop autoboot:'
        for index in range(len(original)):
            for damaged in (original[:index] + original[index + 1:],
                            original[:index] + b'\xff' + original[index + 1:],
                            original[:index] + b'\x00' + original[index:]):
                with self.subTest(index=index, damaged=damaged):
                    self.setUp()
                    self.rx(damaged)
                    self.assertEqual(self.catcher.state, 'interrupting')
                    self.assertEqual(self.sent, [b'    '])

    def test_multiple_damaged_bytes_and_fragmentation(self):
        for damaged in (b'H?t any k\xffy to stop auto\x00boot:',
                        b'Pr?ss any key to interru\x00pt autooot:',
                        b'Hit any k\xffy to stop autoboot:'):
            self.setUp()
            for i, byte in enumerate(damaged):
                self.rx(bytes([byte]), i * .001)
            self.assertEqual(self.catcher.state, 'interrupting')
            self.assertEqual(self.sent, [b'    '])

    def test_noise_and_unrelated_text_do_not_trigger(self):
        for data in (b'\xff\x00\xaa\x55' * 128,
                     b'Hit the reset key to enter diagnostics',
                     b'BootROM DDR training completed, enter command',
                     b'Press any button to start upgrade', b'EC?T> ',
                     b'U-Boot 2014.04', b'U-B\xffot 2014.04'):
            self.setUp()
            self.rx(data)
            self.catcher.tick(.2)
            self.assertEqual(self.sent, [])
            self.assertEqual(self.catcher.state, 'armed')

    def test_damaged_command_prompt_needs_boot_context_and_exact_token(self):
        self.rx(b'U-Boot 2014.04\r\nHit any key to stop autoboot: 4')
        self.rx(b'\r\nEC?T> ', .1)
        self.catcher.tick(.21)
        self.assertEqual(self.catcher.state, 'verifying')
        self.assertEqual(self.sent[-1], b'echo uart_test123\r')
        self.rx(b'\r\nuart_test12?\r\nEC?T> ', .3)
        self.catcher.tick(.5)
        self.assertEqual(self.catcher.state, 'verifying')
        self.rx(b'\r\nuart_test123\r\nEC?T> ', .6)
        self.catcher.tick(.8)
        self.assertEqual(self.catcher.state, 'confirmed')

    def test_short_or_excessively_damaged_prompt_is_not_accepted(self):
        for prompt in (b'> ', b'EC??> ', b'ECNT> help', b'root# '):
            self.assertIsNone(console.command_prompt(prompt, allow_fuzzy=True))

    def test_corrupt_linux_handoff_overrides_boot(self):
        for marker in (b'Starting ker\xffnel', b'Star?ing kerne?',
                       b'Linux versio\x00n 6.18'):
            self.setUp()
            self.rx(b'U-Boot 2014.04\r\n' + marker + b'\r\nECNT> ')
            self.catcher.tick(.2)
            self.assertEqual(self.catcher.state, 'missed')
            self.assertEqual(self.sent, [])

    def test_burst_byte_cap_cannot_be_exceeded(self):
        self.rx(b'U-Boot 2014.04\r\nHit any key to stop autoboot: 4')
        for _ in range(400):
            self.catcher.tick(self.catcher.next_key)
        self.assertEqual(sum(map(len, self.sent)), 1200)
        self.assertEqual(set(self.sent), {b'    '})
        self.assertEqual(self.catcher.state, 'inconclusive')

    def test_confirmation_needs_actual_echo_output_and_new_prompt(self):
        self.rx(b'\r\nECNT> ')
        self.catcher.tick(.11)
        self.rx(b'echo uart_test123\r\nECNT> ', .2)
        self.catcher.tick(.4)
        self.assertEqual(self.catcher.state, 'verifying')  # Local command echo is insufficient.
        self.rx(b'\r\nuart_test123\r\n', .5)
        self.catcher.tick(.7)
        self.assertEqual(self.catcher.state, 'verifying')  # Output alone is insufficient.
        self.rx(b'ECNT> ', .8)
        self.catcher.tick(1)
        self.assertEqual(self.catcher.state, 'confirmed')
        before = self.sent[:]
        self.rx(b'\r\nU-Boot 2026.01\r\n', 2)
        self.catcher.tick(100)
        self.assertEqual(self.sent, before)  # No automatic rearm on a second boot.

    def test_old_token_and_embedded_prompt_do_not_confirm(self):
        self.rx(b'uart_test123\r\nmessage mentioning ECNT> but not a prompt\r\n')
        self.catcher.tick(.2)
        self.assertEqual(self.sent, [])
        self.rx(b'ECNT> ', .3)
        self.catcher.tick(.5)
        self.rx(b'\r\nECNT> ', .6)
        self.catcher.tick(.8)
        self.assertEqual(self.catcher.state, 'verifying')

    def test_linux_evidence_overrides_banner_and_prompt(self):
        self.rx(b'U-Boot 2014.04\r\nStarting kernel ...\r\nECNT> ')
        self.catcher.tick(1)
        self.assertEqual(self.catcher.state, 'missed')
        self.assertEqual(self.sent, [])

    def test_linux_handoff_stops_active_retries(self):
        self.rx(b'U-Boot 2014.04\r\nHit any key to stop autoboot: 4')
        self.rx(b'## Booting kernel from Legacy Image\r\n', .06)
        self.catcher.tick(1)
        self.assertEqual(self.sent, [b'    '])
        self.assertEqual(self.catcher.state, 'missed')

    def test_silence_after_countdown_has_bounded_retries(self):
        self.rx(b'U-Boot 2014.04\r\nHit any key to stop autoboot: 4')
        for i in range(1, 401):
            self.catcher.tick(i * .06)
        self.assertLessEqual(sum(map(len, self.sent)), console.MAX_KEYS)
        self.assertEqual(self.catcher.state, 'inconclusive')
        count = len(self.sent)
        self.rx(b'Hit any key to stop autoboot:', 30)
        self.assertEqual(len(self.sent), count)

    def test_repeated_banner_does_not_extend_deadline(self):
        self.rx(b'U-Boot 2014.04\r\nHit any key to stop autoboot: 4')
        self.rx(b'U-Boot 2014.04\r\n', 14)
        self.catcher.tick(15)
        self.assertEqual(self.catcher.state, 'inconclusive')

    def test_unknown_boot_has_no_tx_and_finite_decision(self):
        self.rx(b'\xff\x00BootROM DDR training\r\n')
        self.catcher.tick(45)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.catcher.state, 'inconclusive')

    def test_a_damaged_verification_is_sent_again(self):
        self.rx(b'Hit any key to stop autoboot: 4', 0)
        self.catcher.tick(0)
        self.rx(b'\r\nECNT> ', 1)
        self.catcher.tick(2)
        command = b'echo uart_test123\r'
        self.assertEqual(self.sent[-1], command)
        # Stock U-Boot lost a character of the line: what it echoes is not the token.
        self.rx(b'echo uarttest123\r\nuarttest123\r\nECNT> ', 2.1)
        self.catcher.tick(2.3)
        self.assertEqual(self.catcher.state, 'verifying')
        self.assertEqual(self.sent[-2:], [command, command])
        self.rx(b'echo uart_test123\r\nuart_test123\r\nECNT> ', 2.4)
        self.catcher.tick(2.6)
        self.assertEqual(self.catcher.state, 'confirmed')

    def test_verification_is_not_repeated_without_end(self):
        self.rx(b'Hit any key to stop autoboot: 4', 0)
        self.catcher.tick(0)
        self.rx(b'\r\nECNT> ', 1)
        now = 2
        for _ in range(6):
            self.catcher.tick(now)
            self.rx(b'echo uarttest123\r\nuarttest123\r\nECNT> ', now + .1)
            now += .3
        self.assertEqual(self.sent.count(b'echo uart_test123\r'), console.VERIFY_ATTEMPTS)
        self.catcher.tick(now + console.VERIFY_SECONDS)
        self.assertEqual(self.catcher.state, 'inconclusive')

    def test_echo_timeout_does_not_claim_success(self):
        self.rx(b'=> ')
        self.catcher.tick(.11)
        self.catcher.tick(5.2)
        self.assertEqual(self.catcher.state, 'inconclusive')
        self.assertEqual(self.sent, [b'echo uart_test123\r'])

    def test_manual_takeover_disables_all_automation(self):
        self.rx(b'U-Boot 2014.04\r\nHit any key to stop autoboot: 4')
        self.catcher.manual()
        self.rx(b'ECNT> ', 1)
        self.catcher.tick(2)
        self.assertEqual(self.sent, [b'    '])
        self.assertEqual(self.catcher.state, 'manual')


@unittest.skipIf(serial is None, 'needs pyserial')
class SessionFixture(unittest.TestCase):
    """A ConsoleSession on one end of a pseudo-terminal; the test is the unit."""
    passive = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='w1700k-uart-test-')
        self.addCleanup(self.tmp.cleanup)
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.slave)
        self.addCleanup(self.close_master)
        self.events = []
        self.session = console.ConsoleSession(
            os.ttyname(self.slave), self.tmp.name,
            lambda kind, **fields: self.events.append((kind, fields)),
            passive=self.passive)
        self.session.open()
        self.addCleanup(self.session.close)

    def close_master(self):
        if self.master is not None:
            os.close(self.master)
            self.master = None

    def read_until(self, suffix):
        data = b''
        until = time.monotonic() + 3
        while not data.endswith(suffix) and time.monotonic() < until:
            if select.select([self.master], [], [], .1)[0]:
                data += os.read(self.master, 4096)
        self.assertTrue(data.endswith(suffix), repr(data))
        return data

    def states(self):
        return [fields['state'] for kind, fields in self.events if kind == 'catcher_state']


class SessionTests(SessionFixture):
    def test_the_port_is_held_exclusively(self):
        with self.assertRaises(serial.SerialException):
            serial.Serial(os.ttyname(self.slave), 115200, exclusive=True)

    def test_lost_interrupt_bytes_then_verified_console(self):
        banner = b'\r\nU-Boot 2014.04-rc1\r\nusxgmii_pcs_int en 1\r\n'
        countdown = b'USXGMII_10G exit\r\nHit any key to stop autoboot: 4'
        os.write(self.master, banner)
        self.assertFalse(select.select([self.master], [], [], .2)[0])
        os.write(self.master, countdown)
        # The simulated board ignores the first three four-byte interrupt bursts.
        keys = b''
        while len(keys) < 12:
            keys += self.read_until(b' ')
        self.assertEqual(set(keys), {ord(' ')})
        prompt = b'\r\n\x1b[32mECNT> \x1b[0m'
        os.write(self.master, prompt)
        command = self.read_until(b'\r').lstrip(b' ')
        self.assertTrue(command.startswith(b'echo uart_'), repr(command))
        token = command[5:-1]
        reply = command + b'\r\n' + token + b'\r\nECNT> '
        os.write(self.master, reply)
        self.assertEqual(self.session.wait_for_catch(3), 'confirmed')
        self.assertFalse(select.select([self.master], [], [], .2)[0])
        with (Path(self.tmp.name) / 'tx.fifo').open('wb', buffering=0) as control:
            control.write(b'help\r')
        self.assertEqual(self.read_until(b'\r'), b'help\r')
        self.session.close()
        self.assertEqual((Path(self.tmp.name) / 'rx.bin').read_bytes(),
                         banner + countdown + prompt + reply)
        self.assertIn('manual', self.states())
        self.assertFalse((Path(self.tmp.name) / 'tx.fifo').exists())

    def test_a_rearmed_catcher_stops_the_next_boot(self):
        os.write(self.master, b'ECNT> ')
        self.session.wait(lambda: self.session.at_prompt('stock'), 2, 'prompt')
        self.session.arm()
        os.write(self.master, b'\r\nU-Boot 2014.04\r\nHit any key to stop autoboot: 4')
        self.assertEqual(set(self.read_until(b' ')), {ord(' ')})

    def unit(self, lose=(), prompt=b'ECNT> ', slow=0):
        """Play a U-Boot at its prompt: every character typed is echoed, except the ones whose
        place in the stream of typed characters is in `lose` (they are lost, as stock U-Boot
        loses them); Ctrl-C discards the line; a newline runs it. `slow` is how long it takes to
        answer. Returns what was received and the lines that were run."""
        os.write(self.master, prompt)
        self.session.wait(lambda: self.session.at_prompt('stock'), 2, 'prompt')
        self.session.catcher.state = 'confirmed'
        received, ran, stop = bytearray(), [], threading.Event()

        def play():
            line, typed = b'', 0
            while not stop.is_set():
                if not select.select([self.master], [], [], .05)[0]:
                    continue
                time.sleep(slow)
                for value in os.read(self.master, 4096):
                    byte = bytes([value])
                    received.extend(byte)
                    if byte == b'\x03':
                        line = b''
                        os.write(self.master, b'<INTERRUPT>\r\n' + prompt)
                    elif byte in b'\r\n':
                        ran.append(line.decode())
                        os.write(self.master, b'\r\nran ' + line + b'\r\n' + prompt)
                        line = b''
                    else:
                        typed += 1
                        if typed not in lose:
                            line += byte
                            os.write(self.master, byte)
        thread = threading.Thread(target=play)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        return received, ran

    def test_a_command_is_entered_once_its_echo_is_whole(self):
        received, ran = self.unit()
        text = self.session.must_run('version', limit=3)
        self.assertIn('ran version', text)
        self.assertEqual(ran, ['version'])
        self.assertEqual(bytes(received), b'version\n')

    def test_a_line_that_lost_a_character_is_typed_again_not_run(self):
        """`flash read 0x4600000 ...` with a digit lost is another command: it is never run."""
        line = 'nand read 0x90000000 0x4600000 0x20000'
        received, ran = self.unit(lose={22})
        with mock.patch.object(console, 'ECHO_SECONDS', .3):
            text = self.session.must_run(line, limit=5)
        self.assertEqual(ran, [line])
        self.assertEqual(bytes(received), line.encode() + b'\x03' + line.encode() + b'\n')
        self.assertIn('ran ' + line, text)
        self.assertEqual([fields['attempt'] for kind, fields in self.events
                          if kind == 'echo_mismatch'], [1])

    def test_a_console_that_keeps_losing_characters_runs_nothing(self):
        line = 'printenv bootcmd'
        received, ran = self.unit(lose={3, 19, 35})
        with mock.patch.object(console, 'ECHO_SECONDS', .3), \
                self.assertRaisesRegex(RuntimeError, 'nothing was entered'):
            self.session.run(line, limit=5)
        self.assertEqual(ran, [])
        self.assertNotIn(b'\n', bytes(received))
        self.assertNotIn(b'\r', bytes(received))

    def test_the_end_of_the_prompt_is_not_taken_for_echo(self):
        """Seen on a unit: `SDR loader>` in view, its space arriving as the command was typed."""
        received, ran = self.unit(prompt=b'ECNT>', slow=.06)
        late = threading.Timer(.03, os.write, (self.master, b' '))
        late.start()
        self.session.must_run('version', limit=3)
        late.join()
        self.assertEqual(ran, ['version'])
        self.assertEqual([kind for kind, _ in self.events if kind == 'echo_mismatch'], [])

    def test_characters_are_not_sent_in_one_burst(self):
        received, ran = self.unit()
        began = time.monotonic()
        self.session.must_run('x' * 200, limit=5)
        self.assertGreater(time.monotonic() - began, 200 * console.TX_GAP)

    def test_writes_are_refused_unless_the_session_allows_them(self):
        for line in ('saveenv', 'flash erase 0x600000 0x40000', 'ubi write 0 fit 1',
                     'mtd erase system 0 0x20000', 'env save'):
            with self.subTest(line), self.assertRaises(console.Refused):
                self.session.check_command(line)
        for line in ('mtd read.raw.oob.dontskipbad spi-nand0 0x90000000', 'printenv',
                     'setenv bootcmd_orig x', 'hash sha256 0 1', 'tftpput 0 1 x'):
            self.session.check_command(line)

    def test_disconnect_is_reported_and_rx_kept(self):
        os.write(self.master, b'partial boot log\r\n')
        deadline = time.monotonic() + 2
        while not (Path(self.tmp.name) / 'rx.bin').stat().st_size:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        self.close_master()
        deadline = time.monotonic() + 3
        while self.session.failure is None and time.monotonic() < deadline:
            time.sleep(.01)
        with self.assertRaisesRegex(RuntimeError, 'UART reader stopped'):
            self.session.check()
        self.assertEqual((Path(self.tmp.name) / 'rx.bin').read_bytes(), b'partial boot log\r\n')


class PassiveSessionTests(SessionFixture):
    passive = True

    def test_boot_prompts_produce_no_automatic_transmission(self):
        raw = b'U-Boot 2014.04\r\nHit any key to stop autoboot: 4\r\nECNT> '
        os.write(self.master, raw)
        self.assertFalse(select.select([self.master], [], [], .4)[0])
        self.session.close()
        self.assertEqual((Path(self.tmp.name) / 'rx.bin').read_bytes(), raw)
        self.assertFalse(any(kind == 'tx_attempt' for kind, _ in self.events))


if __name__ == '__main__':
    unittest.main()
