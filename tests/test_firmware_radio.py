# SPDX-License-Identifier: MIT
"""Our WM's radio code on the host: every module of firmware/radio/ (hal_wm.c aside) built natively
with the free-running HAL mock (firmware/radio/tests/mock.c) and driven by tests/radio_smoke.c
through a cold load, and the RF command builder against the host-built programs it replaced.

There is no EEPROM image in the repository (it is per-unit calibration), so the test writes a
synthetic one (synthetic_eeprom). An all-zero image does not make the modules refuse: every band
then reads as absent and the per-band PHY init and the FEM pin mux are silently skipped, so the
image sets the bytes that select the code paths of this board.

mock.c and radio.c each define radio_ee and radio_id (tentative definitions); -fcommon merges them.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RADIO = ROOT / 'firmware' / 'radio'
sys.path.insert(0, str(ROOT / 'host'))

from w1700k import radio, rf  # noqa: E402

CC = shutil.which('cc')
SOURCES = sorted(p for p in RADIO.glob('*.c') if p.name != 'hal_wm.c')

# radio_cmd.h RADIO_STATE_*
IDENTIFIED, PATCH_INIT, COMMON, POOL, POST, UP = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 15, 1 << 16
BAND = lambda b: 1 << (4 + b)          # noqa: E731
RF = lambda d: 1 << (8 + d)            # noqa: E731
CHAN = lambda b: 1 << (20 + b)         # noqa: E731
CAPTURE = lambda b: 1 << (24 + b)      # noqa: E731
BROUGHT_UP = IDENTIFIED | PATCH_INIT | COMMON | POOL | POST | UP | sum(BAND(b) | RF(b) for b in range(3))
CTL_BAD_ARG = 7                         # probe_page.h MT7996_PROBE_CTL_BAD_ARG


def synthetic_eeprom():
    """7680 bytes; the radio code reads only these (every other byte 0):
      0x190..0x194  MT7996_PHY_EE_CONF_*: band select 1, 2, 3, TX paths 4, 4, 4, RX paths 4, 4, 5
                    (this board's); a band whose select is 0 is absent
      0x196..0x198  MT7996_PHY_EE_196: ELNA, EPA and [3] set, so the FEM pin mux
                    and the channel set's EPA tables run on every band
      0x199..0x19b  MT7996_PHY_EE_199: [2] the FEM type on band 0; [3] clear (set, the post init
                    refuses: its other path is not decoded)
      0x19c..0x19e  MT7996_PHY_EE_19C, the FEM pin polarities; 0x1a4 MT7996_PHY_EE_1A4; 0x1ae
                    ADIE_DCOC_EE_OFF: 0, so both DCOC items run
      0xbaa, 0x57e, 0x127e  MT7996_PHY_EE_RCAL, the dies' R-cal bytes: valid, made-up codes
      0x1269, 0x126f        ADIE_EE_XTAL_TRIM and its signed offset: made up
      0x65e..0x6f9  MT7996_CHSET_EE_RXG, the RX gain offsets per band, chain and subband: a small
                    signed pattern"""
    ee = bytearray(7680)
    ee[0:2] = (0x7990).to_bytes(2, 'little')          # MT7996_PHY_EE_CHIP_ID
    conf = {0: 1, 1: 2, 2: 3, 3: 4, 4: 4, 5: 4, 6: 4, 7: 4, 8: 5}       # field f: 3 bits at 0x190 + f / 2
    for f, value in conf.items():
        ee[0x190 + f // 2] |= value << 3 * (f % 2)
    ee[0x196:0x199] = bytes((0x0B,) * 3)
    ee[0x199:0x19C] = bytes((0x04, 0x00, 0x00))
    ee[0xBAA], ee[0x57E], ee[0x127E] = 0x85, 0x90, 0x91
    ee[0x1269], ee[0x126F] = 0x60, 0x42
    for i in range(0x65E, 0x6FA):
        ee[i] = ((i * 5) % 7 - 3) & 0xFF
    return bytes(ee)


def digest(ops):
    return hashlib.sha256(json.dumps([list(o) for o in ops]).encode()).hexdigest()[:16]


@unittest.skipUnless(CC, 'a native C compiler (cc) is needed')
class RadioCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.binary = tmp / 'radio_smoke'
        subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Werror', '-fcommon', f'-I{ROOT / "include"}', f'-I{RADIO}',
                        '-o', str(cls.binary), str(ROOT / 'tests' / 'radio_smoke.c'), *map(str, SOURCES),
                        str(RADIO / 'tests' / 'mock.c')], check=True)
        cls.eeprom = tmp / 'eeprom.bin'
        cls.eeprom.write_bytes(synthetic_eeprom())
        cls.steps = cls.load('steps')
        cls.commands = cls.load('commands')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def load(cls, mode, **environment):
        out = subprocess.run([str(cls.binary), str(cls.eeprom), mode], capture_output=True, text=True, check=True,
                             env=dict(os.environ, **environment))
        results = [line.split() for line in out.stderr.splitlines() if line.startswith('result ')]
        results = [dict(name=f[1], arg=int(f[2]), res=int(f[4]), fail=(int(f[6]), int(f[7], 16), int(f[8])),
                        state=int(f[10], 16)) for f in results]
        segments, current = [], None
        for line in out.stdout.splitlines():
            if line.startswith('# step '):
                name, arg = line.split()[2:]
                current = (name, int(arg), [])
                segments.append(current)
            else:
                current[2].append(line)
        return {'results': results, 'segments': segments, 'log': out.stdout, 'tail': out.stderr.splitlines()[-1]}

    def accesses(self, run):
        return [line for _, _, lines in run['segments'] for line in lines]

    def segment(self, name, arg, run=None):
        return next(lines for n, a, lines in (run or self.steps)['segments'] if (n, a) == (name, arg))

    def kinds(self, name, arg):
        return Counter(line.split()[0] for line in self.segment(name, arg) if not line.startswith('#'))

    def test_every_command_succeeds(self):
        """No module refused or recorded a HAL failure; the state words as radio_cmd.h names them."""
        for run in (self.steps, self.commands):
            for r in run['results']:
                self.assertEqual((r['res'], r['fail']), (0, (0, 0, 0)), r)
            self.assertTrue(run['tail'].startswith('OK '), run['tail'])
        states = {(r['name'], r['arg']): r['state'] for r in self.commands['results']}
        self.assertEqual(states[('bringup', 0)], BROUGHT_UP)
        self.assertEqual(states[('setup', 2)], BROUGHT_UP | sum(CHAN(b) for b in range(3)) | CAPTURE(0) | CAPTURE(1))
        self.assertEqual(states[('select', 1)], BROUGHT_UP | sum(CHAN(b) | CAPTURE(b) for b in range(3)) | 1 << 29,
                         'and which band ICAP listens to (RADIO_STATE_SELECTED), for STREAM to hold to')
        self.assertEqual([r['name'] for r in self.steps['results'][:10]],
                         ['patch_init', 'pool', 'common', 'band', 'band', 'band', 'rf_init', 'rf_init', 'rf_init',
                          'post'])

    def test_bringup_is_its_steps(self):
        """BRINGUP makes exactly the accesses of its steps in radio.c's order, and CHANSET those of
        the channel set followed by the band's DCOC."""
        self.assertEqual(self.accesses(self.commands), self.accesses(self.steps))

    def test_stable(self):
        self.assertEqual(self.load('steps')['log'], self.steps['log'])

    def test_access_counts(self):
        """A-die transactions as the module headers state them: RF init 778 (MT7976) and 628 (MT7977,
        rfinit.h), DCOC about 890 / 1,420 / 1,770 on bands 0 / 1 / 2 (dcoc.h)."""
        adie = lambda name, arg: self.kinds(name, arg)['AW'] + self.kinds(name, arg)['AR']   # noqa: E731
        self.assertEqual([adie('rf_init', d) for d in range(3)], [778, 628, 628])
        for band, about in enumerate((890, 1420, 1770)):
            self.assertAlmostEqual(adie('dcoc', band), about, delta=about // 100)
        self.assertEqual(self.segment('pool', 0), ['# rom call 00822b92(100, 0, 1c0, 1)'])
        for name, arg, lines in self.steps['segments']:
            if name != 'pool':
                self.assertTrue(lines, f'{name} {arg} made no access')
        for band in range(3):
            self.assertGreater(self.kinds('band', band)['W'], 4000, 'the per-band PHY words and tables')
            self.assertEqual(adie('capture', band), 0)

    def test_bands_touch_their_own_die(self):
        """Band b's per-band steps reach only A-die b (die = band)."""
        for band in range(3):
            for name in ('rf_init', 'setup', 'dcoc'):
                dies = {int(line.split()[1], 16) for line in self.segment(name, band) if line[0] == 'A'}
                self.assertEqual(dies, {band}, f'{name} {band}')

    def test_no_route_word_written(self):
        """The load never writes the capture route block (rf.ROUTE_GRANTS: a grant hangs the MCU);
        only a stream arms the route."""
        route = re.compile(r'W 880090[0-2][0-9a-f] ')
        self.assertFalse([line for line in self.accesses(self.commands) if route.match(line)])

    def test_nothing_a_load_does_can_transmit_or_burn_a_fuse(self):
        """Every access of bring-up, the three bands' set-up and DCOC, capture, rate and select, as
        the mock logs them. This is a receiver: the MAC's transmit stays disabled in every word
        written to it, no A-die chain is given a transmit bit, and the eFuse controller, the
        semaphores and the ROM (but for the pool's layout) are left alone."""
        arb = {'820e3000': 0, '820f3000': 1, '830e3000': 2}                # MT7996_LMAC_ARB_SCR(band)
        tx_dis, rx_dis, chain_tx = 1 << 8, 1 << 9, 0x01F80004             # ..._TX_DIS, _RX_DIS; ADIE_CHAIN_EN_TX_MASK
        for run in (self.commands, self.steps):
            received = set()
            for line in self.accesses(run):
                f = line.split()
                if f[0] == 'W' and f[1] in arb:
                    self.assertTrue(int(f[2], 16) & tx_dis, f'the MAC\'s transmit enabled: {line}')
                    if not int(f[2], 16) & rx_dis:
                        received.add(arb[f[1]])
                if f[0] == 'AW' and int(f[3], 16) == 0x010:                # ADIE_CHAIN_EN
                    self.assertFalse(int(f[4], 16) & chain_tx, f'a chain\'s transmit blocks enabled: {line}')
                if f[0] in ('W', 'R'):
                    address = int(f[1], 16)
                    self.assertFalse(0x70021000 <= address < 0x70022000, f'the eFuse controller: {line}')
                    self.assertFalse(0x7C070000 <= address < 0x7C070400, f'a semaphore: {line}')
                if line.startswith('# rom call'):
                    self.assertEqual(line, '# rom call 00822b92(100, 0, 1c0, 1)', 'the pool layout, and no other ROM routine')
            self.assertEqual(received, {0, 1, 2}, 'and each band was set receiving, with transmit disabled in the same word')
        chains = {line.split()[4] for line in self.accesses(self.commands)
                  if line.startswith('AW') and int(line.split()[3], 16) == 0x010}
        self.assertEqual(chains, {'00000000', '0003fbcb', '8003fbcb'}, 'off, receive, and receive under DCOC: no other state')

    def test_setting_a_band_receiving_disables_transmit_whatever_the_word_held(self):
        """Not left to the reset value: with transmit found enabled, the word written when a band
        is set receiving disables it."""
        run = self.load('commands', RADIO_SMOKE_ARB_SCR='0xf0007000')         # the reset value less TX_DIS and RX_DIS
        written = [int(line.split()[2], 16) for band in range(3) for line in self.segment('capture', band, run)
                   if line.startswith(('W 820e3000', 'W 820f3000', 'W 830e3000'))]
        self.assertEqual(len(written), 3)
        self.assertEqual({value & 0x300 for value in written}, {0x100}, 'TX_DIS set and RX_DIS clear, on each band')

    def test_select_is_the_band_switch(self):
        """SELECT leaves ICAPCR8 of the three ICAPs as the RF band switch writes it mid-stream."""
        band_switch = self.rfctl((rf.RF_BAND | 1 << 8, 0, 0))[0]
        writes = [(a, b) for op, a, b, _ in band_switch['ops'] if op == rf.W32]
        last = {}
        for line in self.segment('select', 1):
            kind, addr, value = line.split()
            if kind == 'W':
                last[int(addr, 16)] = int(value, 16)
        self.assertEqual(sorted(last.items()), sorted(writes))
        self.assertEqual(len(writes), 3)

    # --- the RF command builder (rfctl.c) ---

    def rfctl(self, *ops, mode='rfctl'):
        args = [str(x) for op in ops for x in op]
        out = subprocess.run([str(self.binary), mode, *args], capture_output=True, text=True, check=True).stdout
        return [json.loads(line) for line in out.splitlines()]

    def tune(self, band, hz):
        return self.rfctl((rf.RF_TUNE | band << 8, hz & 0xFFFFFFFF, hz >> 32))[0]

    def test_tune_programs(self):
        """Op for op the hardware-validated programs host/w1700k/rf.py built before (their digests),
        presets always written, with the FRAC word's bit 0 now always clear (the SX ignores it:
        include/hw/adie/sx.h ADIE_SX_FRAC_STEP_LSB; 5250 and 6105 MHz were even already); the LO
        the words give; the health reads reported."""
        for band, hz, skip, want in ((0, 2_442_000_000, 0, 'ec104fb9c406c99d'),
                                     (1, 5_250_000_000, 0, 'a485f7ec75d15ce6'),
                                     (2, 7_400_000_000, 0, 'e6ad5f9f90f7bffe'),
                                     (2, 6_105_000_000, 45, '2f806ee56af76621')):   # after its 45 presets
            p = self.tune(band, hz)
            self.assertEqual(p['res'], 0)
            self.assertEqual(digest(p['ops'][skip:]), want, (band, hz))
            self.assertEqual((p['out'], p['check']), (p['count'] - 5, p['count'] - 5))
            self.assertEqual(p['ops'][p['out']][0], rf.AR)
        self.assertEqual(self.tune(1, 5_250_000_000)['count'], 100)
        p = self.tune(2, 7_400_000_000)
        self.assertEqual(p['lo'] | p['lo_hi'] << 32, 7_399_999_995)

    def test_lo_on_the_grid(self):
        """Any Hz in range: the FRAC word is even and the LO reported is radio.actual_lo's."""
        cases = [(0, 2_080_000_000 + 1_637_003 * k) for k in range(0, 400, 3)] + \
                [(b, 4_650_000_000 + 13_840_007 * k) for b in (1, 2) for k in range(0, 200, 3)]
        progs = self.rfctl(*((rf.RF_TUNE | b << 8, hz & 0xFFFFFFFF, hz >> 32) for b, hz in cases))
        for (band, hz), p in zip(cases, progs):
            frac = [o[2] for o in p['ops'] if o[0] == rf.AW and o[1] & 0xFFF == 0xA4A]
            self.assertTrue(frac and all(f & 1 == 0 for f in frac), (band, hz))
            self.assertEqual(p['lo'] | p['lo_hi'] << 32, radio.actual_lo(band, hz), (band, hz))

    def test_programs(self):
        """Gain, filter, trim, status, locks and band switch as rf.py built them."""
        csd = lambda band, chain: 0x83000000 + (band << 20) + (chain << 16) + 0xC4    # noqa: E731
        gain, agc, filt, trim, status, locks, band = self.rfctl(
            (rf.RF_GAIN | 1 << 8, 2, 9), (rf.RF_GAIN | 1 << 8, rf.RF_GAIN_AGC, 0), (rf.RF_FILTER | 2 << 8, 80, 0),
            (rf.RF_TRIM, 7, 0), (rf.RF_STATUS | 2 << 8, 0, 0), (rf.RF_LOCKS, 0, 0), (rf.RF_BAND | 2 << 8, 0, 0))
        ops = lambda p: [tuple(o) for o in p['ops']]    # noqa: E731
        self.assertEqual(ops(gain), [rf.w32(csd(1, c), 0x929203) for c in range(4)] +
                         [rf.r32(csd(1, c)) for c in range(4)])
        self.assertEqual(gain['out'], 4)
        self.assertEqual(ops(agc)[:4], [rf.w32(csd(1, c), 0) for c in range(4)])
        self.assertEqual(ops(filt), [rf.aw(rf.reg(2, k, 0x100), 2) for k in range(5)] +
                         [rf.aw(rf.reg(2, k, 0x933), 2) for k in range(5)] +
                         [rf.ar(rf.reg(2, k, 0x1E8)) for k in range(5)])
        self.assertEqual(filt['out'], 10)
        self.assertEqual(ops(trim), [rf.armw(rf.reg(0, k, 0xE51), 0x11F00, 7 << 8 | 0x10000) for k in range(4)])
        sx = [rf.ar(rf.reg(2, 4, r)) for r in (0xA4B, 0xA4A, 0xA40, 0xCA4)]
        self.assertEqual(ops(status), sx + [rf.ar(rf.reg(2, 0, 0xBC8))] +
                         [rf.ar(rf.reg(2, k, 0x100)) for k in range(5)] + [rf.r32(csd(2, c)) for c in range(5)])
        self.assertEqual(ops(locks), [rf.ar(rf.reg(b, 0 if b == 0 else 4, 0xCA4)) for b in range(3)])
        self.assertEqual(ops(band), [rf.r32(0x800230B4)] + [rf.w32(a, 2) for a in (0x800230B0, 0x800240B0, 0x800250B0)]
                         + [rf.r32(0x800230B4)])
        for p in (trim, status, locks, band):
            self.assertEqual((p['out'], p['check'], p['lo'], p['lo_hi']), (0, 0xFFFFFFFF, 0, 0))
            self.assertLessEqual(p['count'] - p['out'], rf.RF_OUT_MAX)

    def test_mt7976_filters(self):
        """Band 0's filter independent of the bandwidth: codes 0..3 with MediaTek's 0x125 [22:20]
        and the RC code; 320 MHz is code 3 widened, the trim 9 under the RC code where that is
        not under 8 (mt7976.h; the next test).
        Refused before the bring-up has measured the RC code."""
        rc = 17                                            # radio_smoke RFCTL_RC_CODE
        narrow, wide = self.rfctl((rf.RF_FILTER, 160, 0), (rf.RF_FILTER, 320, 0))
        ops = lambda p: [tuple(o) for o in p['ops']]    # noqa: E731
        for p, code, ext, trim in ((narrow, 3, 0, rc), (wide, 3, 2, rc - 9)):
            self.assertEqual(ops(p), [rf.aw(rf.reg(0, k, 0x100), code) for k in range(4)] +
                             [rf.aw(rf.reg(0, k, 0x933), 2) for k in range(4)] +
                             [rf.armw(rf.reg(0, k, 0x125), 0x700000, ext << 20) for k in range(4)] +
                             [rf.armw(rf.reg(0, k, 0xE51), 0x11F00, trim << 8 | 0x10000) for k in range(4)] +
                             [rf.ar(rf.reg(0, k, 0x1E8)) for k in range(4)])
            self.assertEqual(p['out'], 16)
        uncal = self.rfctl((rf.RF_FILTER, 160, 0), (rf.RF_FILTER | 2 << 8, 320, 0), mode='rfctl-uncal')
        self.assertEqual([p['res'] for p in uncal], [CTL_BAD_ARG, 0])

    def test_the_widened_filters_trim_keeps_out_of_the_codes_that_oscillate(self):
        """The widened filter oscillates at a low trim, and where that begins goes by the code and
        not by how far it is under the calibrated one: a die calibrated to 13 oscillated at 4,
        which is 9 under, and was clean from 5 (mt7976.h). So the trim is 9 under the calibrated
        code but never under 8, and never over the calibrated code; the unwidened filter keeps
        the calibrated code whatever it is."""
        def trims(rc):
            narrow, wide = self.rfctl((rf.RF_FILTER, 160, 0), (rf.RF_FILTER, 320, 0), mode=f'rfctl-rc={rc}')
            found = []
            for p in (narrow, wide):
                words = {o[3] for o in map(tuple, p['ops']) if o[:3] == tuple(rf.armw(rf.reg(0, 0, 0xE51), 0x11F00, 0))[:3]}
                self.assertEqual(len(words), 1, 'one trim, on every chain')
                found.append((words.pop() >> 8) & 31)
            return tuple(found)
        self.assertEqual(trims(17), (17, 8), 'the die it was tuned on: as before')
        self.assertEqual(trims(13), (13, 8), 'the die that oscillated at 4')
        self.assertEqual(trims(31), (31, 22), 'far above the floor: 9 under')
        self.assertEqual(trims(18), (18, 9))
        for rc in range(9, 18):
            self.assertEqual(trims(rc), (rc, 8), rc)
        for rc in range(0, 9):
            self.assertEqual(trims(rc), (rc, rc), 'never raised over the calibrated code')
        self.assertGreater(min(trims(rc)[1] for rc in range(8, 32)), 4, 'the highest code seen to oscillate')

    def test_rate_keeps_lo_and_filter(self):
        """RATE re-runs the bandwidth part of the channel set only: no synthesizer word, no filter
        word, no DC-offset calibration on its die; a channel set before it is required."""
        rates = [lines for name, arg, lines in self.commands['segments'] if name == 'rate']
        self.assertEqual(len(rates), 2)
        filt = {0x100, 0x933, 0x125, 0xE51}
        for lines in rates:
            adie = [line.split() for line in lines if line.startswith(('AW', 'AR'))]
            idx = {int(f[3], 16) for f in adie if f[0] == 'AW'}
            self.assertFalse(idx & filt, 'a filter word written')
            self.assertFalse({i for i in idx if 0xA00 <= i <= 0xCFF}, 'a synthesizer word written')
            self.assertLess(len(adie), 200, 'no DCOC')
            self.assertGreater(len([line for line in lines if line.startswith('W ')]), 300, 'table B')
        self.assertEqual([r['res'] for r in self.commands['results'] if r['name'] == 'rate'], [0, 0])

    def test_refusals_match_the_host(self):
        """The firmware refuses exactly outside what rf.py accepts: LO ranges, gain and trim indices,
        filter widths, bands and commands."""
        for band, (lo, hi) in rf.LO_RANGE.items():
            self.assertEqual([self.tune(band, hz)['res'] for hz in (lo, hi, lo - 1, hi + 1)],
                             [0, 0, CTL_BAD_ARG, CTL_BAD_ARG])
        cases = [((rf.RF_GAIN, 4, 9), 0), ((rf.RF_GAIN, 5, 0), CTL_BAD_ARG), ((rf.RF_GAIN, 0, 10), CTL_BAD_ARG),
                 ((rf.RF_TRIM, 31, 0), 0), ((rf.RF_TRIM, 32, 0), CTL_BAD_ARG),
                 ((rf.RF_FILTER, 10, 0), CTL_BAD_ARG), ((rf.RF_FILTER, 640, 0), CTL_BAD_ARG),
                 ((rf.RF_STATUS | 3 << 8, 0, 0), CTL_BAD_ARG), ((rf.RF_BAND | 3 << 8, 0, 0), CTL_BAD_ARG),
                 ((0, 0, 0), CTL_BAD_ARG), ((rf.RF_BAND + 1, 0, 0), CTL_BAD_ARG)]
        cases += [((rf.RF_FILTER, mhz, 0), 0) for mhz in rf.FILTER_CODE]
        results = self.rfctl(*(op for op, _ in cases))
        self.assertEqual([p['res'] for p in results], [want for _, want in cases])


if __name__ == '__main__':
    unittest.main()
