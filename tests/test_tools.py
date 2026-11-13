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


if __name__ == '__main__':
    unittest.main()
