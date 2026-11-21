# SPDX-License-Identifier: MIT
"""Occupancy as the host has it (host/w1700k/occupancy.py): what it writes to turn the firmware's
measurement on, how it reads a report whole, what it makes of one, and `w1700k occupancy` through
the controller against the fake unit (tests/host_fake.py), whose firmware answers as ours does."""
import struct
import unittest

import host_fake
from w1700k import occupancy
from w1700k.controller import ControlError, Controller, Refused, handle

FS, LO = 160_000_000, 2_442_000_000


class FakeSession:
    """A control session over a page of words: every ioctl's writes are kept, and `on_read` may
    change the page between two reads, as a firmware would."""

    def __init__(self, page=None, on_read=None):
        self.page, self.writes, self.reads, self.on_read = dict(page or {}), [], 0, on_read

    def ctl(self, writes=(), read=None, timeout_ms=2000):
        if writes:
            self.writes.append(list(writes))
            self.page.update(writes)
        if not read:
            return [], False
        self.reads += 1
        if self.on_read:
            self.on_read(self)
        return [self.page.get(read[0] + 4 * i, 0) for i in range(read[1])], False


def page_with(number, snapshots=100, state=occupancy.RUNNING, **report):
    page = {occupancy.STATE: state, occupancy.SEQ: 2 * number, occupancy.SNAPSHOTS: number * snapshots}
    for i, w in enumerate(host_fake.occupancy_words(number, snapshots, **report)):
        page[occupancy.REPORT + 4 * i] = w
    return page


class Words(unittest.TestCase):
    def test_the_layout_is_the_firmwares(self):
        header = (host_fake.ROOT / 'include' / 'hw' / 'mt7996' / 'probe_page.h').read_text()
        self.assertRegex(header, rf'#define MT7996_PROBE_OCC_BASE\s+{occupancy.BASE:#010x}u')
        for name, at in (('PERIOD', occupancy.PERIOD), ('EVERY', occupancy.EVERY), ('THRESHOLD', occupancy.THRESHOLD),
                         ('STATE', occupancy.STATE), ('SNAPSHOTS', occupancy.SNAPSHOTS), ('SEQ', occupancy.SEQ),
                         ('REPORT', occupancy.REPORT)):
            self.assertRegex(header, rf'#define MT7996_PROBE_OCC_{name}\s+\(MT7996_PROBE_OCC_BASE \+ '
                                     rf'{at - occupancy.BASE:#04x}u\)', name)
        self.assertIn(f'#define MT7996_PROBE_OCC_REPORT_WORDS     {occupancy.REPORT_WORDS}u', header)
        layout = (host_fake.ROOT / 'firmware' / 'dsp' / 'occ_layout.h').read_text()
        self.assertIn(f'#define OCC_BINS\t{occupancy.BINS}', layout)
        self.assertIn(f'#define OCC_BLOCKS\t{occupancy.BLOCKS}', layout)
        self.assertEqual(occupancy.REPORT_WORDS, 8 + 5 * occupancy.BINS)
        for code in occupancy.FAILURES:
            self.assertRegex(header, rf'#define\s+MT7996_PROBE_OCC_ST_\w+\s+{code}u')

    def test_the_period_for_a_rate(self):
        self.assertEqual(occupancy.period_for(160_000_000, 100), 781)      # 78,125 chunks a second
        self.assertEqual(occupancy.period_for(40_000_000, 100), 195)
        self.assertEqual(occupancy.period_for(40_000_000, 1000), 20)
        for bad in (0, -1, 1001):
            with self.assertRaises(ValueError):
                occupancy.period_for(40_000_000, bad)

    def test_on_writes_what_a_report_is_before_the_period_that_starts_it(self):
        session = FakeSession()
        occupancy.on(session, 781, 100, 6.0)
        (writes,) = session.writes
        self.assertEqual([at for at, _ in writes], [occupancy.EVERY, occupancy.THRESHOLD, occupancy.PERIOD])
        self.assertEqual((writes[0][1], writes[2][1]), (100, 781))
        self.assertAlmostEqual(struct.unpack('<f', struct.pack('<I', writes[1][1]))[0], 3.981, places=3)
        for bad in ((0, 100, 6.0), (781, 0, 6.0), (781, 1 << 16, 6.0), (781, 100, 0.0), (781, 100, 61.0)):
            with self.assertRaises(ValueError):
                occupancy.on(FakeSession(), *bad)

    def test_one_that_failed_is_turned_off_and_seen_off_first(self):
        def firmware(session):                      # sees the period at 0 on its next step
            if session.page.get(occupancy.PERIOD) == 0 and session.reads > 2:
                session.page[occupancy.STATE] = occupancy.OFF
        session = FakeSession({occupancy.STATE: 5, occupancy.PERIOD: 781}, firmware)
        occupancy.on(session, 781, 100, sleep=lambda s: None)
        self.assertEqual(session.writes[0], [(occupancy.PERIOD, 0)])
        self.assertEqual(session.writes[1][-1], (occupancy.PERIOD, 781))
        stuck = FakeSession({occupancy.STATE: 5})
        clock = iter(range(100))
        with self.assertRaisesRegex(occupancy.OccupancyError, 'did not turn occupancy off'):
            occupancy.on(stuck, 781, 100, sleep=lambda s: None, clock=lambda: next(clock))


