# SPDX-License-Identifier: MIT
"""The guard on a host's raw programs (firmware/stream/guard.h), built for this host as it is: which
ops a locked WM does not run. What is on the list lets the radio transmit, programs an eFuse, or
is an access the headers mark HAZARD; what the host's own flows need is not on it."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CC = shutil.which(os.environ.get('CC', 'cc'))
TOP = 15                                        # an A-die's TOP bank


def areg(die, k, index):
    return die << 16 | k << 12 | index


@unittest.skipUnless(CC, 'a native C compiler (cc) is needed')
class Guard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tool = Path(cls.tmp.name) / 'guard_check'
        subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', f'-I{ROOT / "include"}',
                        f'-I{ROOT / "firmware/stream"}', '-o', str(cls.tool), str(ROOT / 'tests/guard_check.c')], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def judge(self, *ops):
        """For each (name, a, b, c): True when the guard denies it."""
        words = [word if n == 0 else f'{word:x}' for op in ops for n, word in enumerate((*op, 0, 0, 0)[:4])]
        out = subprocess.run([str(self.tool), *words], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(len(out), len(ops))
        return [line == 'denied' for line in out]

    def denied(self, *ops):
        for op, verdict in zip(ops, self.judge(*ops)):
            self.assertTrue(verdict, f'ran: {op[0]} ' + ' '.join(f'{w:#x}' for w in op[1:]))

    def run_(self, *ops):
        for op, verdict in zip(ops, self.judge(*ops)):
            self.assertFalse(verdict, f'denied: {op[0]} ' + ' '.join(f'{w:#x}' for w in op[1:]))

    def test_the_efuse_controller_is_neither_written_nor_read(self):
        self.denied(('W32', 0x70021008, 0x400000C0), ('RMW32', 0x70021008, 0xFF, 0xC0), ('R32', 0x70021008),
                    ('POLL', 0x70021000, 1, 1), ('R32', 0x70021FFC), ('W32', 0x7002100A, 0))
        self.run_(('R32', 0x70020FFC), ('R32', 0x70022000), ('W32', 0x70022000, 1))

    def test_no_semaphore_is_touched_a_read_takes_it(self):
        self.denied(('R32', 0x7C070030), ('POLL', 0x7C070030, 1, 1), ('W32', 0x7C070230, 1), ('R32', 0x7C070000),
                    ('R32', 0x7C0703FC))
        self.run_(('R32', 0x7C070400), ('R32', 0x7C06FFFC))

    def test_the_macs_transmit_disable_is_the_radio_codes_alone(self):
        for arb in (0x820E3000, 0x820F3000, 0x830E3000):
            self.denied(('W32', arb, 0xF0007000), ('W32', arb, 0xF0007300), ('RMW32', arb, 0x100, 0), ('RMW32', arb, 0x200, 0))
            self.run_(('R32', arb), ('POLL', arb, 0x100, 0x100), ('W32', arb + 4, 0), ('W32', arb - 4, 0))

    def test_no_capture_route_grant_is_driven(self):
        self.denied(('W32', 0x88009000, 1), ('W32', 0x88009008, 0x80000000), ('W32', 0x88009004, 1),
                    ('RMW32', 0x88009000, 0, 0x10), ('RMW32', 0x88009004, 0xFF, 0x00800000))
        self.run_(('W32', 0x88009000, 0), ('W32', 0x88009004, 0x01000000), ('W32', 0x88009004, 0xFF000000),
                  ('RMW32', 0x88009004, 0xFF000000, 0x03000000), ('W32', 0x8800900C, 0xFFFFFFFF), ('R32', 0x88009000))

    def test_no_chain_is_given_a_transmit_block(self):
        for die in range(3):
            for k in range(5):
                enable = areg(die, k, 0x010)
                self.denied(('AW', enable, 0x01FA23C5), ('AW', enable, 0x0003FBCB | 4), ('AW', enable, 1 << 19),
                            ('AW', enable, 1 << 28), ('AW', enable, 1 << 29), ('ARMW', enable, 0, 1 << 24))
                self.run_(('AW', enable, 0x0003FBCB), ('AW', enable, 0x8003FBCB), ('AW', enable, 0x000023C1), ('AW', enable, 0),
                          ('ARMW', enable, 0x01F80004, 0), ('AR', enable), ('AW', areg(die, k, 0x011), 0x80000000))

    def test_the_front_ends_pins_and_the_words_that_lock_a_die_are_not_written(self):
        for die in range(3):
            self.denied(('AW', areg(die, TOP, 0x490), 1), ('AW', areg(die, TOP, 0x4A4), 0x10000), ('AW', areg(die, TOP, 0x4A6), 0),
                        ('ARMW', areg(die, TOP, 0x49C), 0xF, 1), ('AW', areg(die, TOP, 0x744), 0),
                        ('AW', areg(die, TOP, 0x060), 0), ('AW', areg(die, TOP, 0x664), 0), ('ARMW', areg(die, TOP, 0x060), 0xFFFFFFFF, 0))
            self.run_(('AW', areg(die, TOP, 0x48F), 1), ('AW', areg(die, TOP, 0x4A7), 1), ('AW', areg(die, TOP, 0x060), 0x06002010),
                      ('AW', areg(die, TOP, 0x664), 0xFFA428AF), ('AR', areg(die, TOP, 0x4A4)), ('AR', areg(die, TOP, 0x060)),
                      ('AW', areg(die, 0, 0x060), 0), ('AW', areg(die, TOP, 0x061), 0))

    def test_the_reads_that_wedge_a_die_are_not_made(self):
        for die in range(3):
            self.denied(('AR', areg(die, TOP, 0x222)), ('AR', areg(die, TOP, 0x200)), ('AR', areg(die, TOP, 0x2FF)),
                        ('APOLL', areg(die, TOP, 0x222), 1, 1), ('AR', areg(die, 1, 0x5AA)))
            self.run_(('AR', areg(die, TOP, 0x1FF)), ('AR', areg(die, TOP, 0x300)), ('AR', areg(die, 0, 0x5AA)),
                      ('AR', areg(die, 2, 0x5AA)), ('AR', areg(die, TOP, 0x000)), ('AW', areg(die, TOP, 0x222), 0))

    def test_no_raw_chip_select_is_reached(self):
        for k in range(8, 15):                                  # chip-select k - 8: "BT" is 2, "GPS" 4
            self.denied(('AR', areg(0, k, 0)), ('AW', areg(1, k, 0x10), 0), ('ARMW', areg(2, k, 0), 1, 1), ('APOLL', areg(0, k, 0), 1, 1))
        self.run_(('AR', areg(0, 4, 0)), ('AR', areg(0, TOP, 0)), ('AR', areg(0, 5, 0)), ('AR', areg(0, 7, 0)))

    def test_what_the_hosts_own_flows_post_is_run(self):
        """The EEPROM image into our WM's buffer and back (wm.py put_eeprom), a look at the mailbox
        page, and the ops an RF command is made of when a tool posts them itself."""
        self.run_(('W32', 0xE0100000, 0x12345678), ('R32', 0xE0100000), ('R32', 0x0090F300), ('W32', 0x0090F400, 1),
                  ('SAR', 1), ('DELAY', 0), ('AR', areg(1, 0, 0x004)), ('AW', areg(1, 0, 0x050), 0x1234),
                  ('ARMW', areg(0, 3, 0x100), 0xFF, 0x12), ('APOLL', areg(2, 4, 0x003), 1, 1), ('R32', 0x18060010),
                  ('POLL', 0x820E0000, 1, 1))


if __name__ == '__main__':
    unittest.main()
