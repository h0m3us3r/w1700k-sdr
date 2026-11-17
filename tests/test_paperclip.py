# SPDX-License-Identifier: MIT
"""The paperclip key: one slot for a login key that only a hand on the unit can change.

What is run here: the gestures and the key check (kernel/w1700k_sdr/keyd.h), the LED's say while
the button speaks (ledd.h), the service that watches the button (keyd.c, built for this host and
given presses through a FIFO), the slot's program on the unit (w1700k-keyslot, over a directory
standing in for durable storage), the loader handing the slot to the next boot (the model of
tests/test_loader_update.py), and the host's end (init/key)."""
import base64
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'init'))
sys.path.insert(0, str(ROOT / 'tests'))
from initkit import config, paperclip, provision         # noqa: E402
import test_loader_update                                 # noqa: E402

CC = shutil.which(os.environ.get('CC', 'cc'))
SDR = ROOT / 'kernel' / 'w1700k_sdr'
KEYSLOT = ROOT / 'platform/rootfs/overlay/usr/sbin/w1700k-keyslot'


def ed25519(seed=1):
    """A well-formed Ed25519 public key line (the key itself is arbitrary bytes)."""
    body = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes([seed]) * 32
    return 'ssh-ed25519 ' + base64.b64encode(body).decode()


