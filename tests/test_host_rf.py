# SPDX-License-Identifier: MIT
"""RF control (rf.py): the op encoding and constants against probe_page.h, the route-grant
refusal, the control mailbox (raw programs on our WM), and the RF commands our WM builds and runs."""
import unittest

import host_fake
from host_fake import ROOT
from w1700k import radio, rf, wm

HEADERS = [ROOT / 'include' / 'hw' / 'hw_bits.h', ROOT / 'include' / 'hw' / 'mt7996' / 'probe_page.h',
           ROOT / 'include' / 'hw' / 'mt7996' / 'cap_route.h']


class Mirrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = host_fake.header_defines(*HEADERS)

    def test_mailbox(self):
        h = self.h
        self.assertEqual((rf.CTL_BASE, rf.CTL_MAX, rf.RF_BLOCK, rf.RF_OUT_MAX),
                         (h['MT7996_PROBE_CTL_BASE'], h['MT7996_PROBE_CTL_MAX'], h['MT7996_PROBE_CTL_RF_BASE'],
                          h['MT7996_PROBE_CTL_RF_OUT_MAX']))
        self.assertEqual((rf.CTL_REQ, rf.CTL_COUNT, rf.CTL_CLEANUP, rf.CTL_OPS, rf.CTL_RES),
                         (h['MT7996_PROBE_CTL_REQ'], h['MT7996_PROBE_CTL_COUNT'], h['MT7996_PROBE_CTL_CLEANUP'],
                          h['MT7996_PROBE_CTL_OP'](0), h['MT7996_PROBE_CTL_RES'](0)))
        for i, name in enumerate(rf.STATE_WORDS):
            self.assertEqual(rf.CTL_BASE + 4 * i, h[f'MT7996_PROBE_CTL_{name.upper()}'], name)
        self.assertLessEqual(rf.RF_BLOCK + 16 + 4 * rf.RF_OUT_MAX, 0x00910000)

    def test_ops_and_codes(self):
        h = self.h
        names = ('END', 'W32', 'RMW32', 'R32', 'DELAY', 'POLL', 'AW', 'AR', 'ARMW', 'APOLL', 'SAR', 'RF')
        self.assertEqual([getattr(rf, n) for n in names], [h[f'MT7996_PROBE_CTL_OP_{n}'] for n in names])
        self.assertEqual(rf.SOFT, h['MT7996_PROBE_CTL_OP_SOFT'])
        codes = ('BAD_OP', 'TIMEOUT', 'TOO_MANY', 'SEMA', 'BUSY', 'SPI', 'BAD_ARG', 'DENIED')
        self.assertEqual(sorted(rf.STATUS), [h[f'MT7996_PROBE_CTL_{c}'] for c in codes])
        commands = ('TUNE', 'GAIN', 'FILTER', 'TRIM', 'STATUS', 'LOCKS', 'BAND')
        self.assertEqual([getattr(rf, f'RF_{c}') for c in commands], [h[f'MT7996_PROBE_CTL_RF_{c}'] for c in commands])
        self.assertEqual((rf.RF_GAIN_AGC, rf.RF_UNLOCKED),
                         (h['MT7996_PROBE_CTL_RF_GAIN_AGC'], h['MT7996_PROBE_CTL_RF_UNLOCKED']))
        self.assertEqual(rf.TOP, 15)

    def test_route_grants(self):
        h = self.h
        window = h['MT7996_CAP_ROUTE_GRANT_B_ALL']                # the capture window's grant, allowed
        self.assertEqual(rf.ROUTE_GRANTS, {h['MT7996_CAP_ROUTE_GRANT_A']: 0xFFFFFFFF,
                                           h['MT7996_CAP_ROUTE_GRANT_B']: ~window & 0xFFFFFFFF,
                                           h['MT7996_CAP_ROUTE_GRANT_C']: 0xFFFFFFFF})


