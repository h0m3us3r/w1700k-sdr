# SPDX-License-Identifier: MIT
"""The test report (tools/test_report.py) run on a small suite of its own: what it records of a
pass, a failure, an error and a skip, its exit status, and the page; and the list of modes the
on-unit sweep (tools/unit_sweep.py) walks for a link, which needs no unit to work out."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools')]

SUITE = '''
    """A suite that exists to be reported on: one of each outcome."""
    import unittest


    class Sample(unittest.TestCase):
        def test_this_one_passes(self):
            self.assertTrue(True)

        def test_this_one_fails(self):
            self.assertEqual(1 + 1, 3, 'arithmetic <as> wished')

        def test_this_one_breaks(self):
            raise RuntimeError('the bench caught fire')

        @unittest.skip('no unit on this bench')
        def test_this_one_is_skipped(self):
            pass
'''


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        tests = Path(cls.work.name) / 'tests'
        tests.mkdir()
        (tests / 'test_sample.py').write_text(textwrap.dedent(SUITE))
        cls.out = Path(cls.work.name) / 'out'
        cls.ran = subprocess.run([sys.executable, str(ROOT / 'tools' / 'test_report.py'), '--tests', str(tests),
                                  '--out', str(cls.out)], capture_output=True, text=True)
        cls.report = json.loads((cls.out / 'report.json').read_text())
        cls.page = (cls.out / 'report.html').read_text()

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_the_exit_status_and_the_terminal_are_the_standard_runners(self):
        self.assertEqual(self.ran.returncode, 1, 'a failing suite fails')
        self.assertIn('Ran 4 tests', self.ran.stderr)
        self.assertIn('FAILED (failures=1, errors=1, skipped=1)', self.ran.stderr)
        self.assertIn('report.html', self.ran.stdout)

    def test_every_test_is_recorded_with_its_outcome(self):
        self.assertEqual(self.report['counts'], {'failed': 1, 'error': 1, 'skipped': 1, 'passed': 1})
        (module,) = self.report['modules']
        self.assertEqual((module['name'], module['about']),
                         ('test_sample', 'A suite that exists to be reported on: one of each outcome.'))
        tests = {t['name']: t for t in module['tests']}
        self.assertEqual({name: t['outcome'] for name, t in tests.items()},
                         {'test_this_one_passes': 'passed', 'test_this_one_fails': 'failed',
                          'test_this_one_breaks': 'error', 'test_this_one_is_skipped': 'skipped'})
        self.assertIn('arithmetic <as> wished', tests['test_this_one_fails']['detail'])
        self.assertIn('the bench caught fire', tests['test_this_one_breaks']['detail'])
        self.assertEqual(tests['test_this_one_is_skipped']['detail'], 'no unit on this bench')
        self.assertTrue(all(t['group'] == 'Sample' and t['seconds'] >= 0 for t in tests.values()))

    def test_the_page_says_it_in_words_and_shows_why(self):
        self.assertIn('<h1>2 tests failed</h1>', self.page)
        self.assertIn('this one fails', self.page, 'names as sentences')
        self.assertIn('arithmetic &lt;as&gt; wished', self.page, 'a traceback, escaped')
        self.assertIn('no unit on this bench', self.page)
        self.assertIn('<details open>', self.page, 'a module with failures is open')
        self.assertIn('No sweep of the stream modes on a real unit', self.page)
        self.assertNotIn('<script', self.page, 'one file, nothing fetched or run')

    def test_a_sweep_left_beside_it_is_shown(self):
        import test_report
        sweep = {'when': '2026-11-13T02:00:00-08:00', 'unit': 'W1700K0000000001', 'link_mbps': 5000, 'seconds': 3,
                 'commit': 'abc1234', 'modes': [
                     {'name': '5 GHz at 320 Msps', 'what': 'the packing the link gives', 'ok': True, 'problem': '',
                      'packing': 'split', 'msps': 320.1, 'lost': 0, 'level_dbfs': -30.5, 'led': 'rainbow, breathing'},
                     {'name': '2.4 GHz at 80 Msps, packing 8', 'what': 'set by hand', 'ok': False,
                      'problem': '71.0 Msps arrived, not 80', 'packing': '8', 'msps': 71.0, 'lost': 12,
                      'level_dbfs': -28.0, 'led': 'hue 240, breathing'}]}
        page = test_report.page(self.report, sweep)
        self.assertIn('1 of 2 stream modes passed on unit W1700K0000000001', page)
        self.assertIn('71.0 Msps arrived, not 80', page)
        self.assertIn('rainbow, breathing', page)


class SweepModes(unittest.TestCase):
    def modes(self, link):
        import unit_sweep
        return unit_sweep.modes(link, None)

    def test_every_band_at_every_rate_and_the_640_capture(self):
        modes = self.modes(5000)
        chosen = [(band, rate) for _, what, band, rate, capture, packing in modes if packing is None and capture == rate]
        self.assertEqual(chosen, [(b, r * 10**6) for b in (0, 1, 2) for r in (40, 80, 160, 320)])
        self.assertEqual([m for m in modes if m[4] == 640_000_000],
                         [('6 GHz at 320 Msps, captured at 640', 'every other sample kept', 2, 320_000_000,
                           640_000_000, None)])

    def test_the_other_packings_are_those_the_link_carries(self):
        by_hand = {}
        for _, _, band, rate, _, packing in self.modes(5000):
            if packing:
                self.assertEqual(band, 0)
                by_hand.setdefault(rate // 10**6, []).append(packing)
        self.assertEqual(by_hand[40], ['half', 'full', '16', '14', '8', '7'], 'raw is the one chosen at 40 Msps')
        self.assertNotIn('raw', by_hand[160], '160 Msps raw is more than 5 Gbit/s carries')
        self.assertEqual(by_hand.get(320, []), [], 'at 320 Msps on 5 Gbit/s only the split packing fits, and it is chosen')
        ten = {rate // 10**6: packing for _, _, _, rate, _, packing in self.modes(10000) if packing}
        self.assertIn(320, ten, 'on 10 GbE there is a second packing to try at 320 Msps')

    def test_a_rate_the_link_cannot_carry_is_a_mode_that_must_be_refused(self):
        """320 Msps needs 4.59 Gbit/s at the least: below that the sweep still runs, and holds the
        controller to refusing it."""
        import unit_sweep
        for link in (2500, 1000):
            modes = self.modes(link)
            refused = [(band, rate // 10**6, capture // 10**6) for _, _, band, rate, capture, packing in modes
                       if packing == unit_sweep.REFUSED]
            self.assertEqual([m for m in refused if m[1] == 320], [(0, 320, 320), (1, 320, 320), (2, 320, 320), (2, 320, 640)],
                             f'{link}: every band at 320 Msps, and the 640 capture')
            for name, what, _, rate, _, packing in modes:
                if packing == unit_sweep.REFUSED:
                    self.assertEqual(what, unit_sweep.TOO_MUCH)
                    self.assertGreaterEqual(rate, 160_000_000 if link == 1000 else 320_000_000, name)
                elif packing:
                    self.assertTrue(unit_sweep.packing_fits(packing, rate, link, None), name)
        self.assertEqual([m for m in self.modes(5000) if m[5] == unit_sweep.REFUSED], [], 'everything fits 5 Gbit/s')
        at_2500 = {rate // 10**6: packing for _, _, band, rate, capture, packing in self.modes(2500)
                   if band == 1 and capture == rate}
        self.assertEqual((at_2500[40], at_2500[80], at_2500[160]), (None, None, None), 'the lower rates still run')


class HostBench(unittest.TestCase):
    """tools/host_bench.py: what it makes of the counters it reads each second."""

    @classmethod
    def setUpClass(cls):
        import host_bench
        cls.bench = host_bench

    def sample(self, slices=156250, lost=0, missed=0, clock=2.0, celsius=70, dt=1.0):
        s = {k: 0 for k in self.bench.SERVICE_COUNTERS}
        s.update(slices=slices, lost=lost, missed=missed, clock_ghz=clock, celsius=celsius, dt=dt)
        return s

    def test_totals_and_how_the_loss_was_spread(self):
        samples = [self.sample(), self.sample(lost=4, missed=2), self.sample(), self.sample(), self.sample(),
                   self.sample(lost=10, missed=5, celsius=75), self.sample()]
        t = self.bench.summarise(samples, 7.0)
        self.assertEqual((t['slices'], t['lost'], t['adapter_missed']), (7 * 156250, 14, 7))
        self.assertEqual(t['msps'], 320.0)
        self.assertAlmostEqual(t['lost_percent'], 100 * 14 / (7 * 156250), places=5)
        self.assertEqual((t['samples'], t['samples_with_loss'], t['worst_sample']), (7, 2, 10))
        self.assertEqual(t['longest_clean_seconds'], 3.0)
        slow = [self.sample(dt=1.04), self.sample(dt=1.04), self.sample(lost=2, dt=1.04), self.sample(dt=1.04)]
        self.assertEqual(self.bench.summarise(slow, 4.16)['longest_clean_seconds'], 2.1, 'in seconds, not samples')
        self.assertEqual((t['clock_ghz'], t['celsius_max']), (2.0, 75))

    def test_nothing_measured_and_an_adapter_that_does_not_count(self):
        t = self.bench.summarise([], 0.0)
        self.assertEqual((t['slices'], t['lost_percent'], t['msps'], t['adapter_missed']), (0, None, 0.0, None))
        t = self.bench.summarise([self.sample(missed=None)], 1.0)
        self.assertIsNone(t['adapter_missed'])

    def test_what_an_adapter_missed_by_any_of_its_names(self):
        self.assertEqual(self.bench.missed({'rx_packets': 9, 'rx_missed': 3, 'tx_errors': 1}), 3)
        self.assertEqual(self.bench.missed({'rx_fifo_errors': 2, 'rx_no_buffer_count': 5}), 7)
        self.assertIsNone(self.bench.missed({'rx_packets': 9}), 'a driver that does not count them')
        self.assertIsNone(self.bench.missed({}))

    def test_cpu_shares_between_two_readings(self):
        before = {0: (100, 1000, 10), 7: (500, 1000, 400)}
        after = {0: (150, 1100, 20), 7: (595, 1100, 482)}
        self.assertEqual(self.bench.busy_percent(before, after), {0: (50, 10), 7: (95, 82)})
        self.assertEqual(self.bench.busy_percent(before, before), {0: (0, 0), 7: (0, 0)})

    def test_the_programs_that_used_the_most(self):
        before = {1: ('w1700k-stream', 100), 2: ('firefox', 50), 3: ('firefox', 10), 4: ('idle', 5)}
        after = {1: ('w1700k-stream', 100 + 3 * self.bench.TICK * 10), 2: ('firefox', 50 + self.bench.TICK * 5),
                 3: ('firefox', 10 + self.bench.TICK * 5), 4: ('idle', 5), 9: ('new', 999)}
        self.assertEqual(self.bench.busiest(before, after, 10.0), [('w1700k-stream', 300), ('firefox', 100)])

    def test_where_an_interfaces_packets_land(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_root, proc_root = Path(tmp, 'sys'), Path(tmp, 'proc')
            controller = sys_root / 'devices' / 'pci0000:00' / '0000:00:0d.0'
            (controller / 'usb2' / '2-2' / '2-2:1.0').mkdir(parents=True)
            for number in (130, 125):
                (controller / 'msi_irqs' / str(number)).mkdir(parents=True)
            net = sys_root / 'class' / 'net' / 'enx0'
            (net / 'queues' / 'rx-0').mkdir(parents=True)
            (net / 'queues' / 'rx-0' / 'rps_cpus').write_text('7f\n')
            (net / 'device').symlink_to(controller / 'usb2' / '2-2' / '2-2:1.0')
            (proc_root / 'irq' / '125').mkdir(parents=True)
            (proc_root / 'irq' / '125' / 'effective_affinity_list').write_text('7\n')
            self.assertEqual(self.bench.receive_placement('enx0', str(sys_root), str(proc_root)),
                             {'queues': 1, 'irq': 125, 'irq_cpu': '7', 'steering': '7f'})
            self.assertEqual(self.bench.receive_placement('none', str(sys_root), str(proc_root)),
                             {'queues': 0, 'irq': None, 'irq_cpu': None, 'steering': None})

    def test_where_a_platform_usb_controllers_packets_land(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_root, proc_root = Path(tmp, 'sys'), Path(tmp, 'proc')
            controller = sys_root / 'devices' / 'platform' / 'soc@0' / 'a600000.usb' / 'xhci-hcd.2.auto'
            (controller / 'usb2' / '2-1' / '2-1.1' / '2-1.1:1.0').mkdir(parents=True)
            (controller / 'usb1').mkdir()
            net = sys_root / 'class' / 'net' / 'enx1'
            (net / 'queues' / 'rx-0').mkdir(parents=True)
            (net / 'device').symlink_to(controller / 'usb2' / '2-1' / '2-1.1' / '2-1.1:1.0')
            (proc_root / 'irq' / '283').mkdir(parents=True)
            (proc_root / 'irq' / '283' / 'effective_affinity_list').write_text('7\n')
            nothing = {'queues': 1, 'irq': None, 'irq_cpu': None, 'steering': None}
            self.assertEqual(self.bench.receive_placement('enx1', str(sys_root), str(proc_root)), nothing)
            (proc_root / 'interrupts').write_text('            CPU0       CPU1\n'
                                                  ' 21:          5          0  GICv3  21 Level     xhci-hcd:usb21\n'
                                                  '283:  511885689          0  GICv3 165 Level     xhci-hcd:usb1\n')
            self.assertEqual(self.bench.receive_placement('enx1', str(sys_root), str(proc_root)),
                             {'queues': 1, 'irq': 283, 'irq_cpu': '7', 'steering': None},
                             'no interrupt in sysfs: the one listed under a bus of the controller')

    def test_the_report_in_words(self):
        samples = [self.sample(), self.sample(lost=4, missed=2), self.sample(slices=0)]
        flowing = samples[:2]
        report = {'when': '2026-11-13T15:00:00-08:00', 'commit': 'abc1234', 'interface': 'enx0', 'mtu': 9216,
                  'link_mbps': 5000, 'placement': {'queues': 1, 'irq': 125, 'irq_cpu': '7', 'steering': '7f'},
                  'power_limits': {'long_term': 200.0, 'short_term': 15.0}, 'cpus': 8,
                  'runs': [{'rate_msps': 320, 'packing': 'split', 'problem': '', 'watched': True, 'idle_seconds': 1,
                            'samples': samples, 'totals': self.bench.summarise(flowing, 2.0),
                            'cpus': {0: (50, 10), 7: (95, 82)}, 'busiest': [('w1700k-stream', 259)]}]}
        text = self.bench.text_report(report)
        for part in ('interrupt on CPU 7, steering mask 7f', 'long-term 200 W, short-term 15 W',
                     '320 Msps, packing split, 2 s watched: 320.0 Msps arrived', 'no stream arrived in 1 of those',
                     'lost 4 (0.00128 %)', 'the adapter missed 2 datagrams (1.00 a second)',
                     'with any loss: 1 of 2; the worst lost 4 slices', '0:50(10) 7:95(82)',
                     'w1700k-stream 259 %'):
            self.assertIn(part, text)
        self.assertIn('  10 s', self.bench.progress_line([self.sample()] * 10, 10.2))



if __name__ == '__main__':
    unittest.main()