def rsa(bits=2048):
    body = b'\x00\x00\x00\x07ssh-rsa\x00\x00\x00\x03\x01\x00\x01' + (bits // 8 + 1).to_bytes(4, 'big') + b'\x00' + b'\xa5' * (bits // 8)
    return 'ssh-rsa ' + base64.b64encode(body).decode()


BAD = {
    'cut short': ed25519()[:-9],
    'another type inside': 'ssh-rsa ' + ed25519().split()[1],
    'no body': 'ssh-ed25519',
    'not base64': 'ssh-ed25519 AAAA****',
    'a private key': '-----BEGIN OPENSSH PRIVATE KEY-----',
    'some other line': 'root:x:0:0:root:/root:/bin/sh',
    'an unknown type': 'ssh-dss ' + ed25519().split()[1],
    'an Ed25519 key of another length': 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x10' + b'k' * 16).decode(),
}


@unittest.skipUnless(CC, 'no C compiler')
class Built(unittest.TestCase):
    """The unit's C built for this host."""
    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        cls.check = os.path.join(cls.built.name, 'led_check')
        cls.service = os.path.join(cls.built.name, 'w1700k-key')
        flags = [CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', f'-I{SDR}']
        subprocess.run(flags + [str(ROOT / 'tests/led_check.c'), '-lm', '-o', cls.check], check=True)
        subprocess.run(flags + [str(SDR / 'keyd.c'), '-lm', '-o', cls.service], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.built.cleanup()

    def tool(self, *args):
        return subprocess.run([self.check, *map(str, args)], capture_output=True, text=True, check=True).stdout.strip()


class Keys(Built):
    def test_a_public_key_is_taken_in_the_form_the_slot_holds(self):
        for line in (ed25519(), ed25519() + ' me@host', '  ' + ed25519() + '\r\n', rsa(), rsa(4096)):
            with self.subTest(line=line[:30]):
                kind, body = line.split()[:2]
                self.assertEqual(self.tool('line', line), f'ok {kind}:{body}')
                self.assertEqual(paperclip.public_key(line), (kind, body), 'the host checks it the same way')

    def test_what_is_not_a_public_key_is_refused_by_the_unit_and_by_the_host(self):
        for what, line in BAD.items():
            with self.subTest(what):
                self.assertTrue(self.tool('line', line).startswith('bad '), self.tool('line', line))
                with self.assertRaises(paperclip.KeyProblem):
                    paperclip.public_key(line)
        self.assertIn('private key', self.tool('line', BAD['a private key']))
        self.assertTrue(self.tool('line', ed25519() + '\x1b[2J').startswith('bad '), 'control characters')

    def test_the_slot_form_is_one_word_for_a_shell_and_a_kernel_command_line(self):
        for line in (ed25519(), rsa(4096)):
            slot = self.tool('line', line + ' a comment; rm -rf /').split(' ', 1)[1]
            self.assertRegex(slot, r'^[a-z0-9-]+:[A-Za-z0-9+/=]+$')

    def test_the_install_tools_refuse_a_damaged_key_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'key.pub'
            path.write_text(ed25519() + ' me\n')
            self.assertEqual(provision.authorized_key(path), (ed25519() + ' me\n').encode())
            path.write_text(BAD['cut short'] + '\n')
            with self.assertRaises(provision.ProvisionError):
                provision.authorized_key(path)

    def test_what_a_press_means_is_how_long_it_was_held(self):
        self.assertEqual([self.tool('action', held) for held in (0.2, 4.9, 5.0, 9.9, 10.0, 14.9, 15.0, 40)],
                         ['none', 'none', 'toggle', 'toggle', 'program', 'program', 'purge', 'purge'])

    def test_the_fingerprint_is_the_one_ssh_shows(self):
        keygen = shutil.which('ssh-keygen')
        if not keygen:
            self.skipTest('no ssh-keygen')
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', f'{tmp}/k'], check=True)
            line = Path(tmp, 'k.pub').read_text()
            shown = subprocess.run([keygen, '-lf', f'{tmp}/k.pub'], capture_output=True, text=True, check=True).stdout.split()[1]
            self.assertEqual(paperclip.fingerprint(paperclip.public_key(line)[1]), shown)
            self.assertTrue(self.tool('line', line.strip()).startswith('ok ssh-ed25519:'), 'a real key')


class Led(Built):
    """While the button speaks the LED is its own: (share, hue, saturation, brightness)."""
    def at(self, record, *times):
        rows = [tuple(float(v) for v in row.split()[1:]) for row in self.tool('key', record, *times).splitlines()]
        return rows[0] if len(times) == 1 else rows

    def test_the_first_five_seconds_say_which_way_the_key_will_be_switched(self):
        """In colour and in direction: green rising to switch it on, red sinking to switch it off."""
        times = [k / 4 for k in range(1, 20)]
        rising = self.at('held 0 off', *times)
        self.assertTrue(all((own, h, s) == (1.0, 120.0, 1.0) for own, h, s, _ in rising), 'green, and all of the LED')
        levels = [b for *_, b in rising]
        self.assertEqual(levels, sorted(levels))
        self.assertLess(levels[0], 0.15)
        self.assertGreater(levels[-1], 0.9)
        sinking = self.at('held 0 on', *times)
        self.assertTrue(all((own, h, s) == (1.0, 0.0, 1.0) for own, h, s, _ in sinking), 'red')
        levels = [b for *_, b in sinking]
        self.assertEqual(levels, sorted(levels, reverse=True))
        self.assertGreater(levels[0], 0.9)
        self.assertLess(levels[-1], 0.2)
        self.assertGreater(levels[-1], 0.1, 'never dark: the button is still held')

    def test_with_nothing_in_the_slot_white_rises_for_all_of_ten_seconds(self):
        """Nothing can happen before a new key can go in: no flash at five seconds, no pulse."""
        rows = self.at('held 0 empty', *[k / 4 for k in range(1, 40)])
        self.assertTrue(all(own == 1 and s == 0 for own, _, s, _ in rows))
        levels = [b for *_, b in rows]
        self.assertEqual(levels, sorted(levels))
        self.assertAlmostEqual(self.at('held 0 empty', 5.05)[3], 0.06 + 0.94 * 0.505, places=2, msg='half way, not a flash')
        self.assertGreater(levels[-1], 0.95)
        self.assertEqual(self.at('held 0 empty', 10.05)[2:], (0.0, 1.0), 'the first flash is the one before the blue')
        self.assertEqual(self.at('held 0 empty', 10.6)[1:3], (225.0, 1.0))

    def test_at_five_seconds_a_flash_then_the_colour_of_what_letting_go_would_do(self):
        for slot, hue in (('off', 120), ('on', 0)):
            with self.subTest(slot):
                self.assertEqual(self.at(f'held 0 {slot}', 5.05)[2:], (0.0, 1.0), 'the flash is white')
                own, h, s, b = self.at(f'held 0 {slot}', 5.5)
                self.assertEqual((own, h, s), (1.0, hue, 1.0))
                levels = [row[3] for row in self.at(f'held 0 {slot}', *[5.3 + k / 20 for k in range(32)])]
                self.assertLess(min(levels), 0.4)
                self.assertGreater(max(levels), 0.95, 'it pulses')

    def test_at_ten_seconds_a_flash_then_blue_whatever_the_slot_holds(self):
        for slot in ('on', 'off', 'empty'):
            self.assertEqual(self.at(f'held 0 {slot}', 10.05)[2:], (0.0, 1.0))
            own, h, s, _ = self.at(f'held 0 {slot}', 10.6)
            self.assertEqual((own, h, s), (1.0, 225.0, 1.0))

    def test_at_fifteen_seconds_a_flash_then_red_lit_and_dark(self):
        for slot in ('on', 'off'):
            self.assertEqual(self.at(f'held 0 {slot}', 15.05)[2:], (0.0, 1.0))
            rows = self.at(f'held 0 {slot}', *[15.13 + k / 100 for k in range(90)])
            self.assertTrue(all((own, h, s) == (1.0, 0.0, 1.0) for own, h, s, _ in rows))
            levels = [row[3] for row in rows]
            self.assertEqual(sorted(set(levels)), [0.06, 1.0], 'a blink, not a pulse')
            self.assertEqual(sum(1 for a, b in zip(levels, levels[1:]) if b > a), 2, 'three times a second')
        own, _, s, b = self.at('held 0 empty', 16.0)
        self.assertEqual((own, s), (1.0, 0.0), 'nothing to erase: no colour')

    def test_the_colours_of_the_hold_are_green_red_and_blue_and_nothing_between(self):
        hues = {row[1] for slot in ('on', 'off') for row in self.at(f'held 0 {slot}', *[5.3 + k / 10 for k in range(150)])
                if row[2] == 1.0}
        self.assertEqual(hues, {0.0, 120.0, 225.0})

    def test_erased_the_red_pales_as_it_sinks(self):
        rows = self.at('purged 0', *[k / 10 for k in range(18)])
        self.assertEqual(rows[0][1:], (0.0, 1.0, 1.0), 'red, full')
        self.assertEqual([row[2] for row in rows], sorted((row[2] for row in rows), reverse=True))
        self.assertEqual([row[3] for row in rows], sorted((row[3] for row in rows), reverse=True))
        self.assertEqual(rows[-1][3], 0.0)
        self.assertEqual(self.at('purged 0', 3.0)[0], 0.0)

    def test_letting_go_early_runs_the_fill_out_and_gives_the_led_back(self):
        for slot, hue, level in (('off', 120, 0.06 + 0.94 * 3 / 5), ('on', 0, 1 - 0.88 * 3 / 5), ('empty', 0, 0.06 + 0.94 * 3 / 10)):
            rows = self.at(f'drain 0 3.000 {slot}', 0.0, 0.1, 0.3, 0.6)
            self.assertAlmostEqual(rows[0][3], level, places=2, msg='from where it stood')
            self.assertEqual(rows[0][1:3], (hue, 0.0 if slot == 'empty' else 1.0), 'in the colour it had')
            self.assertEqual([row[3] for row in rows], sorted((row[3] for row in rows), reverse=True))
            self.assertEqual(rows[-1][0], 0.0)

    def test_every_flourish_ends_and_gives_the_led_back(self):
        for record, colour in (('on 0', 120), ('off 0', 0), ('taken 0', 120), ('failed 0', 0), ('closed 0', 225)):
            with self.subTest(record):
                self.assertEqual(self.at(record, 0.5)[0], 1.0)
                self.assertEqual(self.at(record, 3.0)[0], 0.0)
                self.assertEqual(self.at(record, 1.5 if record != 'closed 0' else 0.3)[1], colour)
        self.assertEqual(self.at('empty 0', 1.0)[0], 0.0)

    def test_switched_on_rises_twice_and_off_sinks_into_the_dark(self):
        on = [row[3] for row in self.at('on 0', *[k / 40 for k in range(40)])]
        peaks = [k for k in range(1, 39) if on[k] > on[k - 1] and on[k] >= on[k + 1] and on[k] > 0.9]
        self.assertEqual(len(peaks), 3, 'up, up, and up to stay')
        off = [row[3] for row in self.at('off 0', *[k / 10 for k in range(16)])]
        self.assertEqual(off, sorted(off, reverse=True))
        self.assertEqual((off[0], off[-1]), (1.0, 0.0))

    def test_waiting_for_a_key_breathes_blue_until_it_is_over(self):
        rows = self.at('window 0', *[k / 5 for k in range(40)])
        self.assertTrue(all((own, h, s) == (1.0, 225.0, 1.0) for own, h, s, _ in rows))
        self.assertLess(min(row[3] for row in rows), 0.3)
        self.assertEqual(self.at('window 0', 179.0)[0], 1.0)
        self.assertEqual(self.at('window 0', 190.0)[0], 0.0, 'a record nobody ended')

    def test_a_key_taken_turns_the_blue_through_cyan_to_green(self):
        self.assertEqual(self.at('taken 0', 0.05)[2:], (0.0, 1.0), 'a flash first')
        hues = [row[1] for row in self.at('taken 0', *[0.15 + k / 20 for k in range(22)])]
        self.assertEqual(hues, sorted(hues, reverse=True))
        self.assertLessEqual(hues[0], 225)
        self.assertEqual(hues[-1], 120.0)
        self.assertTrue(any(170 < hue < 190 for hue in hues), 'by way of cyan, not of red')

    def test_a_button_down_for_a_minute_says_nothing_more(self):
        self.assertEqual(self.at('held 0 on', 30.0)[0], 1.0)
        self.assertEqual(self.at('held 0 on', 61.0)[0], 0.0)

    def test_no_record_or_one_not_understood_leaves_the_led_alone(self):
        for record in ('none 0 -', 'gibberish', '', 'held'):
            self.assertEqual(self.at(record or ' ', 1.0)[0], 0.0)


class Service(Built):
    """w1700k-key with presses from a FIFO, short times, and a slot program that only takes notes."""
    TOGGLE, PROGRAM, PURGE, WINDOW = 0.3, 0.7, 1.2, 2.5

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        os.mkfifo(self.dir / 'buttons')
        (self.dir / 'slot-state').write_text('empty 1\n')
        slot = self.dir / 'slot'
        slot.write_text(f'''#!/bin/sh
d={self.dir}
echo "$1" >> $d/calls
[ ! -e $d/slot-fails ] || exit 1
case $1 in
toggle) case $(cut -d' ' -f1 $d/slot-state) in on) new=off ;; off) new=on ;; *) new=empty ;; esac
        echo "$new 1" > $d/slot-state; echo $new ;;
purge) case $(cut -d' ' -f1 $d/slot-state) in empty) echo empty ;; *) echo "empty 1" > $d/slot-state; echo purged ;; esac ;;
program) cat > $d/programmed; echo "on 1" > $d/slot-state; echo "persist=1 hostkey=ssh-ed25519:HOSTKEY" ;;
esac
''')
        slot.chmod(0o755)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.service_process = subprocess.Popen(
            [self.service, '--buttons', str(self.dir / 'buttons'), '--state', str(self.dir / 'state'),
             '--slot-state', str(self.dir / 'slot-state'), '--slot', str(slot), '--listen', '127.0.0.1',
             '--port', str(self.port), '--toggle', str(self.TOGGLE), '--program', str(self.PROGRAM),
             '--purge', str(self.PURGE), '--window', str(self.WINDOW)], stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        self.buttons = open(self.dir / 'buttons', 'w')
        self.addCleanup(self.buttons.close)
        self.phase('none')

    def stop(self):
        self.service_process.terminate()
        self.service_process.wait(5)
        self.service_process.stderr.close()

    def phase(self, wanted, within=3.0):
        """Wait for the record to say `wanted`; the whole record."""
        until = time.monotonic() + within
        words = []
        while time.monotonic() < until:
            try:
                words = (self.dir / 'state').read_text().split()
            except FileNotFoundError:
                words = []
            if words and words[0] == wanted:
                return words
            time.sleep(0.01)
        self.fail(f'the record never said {wanted}: {words}')

    def press(self, seconds):
        self.buttons.write('d'); self.buttons.flush()
        time.sleep(seconds)
        self.buttons.write('u'); self.buttons.flush()

    def calls(self):
        path = self.dir / 'calls'
        return path.read_text().split() if path.exists() else []

    def offer(self, line):
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as link:
            link.sendall(line.encode() + b'\n')
            return link.makefile().readline().strip()

    def open_slot(self):
        self.press(self.PROGRAM + 0.15)
        self.phase('window')

    def test_a_short_press_means_nothing(self):
        (self.dir / 'slot-state').write_text('off 1\n')
        self.press(0.1)
        record = self.phase('drain')
        self.assertAlmostEqual(float(record[2]), 0.1, delta=0.08)
        self.assertEqual(record[3], 'off', 'the slot, for the colour of what runs out')
        self.assertEqual(self.calls(), [])

    def test_while_held_the_record_says_since_when_and_what_the_slot_is(self):
        (self.dir / 'slot-state').write_text('on 1\n')
        before = time.monotonic()
        self.buttons.write('d'); self.buttons.flush()
        record = self.phase('held')
        self.assertEqual(record[2], 'on')
        self.assertAlmostEqual(float(record[1]), before, delta=0.3)
        self.buttons.write('u'); self.buttons.flush()
        self.phase('drain')

    def test_five_seconds_switch_the_key_off_and_on_again(self):
        (self.dir / 'slot-state').write_text('on 1\n')
        self.press(self.TOGGLE + 0.15)
        self.phase('off')
        self.press(self.TOGGLE + 0.15)
        self.phase('on')
        self.assertEqual(self.calls(), ['toggle', 'toggle'])

    def test_an_empty_slot_has_nothing_to_switch(self):
        self.press(self.TOGGLE + 0.15)
        record = self.phase('drain')
        self.assertAlmostEqual(float(record[2]), self.TOGGLE + 0.15, delta=0.1)
        self.assertEqual(record[3], 'empty', 'what had filled runs out, from where it stood')
        self.assertEqual(self.calls(), ['toggle'], 'the slot itself was asked: this boot may not know of its key')

    def test_fifteen_seconds_erase_the_key(self):
        (self.dir / 'slot-state').write_text('off 1\n')
        self.press(self.PURGE + 0.15)
        self.phase('purged')
        self.assertEqual(self.calls(), ['purge'])
        self.assertEqual((self.dir / 'slot-state').read_text(), 'empty 1\n')
        with self.assertRaises(OSError, msg='held past programming: the slot was never opened'):
            socket.create_connection(('127.0.0.1', self.port), timeout=1)
        self.press(self.PURGE + 0.15)
        self.phase('empty')

    def test_ten_seconds_open_the_slot_and_a_key_goes_in(self):
        with self.assertRaises(OSError, msg='closed until the button says so'):
            socket.create_connection(('127.0.0.1', self.port), timeout=1)
        self.open_slot()
        self.assertEqual(self.offer(ed25519(7) + ' me@host'), 'ok persist=1 hostkey=ssh-ed25519:HOSTKEY')
        self.phase('taken')
        kind, body = ed25519(7).split()
        self.assertEqual((self.dir / 'programmed').read_text(), f'{kind}:{body}\n')
        with self.assertRaises(OSError, msg='one key, and the slot is closed again'):
            socket.create_connection(('127.0.0.1', self.port), timeout=1)

    def test_what_is_not_a_key_is_refused_and_the_wait_goes_on(self):
        self.open_slot()
        answer = self.offer(BAD['cut short'])
        self.assertTrue(answer.startswith('error: not a public key'), answer)
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.offer(ed25519()).startswith('ok '))
        self.phase('taken')

    def test_five_lines_that_are_not_keys_end_the_wait(self):
        self.open_slot()
        for _ in range(5):
            self.assertTrue(self.offer('nonsense').startswith('error:'))
        self.phase('failed')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=1)

    def test_a_press_ends_the_wait_and_means_no_more(self):
        self.open_slot()
        self.press(self.TOGGLE + 0.15)
        self.phase('closed')
        time.sleep(0.2)
        self.assertEqual(self.calls(), [], 'that press switched nothing')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=1)
        self.press(0.1)
        self.phase('drain')

    def test_the_wait_ends_by_itself(self):
        self.open_slot()
        self.phase('closed', within=self.WINDOW + 2)
        self.assertEqual(self.calls(), [])

    def test_a_key_the_slot_cannot_keep_is_said_to_the_host_and_on_the_led(self):
        (self.dir / 'slot-fails').touch()
        self.open_slot()
        self.assertTrue(self.offer(ed25519()).startswith('error: the unit could not keep the key'))
        self.phase('failed')
        self.press(self.TOGGLE + 0.15)
        self.phase('failed')

    def test_usage(self):
        for arguments in (['--nonsense', '1'], ['--toggle', '5', '--program', '3'], ['--purge', '8'], ['--port']):
            result = subprocess.run([self.service, *arguments], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('usage', result.stderr)


class Slot(unittest.TestCase):
    """w1700k-keyslot as the unit runs it, with a directory for durable storage."""
    BUILT_IN = ed25519(9) + ' installer'

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / 'durable').mkdir()
        (self.dir / 'cmdline').write_text('console=ttyS0,115200 ro\n')
        (self.dir / 'host.pub').write_text('ssh-ed25519 HOSTBODY root@w1700k\n')
        run = self.dir / 'durable-run'
        run.write_text(f'#!/bin/sh\n[ ! -e {self.dir}/durable-busy ] || {{ echo "durable-open: durable is already open" >&2; exit 1; }}\n'
                       'echo "durable-open: /durable mounted from mtd6"\n"$@"\nstatus=$?\n'
                       'echo "durable-close: /durable closed"\nexit $status\n')
        run.chmod(0o755)
        self.env = dict(os.environ, W1700K_KEYSLOT_RUN=str(self.dir / 'run'), W1700K_KEYSLOT_KEYS=str(self.dir / 'root/.ssh/authorized_keys'),
                        W1700K_KEYSLOT_BUILT_IN=str(self.dir / 'built-in'), W1700K_KEYSLOT_CMDLINE=str(self.dir / 'cmdline'),
                        W1700K_KEYSLOT_HOST_KEY=str(self.dir / 'host.pub'), W1700K_KEYSLOT_DURABLE_RUN=str(run),
                        W1700K_KEYSLOT_DIR=str(self.dir / 'durable/key'))

    def keyslot(self, verb, stdin='', ok=True):
        result = subprocess.run(['sh', str(KEYSLOT), verb], input=stdin, capture_output=True, text=True, env=self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def slot_form(self, line):
        kind, body = line.split()[:2]
        return f'{kind}:{body}'

    def keys(self):
        return (self.dir / 'root/.ssh/authorized_keys').read_text().splitlines()

    def record(self):
        return (self.dir / 'run').read_text().strip()

    def boot(self, *words):
        (self.dir / 'cmdline').write_text(' '.join(('console=ttyS0,115200 ro',) + words) + '\n')
        return self.keyslot('boot')

    def test_a_boot_under_a_loader_that_says_nothing(self):
        self.boot()
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.record(), 'empty 0')
        self.assertEqual(oct((self.dir / 'root/.ssh').stat().st_mode & 0o777), '0o700')
        self.assertEqual(oct((self.dir / 'root/.ssh/authorized_keys').stat().st_mode & 0o777), '0o600')

    def test_the_key_the_loader_hands_over_is_a_login_key_beside_the_built_in_one(self):
        (self.dir / 'built-in').write_text(self.BUILT_IN + '\n')
        self.boot('w1700k.keyslot=on', 'w1700k.key=' + self.slot_form(ed25519(3)))
        self.assertEqual(self.keys(), [self.BUILT_IN, ed25519(3) + ' paperclip'])
        self.assertEqual(self.record(), 'on 1')

    def test_a_slot_that_is_off_or_empty_gives_no_key(self):
        (self.dir / 'built-in').write_text(self.BUILT_IN + '\n')
        for state in ('off', 'empty'):
            self.boot(f'w1700k.keyslot={state}')
            self.assertEqual(self.keys(), [self.BUILT_IN])
            self.assertEqual(self.record(), f'{state} 1')

    def test_a_command_line_is_not_trusted_to_be_a_key(self):
        for key in ('', 'ssh-ed25519:AAAA$(reboot)', 'ssh-ed25519:AAAA;reboot', 'root::0:0', 'ssh-ed25519:AAAA:more', 'nothing'):
            with self.subTest(key):
                (self.dir / 'cmdline').write_text(f'ro w1700k.keyslot=on w1700k.key={key}\n')
                self.keyslot('boot')
                self.assertEqual(self.keys(), [])
                self.assertEqual(self.record(), 'off 1')

    def test_programming_keeps_the_key_in_the_form_the_loader_imports(self):
        self.boot('w1700k.keyslot=empty')
        result = self.keyslot('program', self.slot_form(ed25519(5)) + '\n')
        self.assertEqual(result.stdout.strip(), 'persist=1 hostkey=ssh-ed25519:HOSTBODY')
        self.assertEqual((self.dir / 'durable/key/slot').read_text(),
                         f'key_line={self.slot_form(ed25519(5))}\nkey_on=1\n')
        self.assertEqual(self.keys(), [ed25519(5) + ' paperclip'], 'and it works at once')
        self.assertEqual(self.record(), 'on 1')
        self.assertEqual(os.listdir(self.dir / 'durable/key'), ['slot'])

    def test_a_new_key_replaces_the_one_in_the_slot(self):
        self.boot('w1700k.keyslot=empty')
        self.keyslot('program', self.slot_form(ed25519(5)) + '\n')
        self.keyslot('program', self.slot_form(rsa()) + '\n')
        self.assertEqual(self.keys(), [rsa() + ' paperclip'])
        self.assertIn(self.slot_form(rsa()), (self.dir / 'durable/key/slot').read_text())
        self.assertNotIn(ed25519(5).split()[1], (self.dir / 'durable/key/slot').read_text())

    def test_what_is_not_a_key_never_reaches_the_slot(self):
        self.boot()
        for text in ('', 'ssh-ed25519 AAAA\n', 'ssh-ed25519:AAAA; reboot\n', 'ssh-ed25519:\n', 'key_on=1\n'):
            with self.subTest(text):
                self.assertNotEqual(self.keyslot('program', text, ok=False).returncode, 0)
                self.assertFalse((self.dir / 'durable/key').exists())
                self.assertEqual(self.keys(), [])

    def test_toggling_switches_the_key_off_and_on_and_keeps_it(self):
        self.boot('w1700k.keyslot=empty')
        self.keyslot('program', self.slot_form(ed25519(5)) + '\n')
        self.assertEqual(self.keyslot('toggle').stdout.strip(), 'off')
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.record(), 'off 1')
        self.assertEqual((self.dir / 'durable/key/slot').read_text(), f'key_line={self.slot_form(ed25519(5))}\nkey_on=0\n')
        self.assertEqual(self.keyslot('toggle').stdout.strip(), 'on')
        self.assertEqual(self.keys(), [ed25519(5) + ' paperclip'])
        self.assertEqual(self.record(), 'on 1')

    def test_purging_erases_the_key_from_the_slot_and_from_the_logins(self):
        (self.dir / 'built-in').write_text(self.BUILT_IN + '\n')
        self.boot('w1700k.keyslot=empty')
        self.keyslot('program', self.slot_form(ed25519(5)) + '\n')
        self.assertEqual(self.keyslot('purge').stdout.strip(), 'purged')
        self.assertEqual(os.listdir(self.dir / 'durable/key'), [])
        self.assertEqual(self.keys(), [self.BUILT_IN], 'the key built into the root is not the slot\'s to erase')
        self.assertEqual(self.record(), 'empty 1')
        self.assertEqual(self.keyslot('purge').stdout.strip(), 'empty')
        self.assertEqual(self.keyslot('toggle').stdout.strip(), 'empty', 'nothing left to switch on')

    def test_a_key_that_is_off_is_erased_as_well(self):
        self.boot('w1700k.keyslot=empty')
        self.keyslot('program', self.slot_form(ed25519(5)) + '\n')
        self.keyslot('toggle')
        self.assertEqual(self.keyslot('purge').stdout.strip(), 'purged')
        self.assertFalse((self.dir / 'durable/key/slot').exists())

    def test_toggling_an_empty_slot_says_so_and_writes_nothing(self):
        self.boot('w1700k.keyslot=empty')
        self.assertEqual(self.keyslot('toggle').stdout.strip(), 'empty')
        self.assertFalse((self.dir / 'durable/key').exists())

    def test_toggling_goes_by_the_slot_not_by_what_this_boot_was_told(self):
        """Under a loader that does not hand the slot over, the record says empty; the slot knows."""
        self.boot()
        self.keyslot('program', self.slot_form(ed25519(5)) + '\n')
        self.boot()
        self.assertEqual(self.record(), 'empty 0')
        self.assertEqual(self.keyslot('toggle').stdout.strip(), 'off')
        self.assertEqual(self.keyslot('toggle').stdout.strip(), 'on')
        self.assertEqual(self.keys(), [ed25519(5) + ' paperclip'])
        self.assertEqual(self.record(), 'on 0')

    def test_storage_in_use_fails_and_changes_nothing(self):
        self.boot('w1700k.keyslot=on', 'w1700k.key=' + self.slot_form(ed25519(3)))
        (self.dir / 'durable-busy').touch()
        self.assertNotEqual(self.keyslot('program', self.slot_form(ed25519(5)) + '\n', ok=False).returncode, 0)
        self.assertNotEqual(self.keyslot('toggle', ok=False).returncode, 0)
        self.assertNotEqual(self.keyslot('purge', ok=False).returncode, 0)
        self.assertEqual(self.keys(), [ed25519(3) + ' paperclip'])
        self.assertEqual(self.record(), 'on 1')

    def test_status_and_usage(self):
        self.boot('w1700k.keyslot=off')
        self.assertEqual(self.keyslot('status').stdout.split(), ['slot=off', 'persist=1'])
        self.assertEqual(self.keyslot('nonsense', ok=False).returncode, 2)