class Ops(unittest.TestCase):
    def test_encoding(self):
        self.assertEqual(rf.delay(10), (rf.DELAY | 10 << 12, 0, 0, 0))
        self.assertEqual(rf.apoll(0x24CA7, 1 << 16, 1 << 16, 1000, soft=True),
                         (rf.APOLL | rf.SOFT | 1000 << 12, 0x24CA7, 1 << 16, 1 << 16))
        self.assertEqual(rf.reg(2, 4, 0xA4B), 0x24A4B)
        self.assertEqual(rf.reg(0, rf.TOP, 0x02C), 0x0F02C)
        self.assertEqual(rf.armw(0x100, 0xF0, 0x30), (rf.ARMW, 0x100, 0xF0, 0x30))
        self.assertEqual(rf.rf_op(rf.RF_TUNE, 2, 0x1_2345_6789 & 0xFFFFFFFF, 1), (rf.RF, 0x201, 0x23456789, 1))
        self.assertEqual(rf.rf_op(rf.RF_GAIN, 1, rf.RF_GAIN_AGC), (rf.RF, 0x102, 0xFFFFFFFF, 0))
        for bad in (lambda: rf.delay(1 << 20), lambda: rf.reg(3, 0, 0), lambda: rf.reg(0, 5, 0),
                    lambda: rf.reg(0, 0, 0x1000), lambda: rf.armw(0x100, 0xF0, 0x100), lambda: rf.rmw32(0, 1, 2)):
            with self.assertRaises(ValueError):
                bad()


class RouteGrants(unittest.TestCase):
    def test_builders_refuse(self):
        for addr in (0x88009000, 0x88009004, 0x88009008, 0x88009006):
            with self.assertRaises(ValueError):
                rf.w32(addr, 1)
            with self.assertRaises(ValueError):
                rf.rmw32(addr, 1, 1)
        # zeros and the capture-window grant do not grant a bank the MCU uses
        rf.w32(0x88009000, 0)
        rf.w32(0x88009004, 0xFF000000)
        rf.rmw32(0x88009004, 0xFF000000, 0xFF000000)
        rf.w32(0x8800900C, 0x10)
        rf.r32(0x88009004)

    def test_program_check(self):
        with self.assertRaises(ValueError):
            rf.check_program([rf.r32(0x800230B4), (rf.W32, 0x88009004, 0xFF, 0)])
        with self.assertRaises(ValueError):
            rf.check_program([(rf.RMW32 | rf.SOFT, 0x88009008, 1, 1)])
        rf.check_program([(rf.R32, 0x88009000, 0, 0), (rf.AW, 0x88009000, 1, 0)])

    def test_session_refuses_before_posting(self):
        unit = host_fake.FakeUnit()
        unit.device.boot_wm()
        session = rf.CtlSession(unit, own_wm=True)
        with self.assertRaises(ValueError):
            session.post([(rf.W32, 0x88009000, 1, 0)])
        self.assertEqual(unit.device.programs, [])


class Mailbox(unittest.TestCase):
    def setUp(self):
        self.unit = host_fake.FakeUnit()
        self.dev = self.unit.device
        self.dev.boot_wm()
        self.session = rf.CtlSession(self.unit, own_wm=True)

    def test_needs_our_wm_or_a_stream(self):
        with self.assertRaises(rf.NotStreaming):
            rf.CtlSession(self.unit).run([rf.r32(0x800230B4)])

    def test_program_round_trip(self):
        self.dev.mem[0x83000000] = 0x1234
        out = self.session.run([rf.r32(0x83000000), rf.w32(0x83000004, 7), rf.rmw32(0x83000004, 0xF0, 0x30),
                                rf.r32(0x83000004), rf.aw(rf.reg(1, 2, 0x100), 3), rf.armw(rf.reg(1, 2, 0x100), 0xC, 4),
                                rf.ar(rf.reg(1, 2, 0x100)), rf.delay(10)])
        self.assertEqual(out, [0x1234, 0, 0, 0x37, 0, 7, 7, 0])
        self.assertEqual(self.dev.programs[0][1], (rf.W32, 0x83000004, 7, 0))

    def test_long_program_split(self):
        out = self.session.run([rf.r32(0x83000000 + 4 * i) for i in range(300)])
        self.assertEqual(len(out), 300)
        self.assertEqual([len(p) for p in self.dev.programs], [128, 128, 44])
        with self.assertRaises(ValueError):
            self.session.post([rf.r32(0)] * 129)

    def test_failed_op_named(self):
        ops = [rf.r32(0x83000000)] * 129 + [(rf.POLL | 5 << 12, 0x83000000, 1, 1)]
        with self.assertRaisesRegex(rf.CtlError, '^op 129: timeout$'):
            self.session.run(ops)

    def test_soft_timeout_counted(self):
        self.session.run([rf.apoll(rf.reg(0, 0, 0xCA7), 1 << 16, 1 << 16, soft=True)])
        self.assertEqual(self.session.soft, 1)

    def test_program_still_running(self):
        self.dev.mem[rf.CTL_REQ] = 9
        with self.assertRaisesRegex(rf.CtlError, 'still running'):
            self.session.post([rf.r32(0)])

    def test_ioctl_timeout_and_ended_session(self):
        self.dev.ctl_timeouts = 1
        with self.assertRaises(rf.CtlTimeout):
            self.session.state()
        self.session.close()
        with self.assertRaisesRegex(rf.CtlError, 'session ended'):
            self.session.state()