class Reading(unittest.TestCase):
    def test_a_whole_report(self):
        number, words = occupancy.report(FakeSession(page_with(3)))
        self.assertEqual((number, words), (6, host_fake.occupancy_words(3, 100)))

    def test_none_while_one_is_written_or_before_the_first(self):
        self.assertIsNone(occupancy.report(FakeSession({occupancy.SEQ: 0})))
        self.assertIsNone(occupancy.report(FakeSession({**page_with(3), occupancy.SEQ: 7})))

    def test_one_that_changed_under_the_reading_is_not_taken(self):
        def firmware(session):                      # a new report lands between the first read and the last
            if session.reads == 3:
                session.page.update(page_with(4))
        self.assertIsNone(occupancy.report(FakeSession(page_with(3), firmware)))

    def test_wait_takes_the_next_one(self):
        def firmware(session):
            if session.reads == 6:
                session.page.update(page_with(4))
        number, words = occupancy.wait(FakeSession(page_with(3), firmware), after=6, sleep=lambda s: None)
        self.assertEqual((number, words[0]), (8, 4))

    def test_wait_says_why_none_comes(self):
        clock = iter(range(1000))
        quiet = dict(sleep=lambda s: None, clock=lambda: next(clock))
        for code, reason in occupancy.FAILURES.items():
            with self.assertRaisesRegex(occupancy.OccupancyError, reason[:30]):
                occupancy.wait(FakeSession({occupancy.STATE: code}), **quiet)
        with self.assertRaisesRegex(occupancy.OccupancyError, 'beside a stream'):
            occupancy.wait(FakeSession({occupancy.STATE: occupancy.OFF}), **quiet)
        with self.assertRaisesRegex(occupancy.OccupancyError, r'no report in 5 s \(37 snapshots so far\)'):
            occupancy.wait(FakeSession({occupancy.STATE: occupancy.RUNNING, occupancy.SNAPSHOTS: 37}), **quiet)