class AcrossAReboot(test_loader_update.Model):
    """The whole way round: the slot's program writes the slot, the loader reads it at the next
    boot, and the slot's program makes root's login keys of what the loader handed over."""
    def setUp(self):
        super().setUp()
        self.slot = Slot('test_status_and_usage')
        self.slot.setUp()
        self.addCleanup(self.slot.doCleanups)
        self.slot.env['W1700K_KEYSLOT_DIR'] = str(self.model / 'durable/key')

    def reboot(self):
        handed = self.model / 'bootargs'
        handed.unlink(missing_ok=True)
        self.boot()
        (self.slot.dir / 'cmdline').write_text(handed.read_text())
        self.slot.keyslot('boot')

    def test_a_programmed_key_is_there_after_a_reboot_and_a_switched_off_one_is_not(self):
        self.reboot()
        self.assertEqual((self.slot.keys(), self.slot.record()), ([], 'empty 1'))
        self.slot.keyslot('program', self.slot.slot_form(ed25519(5)) + '\n')
        self.reboot()
        self.assertEqual((self.slot.keys(), self.slot.record()), ([ed25519(5) + ' paperclip'], 'on 1'))
        self.slot.keyslot('toggle')
        self.reboot()
        self.assertEqual((self.slot.keys(), self.slot.record()), ([], 'off 1'))
        self.slot.keyslot('toggle')
        self.reboot()
        self.assertEqual((self.slot.keys(), self.slot.record()), ([ed25519(5) + ' paperclip'], 'on 1'))
        self.slot.keyslot('purge')
        self.reboot()
        self.assertEqual((self.slot.keys(), self.slot.record()), ([], 'empty 1'))

    def test_the_longest_key_the_unit_takes_survives_the_way(self):
        self.slot.keyslot('boot')
        self.slot.keyslot('program', self.slot.slot_form(rsa(4096)) + '\n')
        self.reboot()
        self.assertEqual(self.slot.keys(), [rsa(4096) + ' paperclip'])