class Commands(unittest.TestCase):
    """RF commands: one RF op each; the result words from the RF block."""

    def setUp(self):
        self.unit = host_fake.FakeUnit()
        self.dev = self.unit.device
        self.dev.boot_wm()
        self.session = rf.CtlSession(self.unit, own_wm=True)

    def test_tune(self):
        self.assertEqual(rf.tune(self.session, 1, 5_300_000_000), 5_300_000_000)
        self.assertEqual(rf.tune(self.session, 0, 2_442_000_000.0), radio.actual_lo(0, 2_442_000_000))
        self.assertEqual(self.dev.rf_ops, [(rf.RF_TUNE, 1, 5_300_000_000 & 0xFFFFFFFF, 1),
                                           (rf.RF_TUNE, 0, 2_442_000_000, 0)])
        self.assertEqual(len(self.dev.programs), 2, 'one RF op each')

    def test_tune_range(self):
        for band, hz in ((0, 2_000_000_000), (0, 2_800_000_000), (1, 4_600_000_000), (2, 8_100_000_000)):
            with self.assertRaises(ValueError):
                rf.tune(self.session, band, hz)
        self.assertEqual(self.dev.rf_ops, [])

    def test_unlocked(self):
        self.dev.unlocking[2] = 2
        with self.assertRaisesRegex(rf.SynthUnlocked, r'band 2 synthesizer unlocked: the VCO did not lock at '
                                                      r'6105.000 MHz \(too high\); if it stays unlocked, power-cycle'):
            rf.tune(self.session, 2, 6_105_000_000)
        self.assertEqual(rf.synth_locks(self.session), [True, True, False])

    def test_gain_filter_trim(self):
        self.assertEqual(rf.set_gain(self.session, 0, 2, 9), [0x929203] * 4)
        self.assertEqual(rf.set_gain(self.session, 0), [0] * 4)
        self.assertEqual(rf.set_filter(self.session, 2, 320), [4] * 5)
        self.assertEqual(rf.set_trim(self.session, 1, 31), [0x11F00] * 4)
        self.assertEqual(self.dev.rf_ops, [(rf.RF_GAIN, 0, 2, 9), (rf.RF_GAIN, 0, rf.RF_GAIN_AGC, 0),
                                           (rf.RF_FILTER, 2, 320, 0), (rf.RF_TRIM, 1, 31, 0)])
        for bad in (lambda: rf.set_gain(self.session, 0, 2), lambda: rf.set_gain(self.session, 0, 5, 0),
                    lambda: rf.set_gain(self.session, 0, 0, 10), lambda: rf.set_filter(self.session, 0, 30),
                    lambda: rf.set_trim(self.session, 0, 32)):
            with self.assertRaises(ValueError):
                bad()
        self.assertEqual(len(self.dev.rf_ops), 4, 'refused on the host')

    def test_status(self):
        rf.tune(self.session, 0, 2_442_000_000)
        rf.set_gain(self.session, 0, 1, 3)
        out = rf.status(self.session, 0)
        self.assertAlmostEqual(out.pop('vco_hz'), 2_442_000_000 * 16 / 5, delta=10)   # MT7976: LO x 16/5
        self.assertEqual(out, {'band': 0, 'vco': 'locked', 'firmware_lo_khz': 2_442_000, 'filter_codes': [0] * 4,
                               'gain': ['lna 1 lpf 3'] * 4})
        self.assertEqual([rf._describe_gain(w) for w in (0, 0x3101, 0x8)], ['agc', 'lna 1 lpf 3', '0x00000008'])

    def test_band_switch(self):
        wm.Wm(self.unit, session=self.session).radio_post('stream', wm.RADIO_PACKING['raw'])
        self.dev.pump_start(['mt7996-stream'])
        self.assertTrue(self.session.streaming())
        before, after = rf.select_band(self.session, 2)
        self.assertNotEqual(before, after)
        self.assertEqual(self.dev.icap_band, 2)
        with self.assertRaises(ValueError):
            rf.select_band(self.session, 3)

    def test_refused_by_the_firmware(self):
        with self.assertRaisesRegex(rf.CtlError, '^RF command 9 band 0: RF command or argument refused$'):
            rf.rf_command(self.session, 9)

    def test_needs_our_wm(self):
        with self.assertRaisesRegex(rf.CtlError, 'need our own WM'):
            rf.rf_command(rf.CtlSession(self.unit), rf.RF_LOCKS)


if __name__ == '__main__':
    unittest.main()