class Decoding(unittest.TestCase):
    def setUp(self):
        # bins 40..42 of the program's order are 24..22 bins below the LO: -60 .. -55 MHz at 160 Msps
        self.report = occupancy.decode(host_fake.occupancy_words(2, 100), FS, LO)

    def test_the_bins_run_from_the_lowest_frequency_to_the_highest(self):
        hz = [b['hz'] for b in self.report['bins']]
        self.assertEqual((hz[0], hz[32], hz[-1]), (LO - FS // 2, LO, LO + FS // 2 - FS // 64))
        self.assertEqual(hz, sorted(hz))
        self.assertEqual(self.report['bin_hz'], 2_500_000)

    def test_a_bins_numbers(self):
        at = {b['hz']: b for b in self.report['bins']}
        busy, free = at[LO - 24 * 2_500_000], at[LO + 5 * 2_500_000]
        self.assertEqual((busy['busy'], busy['strong'], free['busy'], free['strong']), (0.5, 0.5, 0.0, 0.0))
        full = (8191 * 32) ** 2
        self.assertEqual(free['mean_dbfs'], round(10 * __import__('math').log10(100.0 / full), 1))
        self.assertEqual(busy['max_dbfs'] - free['noise_dbfs'], 20.0)
        self.assertEqual((self.report['report'], self.report['snapshots'], self.report['snapshots_since_on']), (2, 100, 200))
        self.assertEqual((self.report['gain_span_db'], self.report['dsp_ms'], self.report['learned_from']), (10.0, 0.365, 21))
        self.assertEqual(self.report['peak_dbfs'], -6.2)

    def test_a_shape_learned_from_too_little_is_said(self):
        self.assertNotIn('note', self.report)
        words = host_fake.occupancy_words(2, 100)
        words[7] = 3
        report = occupancy.decode(words, FS, LO)
        self.assertEqual(report['learned_from'], 3)
        self.assertRegex(report['note'], 'only 3 of 64 snapshots were quiet')

    def test_only_the_middle_half_is_inside_the_channel_filter(self):
        inside = [b['hz'] for b in self.report['bins'] if b['in_band']]
        self.assertEqual((inside[0], inside[-1], len(inside)), (LO - 15 * 2_500_000, LO + 15 * 2_500_000, 31))

    def test_a_report_of_nothing(self):
        report = occupancy.decode([0] * occupancy.REPORT_WORDS, FS, LO)
        self.assertEqual((report['snapshots'], report['peak_dbfs'], report['gain_span_db']), (0, None, None))
        self.assertEqual({(b['mean_dbfs'], b['busy']) for b in report['bins']}, {(None, 0.0)})
        with self.assertRaises(ValueError):
            occupancy.decode([0] * 264, FS, LO)

    def test_wifi_channels_inside_the_filter(self):
        # 2442 +- 40 MHz holds channels 1..13's centres from 2412 on, whole from 2412 - 8.125 >= 2403.25
        words = host_fake.occupancy_words(1, 100, busy_bins=(62, 63, 0, 1, 2))      # around the LO: channel 7
        channels = occupancy.wifi_channels(occupancy.decode(words, FS, LO))
        self.assertEqual([c['channel'] for c in channels], list(range(1, 14)))
        by = {c['channel']: c for c in channels}
        self.assertEqual((by[7]['mhz'], by[7]['busy']), (2442, 0.5))
        self.assertEqual((by[1]['busy'], by[13]['busy']), (0.0, 0.0))
        self.assertEqual(by[5]['busy'], 0.5, 'a channel is as busy as its busiest bin: channel 5 reaches 2440.1 MHz')
        self.assertEqual(by[4]['busy'], 0.0)
        self.assertEqual((by[7]['strong'], by[1]['strong']), (0.5, 0.0))
        skirts = host_fake.occupancy_words(1, 100, busy_bins=(62, 63, 0, 1, 2), skirts=range(3, 62))
        by = {c['channel']: c for c in occupancy.wifi_channels(occupancy.decode(skirts, FS, LO))}
        self.assertEqual((by[7]['busy'], by[7]['strong'], by[1]['busy'], by[1]['strong']), (0.5, 0.5, 0.5, 0.0),
                         'beside a strong transmitter every channel is busy, and one is where it sits')
        five = occupancy.wifi_channels(occupancy.decode(words, FS, 5_210_000_000))
        self.assertEqual([c['channel'] for c in five], [36, 40, 44, 48])
        six = occupancy.wifi_channels(occupancy.decode(words, 40_000_000, 6_115_000_000))
        self.assertEqual([c['channel'] for c in six], [33])


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = host_fake.temp_dir()
        self.config = host_fake.make_config(self.tmp.name)
        self.unit = host_fake.FakeUnit()
        self.dev = self.unit.device
        self.events = []
        self.ctl = Controller(self.config, emit=self.events.append, unit=self.unit)
        self.ctl.attach()
        self.ctl.load(0)

    def tearDown(self):
        self.ctl.close()
        self.tmp.cleanup()

    def test_a_report_of_the_running_stream(self):
        self.ctl.start()
        report = self.ctl.occupancy(2.0, 50)
        cfg = self.ctl.bands[0]
        self.assertEqual((report['snapshots'], report['report']), (100, 1), 'two seconds at 50 a second')
        self.assertEqual(self.dev.mem[occupancy.EVERY], 100)
        self.assertEqual(report['bins'][32]['hz'], self.ctl.state()['rf']['lo_hz'])
        self.assertEqual(report['bin_hz'], cfg.sample_rate / 64)
        self.assertTrue(report['wifi'], 'the 2.4 GHz channels inside the filter')
        self.assertEqual(self.dev.mem[occupancy.PERIOD], 0, 'turned off again afterwards')
        self.assertIsNone(self.ctl.state()['error'])

    def test_kept_running_when_asked(self):
        self.ctl.start()
        self.ctl.occupancy(1.0, 100, keep=True)
        self.assertEqual(self.dev.mem[occupancy.PERIOD], occupancy.period_for(self.ctl.bands[0].sample_rate, 100))
        second = self.ctl.occupancy(1.0, 100, keep=True)
        self.assertEqual(second['report'], 2, 'the next report, not the one already read')

    def test_it_needs_a_stream(self):
        with self.assertRaisesRegex(Refused, 'beside a stream'):
            self.ctl.occupancy()
        self.assertNotIn(occupancy.PERIOD, self.dev.mem)

    def test_a_stream_of_two_channels_is_refused_and_nothing_is_written(self):
        """Its chunks hold two chains' samples in turn, which the DSP would read as one."""
        self.ctl.configure(0, sample_rate=40_000_000)
        self.ctl.start(channels=2)
        with self.assertRaisesRegex(Refused, 'one channel'):
            self.ctl.occupancy()
        self.assertNotIn(occupancy.PERIOD, self.dev.mem)
        self.assertTrue(self.ctl.state()['stream']['running'])

    def test_a_set_without_the_dsp_image_is_said_and_is_not_the_receivers_error(self):
        self.ctl.start()
        self.dev.dsp_loaded = False
        with self.assertRaisesRegex(Refused, 'no DSP image'):
            self.ctl.occupancy()
        self.assertIsNone(self.ctl.state()['error'])
        self.assertTrue(self.ctl.state()['stream']['running'])
        self.assertEqual(self.dev.mem[occupancy.PERIOD], 0)

    def test_bad_requests(self):
        self.ctl.start()
        for seconds, per_second in ((0, 100), (601, 100), (600, 1000), (1, 0)):
            with self.assertRaises((ValueError, ControlError)):
                self.ctl.occupancy(seconds, per_second)

    def test_the_json_protocol(self):
        self.ctl.start()
        reply = handle(self.ctl, {'id': 1, 'op': 'occupancy', 'seconds': 0.5, 'per_second': 20})
        self.assertEqual(reply['occupancy']['snapshots'], 10)
        self.assertEqual(handle(self.ctl, {'id': 2, 'op': 'occupancy'})['occupancy']['snapshots'], 100)
        with self.assertRaisesRegex(ValueError, 'seconds: expected'):
            handle(self.ctl, {'id': 3, 'op': 'occupancy', 'seconds': 'long'})


if __name__ == '__main__':
    unittest.main()
