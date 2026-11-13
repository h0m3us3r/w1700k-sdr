# SPDX-License-Identifier: MIT
"""The status LED's vocabulary (kernel/w1700k_sdr/ledd.h, built for the host as tests/led_check.c):
colours leave HSB only as the four emitters' levels, fades run through hue the short way and never
through colours neither end has, each device state has its look, and the controller's state record
is read as the controller writes it."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))
sys.path.insert(0, str(ROOT / 'tests'))

import host_fake  # noqa: E402
from w1700k import config  # noqa: E402
from w1700k.controller import Controller  # noqa: E402

CC = shutil.which(os.environ.get('CC', 'cc'))


@unittest.skipUnless(CC, 'no C compiler')
class Led(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tool = os.path.join(cls.tmp.name, 'led_check')
        subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-I' + str(ROOT / 'kernel' / 'w1700k_sdr'),
                        str(ROOT / 'tests' / 'led_check.c'), '-lm', '-o', cls.tool], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_tool(self, *args):
        return subprocess.run([self.tool, *map(str, args)], capture_output=True, text=True, check=True).stdout

    def levels(self, h, s, b):
        return tuple(int(v) for v in self.run_tool('levels', h, s, b).split())

    def blend(self, a, c, t):
        return tuple(float(v) for v in self.run_tool('blend', *a, *c, t).split())

    def look(self, record, link=1, flowing=0, ended=0):
        path = '-'
        if record is not None:
            path = os.path.join(self.tmp.name, 'state.json')
            with open(path, 'w') as f:
                f.write(record if isinstance(record, str) else json.dumps(record))
        return self.run_tool('look', path, link, flowing, ended).split(' | ')[0]

    def test_a_pure_hue_is_its_emitters_and_white_is_the_white_one(self):
        self.assertEqual(self.levels(0, 1, 1), (255, 0, 0, 0))
        self.assertEqual(self.levels(120, 1, 1), (0, 255, 0, 0))
        self.assertEqual(self.levels(240, 1, 1), (0, 0, 255, 0))
        self.assertEqual(self.levels(60, 1, 1), (255, 255, 0, 0), 'yellow: red and green')
        self.assertEqual(self.levels(300, 1, 1), (255, 0, 255, 0), 'magenta: red and blue')
        self.assertEqual(self.levels(77, 0, 1), (0, 0, 0, 255), 'no saturation: the white emitter alone')
        self.assertEqual(self.levels(200, 1, 0), (0, 0, 0, 0))
        r, g, b, w = self.levels(0, 0.5, 1)
        self.assertEqual((g, b), (0, 0), 'a pale red is red and white, nothing else')
        self.assertEqual((r, w), (128, 128))

    def test_brightness_is_perceptual_and_keeps_the_mix(self):
        steps = [self.levels(30, 1, b / 20) for b in range(21)]
        for lo, hi in zip(steps, steps[1:]):
            self.assertTrue(all(x <= y for x, y in zip(lo, hi)), 'never dimmer as brightness rises')
        self.assertEqual(self.levels(30, 1, 0.5)[0], round(255 * 0.5 ** 2.2), 'half as bright to the eye: 22 % of the light')
        full, half = self.levels(30, 1, 1), self.levels(30, 1, 0.7)
        self.assertAlmostEqual(half[1] / half[0], full[1] / full[0], delta=0.03, msg='the hue does not shift as it dims')

    def test_fades_go_the_short_way_round_the_hue(self):
        self.assertEqual(self.blend((0, 1, 1), (120, 1, 1), 0.5), (60.0, 1.0, 1.0), 'red to green passes yellow')
        h, _, _ = self.blend((350, 1, 1), (30, 1, 1), 0.5)
        self.assertAlmostEqual(h, 10.0, places=2, msg='across 0, not back through cyan')
        h, _, _ = self.blend((30, 1, 1), (350, 1, 1), 0.25)
        self.assertAlmostEqual(h, 20.0, places=2)
        self.assertEqual(self.blend((10, 1, 1), (200, 0.5, 0.2), 0), (10.0, 1.0, 1.0))
        self.assertEqual(self.blend((10, 1, 1), (200, 0.5, 0.2), 1), (200.0, 0.5, 0.2))

    def test_white_and_dark_have_no_hue_of_their_own(self):
        """Up from dark, or in from white, the hue is the coloured end's all the way."""
        for t in (0.0, 0.3, 0.9):
            self.assertEqual(self.blend((0, 1, 0), (240, 1, 1), t)[0], 240.0, 'from dark to blue: never red')
            self.assertEqual(self.blend((0, 0, 0.4), (120, 1, 0.7), t)[0], 120.0, 'from white to green')
            self.assertEqual(self.blend((120, 1, 0.7), (0, 0, 0.4), t)[0], 120.0, 'and back')

    def record(self, **changes):
        base = {'loaded': True, 'band': 1, 'packing': None, 'synth_locked': [True, True, True],
                'bands': {'0': {'lo_hz': 1, 'sample_rate': 40_000_000}, '1': {'lo_hz': 2, 'sample_rate': 160_000_000},
                          '2': {'lo_hz': 3, 'sample_rate': 320_000_000}}}
        base.update(changes)
        return base

    def test_each_state_has_its_look(self):
        self.assertEqual(self.look(None, link=0), 'breathe 50 1.00 0.90 3.00 0.85 turn 0', 'no link: yellow breathing')
        self.assertEqual(self.look(None), 'rise 0 0.00 1.00 6.00 0.65 turn 0', 'link, nothing loaded: white, rising')
        self.assertEqual(self.look(self.record(loaded=False)), 'rise 0 0.00 1.00 6.00 0.65 turn 0')
        self.assertEqual(self.look(self.record()), 'steady 120 1.00 0.70 0.00 0.00 turn 0', 'ready: green')
        self.assertEqual(self.look(self.record(synth_locked=[True, False, True])),
                         'blink 50 1.00 0.90 1.20 0.35 turn 0', 'the captured band out of lock: yellow blinking')
        self.assertEqual(self.look(self.record(synth_locked=[False, True, False])),
                         'steady 120 1.00 0.70 0.00 0.00 turn 0', 'another band out of lock is not this stream\'s trouble')
        self.assertEqual(self.look(self.record(), link=0), 'breathe 50 1.00 0.90 3.00 0.85 turn 0', 'the link first')

    def test_a_stream_shows_its_rate_and_whether_it_is_lossless(self):
        hues = {40: 180, 80: 240, 160: 300}
        for rate, hue in hues.items():
            bands = {'1': {'lo_hz': 2, 'sample_rate': rate * 1_000_000}}
            for packing in ('raw', 'half', 'full'):
                self.assertEqual(self.look(self.record(packing=packing, bands=bands), flowing=1),
                                 'steady %d 1.00 0.90 0.00 0.00 turn 0' % hue, '%d Msps %s: solid' % (rate, packing))
            for packing in ('split', '16', '14', '8', '7'):
                self.assertEqual(self.look(self.record(packing=packing, bands=bands), flowing=1),
                                 'breathe %d 1.00 0.90 3.00 0.65 turn 0' % hue, '%d Msps %s: breathing' % (rate, packing))
        for hue in hues.values():       # a third of the circle apart, and as clear of go, look and stop
            for meaning in (0, 120, 360):
                self.assertGreaterEqual(abs(hue - meaning), 60)

    def test_320_msps_is_the_rainbow(self):
        """Every colour, once round in six seconds; solid, breathing and blinking keep their meanings."""
        bands = {'1': {'lo_hz': 2, 'sample_rate': 320_000_000}}
        self.assertEqual(self.look(self.record(packing='half', bands=bands), flowing=1),
                         'steady 0 1.00 0.90 0.00 0.00 turn 6', 'lossless: the brightness holds')
        self.assertEqual(self.look(self.record(packing='split', bands=bands), flowing=1),
                         'breathe 0 1.00 0.90 2.50 0.65 turn 6', 'reduced: it breathes, out of step with the turn')
        self.assertEqual(self.look(self.record(packing='split', bands=bands), flowing=0),
                         'blink 0 1.00 0.90 2.00 0.50 turn 6', 'nothing leaving the port')
        self.assertNotEqual(6.0 / 2.5, round(6.0 / 2.5), 'or the same colours would dim every time')

    def test_a_stream_that_stalls_or_ends(self):
        streaming = self.record(packing='full')
        self.assertEqual(self.look(streaming, flowing=0), 'blink 300 1.00 0.90 2.00 0.50 turn 0', 'nothing leaving the port')
        self.assertEqual(self.look(streaming, flowing=1, ended=1), 'blink 0 1.00 1.00 1.00 0.50 turn 0', 'ended by itself: red')
        self.assertEqual(self.look(self.record(), ended=1), 'steady 120 1.00 0.70 0.00 0.00 turn 0',
                         'a done file left by a stream the host stopped means nothing')

    def test_the_record_the_controller_writes(self):
        """The real thing: a controller against the fake unit, its state file read by the LED."""
        unit = host_fake.FakeUnit()
        ctl = Controller(config.parse({}), unit=unit)
        try:
            ctl.load(1)
            state = unit.files['/tmp/w1700k-state.json'].decode()
            self.assertEqual(self.run_tool('look', self.write(state), 1, 0, 0).split(' | ')[1].strip(),
                             'loaded 1 streaming 0 reduced 0 rate 320 unlocked 0')
            ctl.configure(1, sample_rate=160_000_000)
            unit.device.link_mbps = 2500
            ctl.start()
            state = unit.files['/tmp/w1700k-state.json'].decode()
            self.assertEqual(self.run_tool('look', self.write(state), 1, 1, 0).split(' | ')[1].strip(),
                             'loaded 1 streaming 1 reduced 1 rate 160 unlocked 0')
            ctl.stop()
            ctl.configure(0, sample_rate=40_000_000)
            ctl.start()
            state = unit.files['/tmp/w1700k-state.json'].decode()
            self.assertEqual(self.run_tool('look', self.write(state), 1, 1, 0).split(' | ')[1].strip(),
                             'loaded 1 streaming 1 reduced 0 rate 40 unlocked 0')
        finally:
            ctl.close()

    def write(self, text):
        path = os.path.join(self.tmp.name, 'controller.json')
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_the_engine_greets_fades_and_acknowledges(self):
        rows = [tuple(float(v) for v in line.split()) for line in self.run_tool('engine').splitlines()]
        at = {round(t, 2): (h, s, b) for t, h, s, b in rows}
        self.assertEqual(at[0.0][2], 0.0, 'out of the dark')
        self.assertLess(at[0.5][2], at[1.0][2])
        hues = [at[round(k * 0.05, 2)][0] for k in range(1, 60)]
        self.assertTrue(all(b > a for a, b in zip(hues, hues[1:])), 'the greeting: the hue once round')
        self.assertGreater(hues[-1], 340)
        self.assertEqual(at[3.7], (120.0, 1.0, 0.7), 'settled on green')
        mid = at[4.1]                    # half way through the 0.6 s fade to blue (hue 225)
        self.assertAlmostEqual(mid[0], 172.5, delta=1.0)
        self.assertEqual(at[4.5], (225.0, 1.0, 0.9))
        lifted = at[4.7]                 # the acknowledgement: towards white, the hue kept
        self.assertEqual(lifted[0], 225.0)
        self.assertLess(lifted[1], 0.8)
        self.assertGreater(lifted[2], 0.9)
        self.assertEqual(at[4.9], (225.0, 1.0, 0.9), 'and back within a quarter second')

    def test_one_operations_many_writes_get_one_lift(self):
        """A start rewrites the state record when it begins, as it goes and when it is done."""
        self.assertEqual(self.run_tool('heard', 10, 10.25, 10.75, 11.4).strip(), '10.00', 'one instruction')
        self.assertEqual(self.run_tool('heard', 10, 10.5, 11.5, 12.0, 13.1).strip(), '10.00 11.50 13.10',
                         'and the next ones as they come')

    def test_idle_white_rises_like_a_bar_and_goes_on_where_it_was_left(self):
        rows = [tuple(float(v) for v in line.split()) for line in self.run_tool('rise').splitlines()]
        at = {round(t, 2): (s, b) for t, _, s, b in rows}
        white = lambda t: at[t][1]                           # noqa: E731
        self.assertEqual(at[1.0][0], 0.0, 'white: no colour in it')
        self.assertAlmostEqual(white(1.0), 0.35 + 0.65 * 1.0 / 6, places=3)
        climb = [white(round(k * 0.05, 2)) for k in range(13, 40)]      # after the fade from green
        self.assertTrue(all(b > a for a, b in zip(climb, climb[1:])), 'brighter all the way')
        # 2 s: green comes, and takes over at once
        self.assertEqual(at[2.6], (1.0, 0.7), 'the next state within its fade, not after the climb')
        # 4 s: idle again, on from the 2 s it had climbed
        self.assertAlmostEqual(white(4.7), 0.35 + 0.65 * 2.7 / 6, places=3)
        self.assertGreater(white(4.7), white(1.95), 'not from low again')
        self.assertLess(white(7.9), 1.0, 'still climbing after 5.9 s of it')
        self.assertEqual(white(8.05), 1.0, 'full after 6 s of climbing in all')
        self.assertEqual(white(9.9), 1.0, 'and it stays')
        # 10 s: left after it had finished; 12 s: back, from low
        self.assertAlmostEqual(white(12.7), 0.35 + 0.65 * 0.7 / 6, places=3)
        self.assertAlmostEqual(white(13.95), 0.35 + 0.65 * 1.95 / 6, places=3)


if __name__ == '__main__':
    unittest.main()