@unittest.skipUnless((config.SQUASHFS_TOOLS / 'unsquashfs').exists() and config.ROOTFS.exists(),
                     'needs the built root and squashfs tools (make)')
class NoLoginKey(unittest.TestCase):
    """init/install, init/flash and init/provision --no-login-key: a unit's root with no key
    built in, for a unit that takes its key from its reset button alone. Not the default."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / 'k.pub').write_text(ed25519(4) + ' installer\n')
        self.said = []

    def root(self, **options):
        image, _ = provision.provision(self.dir / 'unit', self.dir / 'k.pub', say=self.said.append, **options)
        listing = subprocess.run([str(config.SQUASHFS_TOOLS / 'unsquashfs'), '-l', str(image)],
                                 capture_output=True, text=True, check=True).stdout
        return {line.split('squashfs-root/', 1)[1] for line in listing.splitlines() if 'squashfs-root/' in line}

    def test_by_default_the_installers_key_is_built_in(self):
        self.assertIn('root/.ssh/authorized_keys', self.root())
        self.assertEqual((self.dir / 'unit/login-key').read_text().strip(), paperclip.fingerprint(ed25519(4).split()[1]))

    def test_with_the_option_the_root_has_no_login_key_and_everything_else(self):
        with_key, without = self.root(), self.root(no_login_key=True)
        self.assertEqual(with_key - without, {'root/.ssh/authorized_keys', 'root/.ssh'})
        self.assertIn('etc/dropbear/dropbear_ed25519_host_key', without, 'the unit still has its host key')
        self.assertIn('usr/sbin/w1700k-key', without, 'and the button that gives it a login key')
        self.assertEqual((self.dir / 'unit/login-key').read_text().strip(), 'none')
        self.assertEqual(self.said, [])

    def test_no_key_file_is_needed_for_it(self):
        (self.dir / 'k.pub').unlink()
        self.assertNotIn('root/.ssh/authorized_keys', self.root(no_login_key=True))

    def test_a_key_built_in_after_a_root_without_one_is_said(self):
        self.root(no_login_key=True)
        self.root(no_login_key=True)
        self.assertEqual(self.said, [])
        self.root()
        self.assertEqual(len(self.said), 1)
        self.assertIn('the last one made for this unit had none', self.said[0])
        self.root()
        self.assertEqual(len(self.said), 1, 'said once, when it changes')


class Unit:
    """A unit waiting for a key on a local port: answers each line offered with the next answer."""
    def __init__(self, *answers):
        self.answers, self.offered = list(answers), []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        for answer in self.answers:
            try:
                link, _ = self.listener.accept()
            except OSError:
                return
            with link:
                self.offered.append(link.makefile().readline())
                link.sendall(answer.encode() + b'\n')

    def close(self):
        self.listener.close()


class Host(unittest.TestCase):
    """init/key against a unit that waits (or does not)."""
    HOST = 'hostkey=' + ed25519(8).replace(' ', ':')

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / 'k.pub').write_text(ed25519(2) + ' me@host\n')
        (self.dir / 'k').write_text('private\n')
        self.said, self.ssh = [], []
        self.login_works = True

    def unit(self, *answers):
        unit = Unit(*answers)
        self.addCleanup(unit.close)
        return unit

    def ssh_run(self, command, **options):
        self.ssh.append(command)
        known = [o.split('=', 1)[1] for o in command if o.startswith('UserKnownHostsFile=')][0]
        self.known = Path(known).read_text()
        return subprocess.CompletedProcess(command, 0 if self.login_works else 255, '', '' if self.login_works else 'Permission denied (publickey).')

    def send(self, unit, key='k.pub', **options):
        return paperclip.run('127.0.0.1', self.dir / key, port=unit.port, say=self.said.append, run=self.ssh_run,
                             **options)

    def test_the_key_is_sent_as_its_type_and_body_and_the_login_is_tried_against_the_units_own_host_key(self):
        unit = self.unit('ok persist=1 ' + self.HOST)
        self.assertEqual(self.send(unit), 0)
        self.assertEqual(unit.offered, [ed25519(2) + '\n'], 'no comment, nothing else')
        self.assertEqual(self.known, f'127.0.0.1 {ed25519(8)}\n')
        command = self.ssh[0]
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('GlobalKnownHostsFile=/dev/null', command)
        self.assertEqual(command[command.index('-i') + 1], str(self.dir / 'k'))
        self.assertEqual(command[-2:], ['root@127.0.0.1', 'true'])
        text = '\n'.join(self.said)
        self.assertIn(paperclip.fingerprint(ed25519(2).split()[1]), text, 'which key went')
        self.assertIn(paperclip.fingerprint(ed25519(8).split()[1]), text, "the unit's host key, to compare")
        self.assertIn('login works', text)

    def test_a_key_that_is_not_one_is_never_sent(self):
        unit = self.unit('ok persist=1 ' + self.HOST)
        for what, line in BAD.items():
            (self.dir / 'bad.pub').write_text(line + '\n')
            with self.subTest(what), self.assertRaises(paperclip.KeyProblem):
                self.send(unit, 'bad.pub')
        with self.assertRaises(paperclip.KeyProblem):
            self.send(unit, 'absent.pub')
        self.assertEqual(unit.offered, [])

    def test_the_units_refusal_is_reported_in_its_words(self):
        unit = self.unit('error: not a public key: its body is not base64 (cut short?)')
        with self.assertRaisesRegex(paperclip.KeyProblem, 'did not take the key: not a public key: its body is not base64'):
            self.send(unit)
        self.assertEqual(self.ssh, [])

    def test_a_unit_that_never_opens_its_slot(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        clock = iter(range(0, 1000, 10))
        with self.assertRaisesRegex(paperclip.KeyProblem, 'did not open its slot.*Hold the reset button'):
            paperclip.run('127.0.0.1', self.dir / 'k.pub', port=port, wait=30, say=self.said.append,
                          clock=lambda: next(clock), sleep=lambda seconds: None)
        self.assertEqual(sum('Hold its reset button' in line for line in self.said), 1, 'said once what to do')

    def test_it_waits_for_the_button(self):
        unit = self.unit('ok persist=1 ' + self.HOST)
        attempts = []

        def connect(address, timeout):
            attempts.append(address)
            if len(attempts) < 4:
                raise ConnectionRefusedError('refused')
            return socket.create_connection(address, timeout=timeout)
        self.assertEqual(self.send(unit, connect=connect, sleep=lambda seconds: None), 0)
        self.assertEqual(len(attempts), 4)

    def test_a_loader_that_does_not_keep_the_key_is_said(self):
        unit = self.unit('ok persist=0 ' + self.HOST)
        self.assertEqual(self.send(unit), 0)
        self.assertIn('until the unit restarts', '\n'.join(self.said))

    def test_a_key_taken_that_does_not_log_in_is_a_failure(self):
        unit = self.unit('ok persist=1 ' + self.HOST)
        self.login_works = False
        self.assertEqual(self.send(unit), 1)
        self.assertIn('Permission denied', '\n'.join(self.said))

    def test_the_host_key_is_kept_where_asked_once(self):
        known = self.dir / 'known/hosts'
        for _ in range(2):
            unit = self.unit('ok persist=1 ' + self.HOST)
            self.assertEqual(self.send(unit, known_hosts=known), 0)
        self.assertEqual(known.read_text(), f'127.0.0.1 {ed25519(8)}\n')

    def test_a_host_key_that_is_not_a_key_is_not_trusted(self):
        unit = self.unit('ok persist=1 hostkey=ssh-ed25519:AAAA')
        with self.assertRaises(paperclip.KeyProblem):
            self.send(unit)
        self.assertEqual(self.ssh, [])


if __name__ == '__main__':
    unittest.main()
