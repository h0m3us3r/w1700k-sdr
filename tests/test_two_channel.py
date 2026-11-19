# SPDX-License-Identifier: MIT
"""tools/two_channel.py: channel 1 of a two-channel stream measured against channel 0. Signals
made here with a known gain, phase and delay must come back as they were made."""
import math
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:                                      # the tool needs it; the suite does not
    np = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
if np is not None:
    import two_channel

RATE = 80e6


def delayed(x, samples):
    """x later by a fraction of a sample (periodically: the test signals are made whole)."""
    f = np.fft.fftfreq(len(x))
    return np.fft.ifft(np.fft.fft(x) * np.exp(-2j * np.pi * f * samples))


@unittest.skipIf(np is None, 'numpy is not installed')
class Estimate(unittest.TestCase):
    N = 1 << 17

    def setUp(self):
        self.rng = np.random.default_rng(11)

    def noise(self, level=1.0, n=None):
        n = n or self.N
        return level * (self.rng.normal(size=n) + 1j * self.rng.normal(size=n))

    def band_limited(self, low, high):
        """Noise between `low` and `high` Hz from the LO: a signal both chains receive."""
        spectrum = np.fft.fft(self.noise(300))
        f = np.fft.fftfreq(self.N) * RATE
        spectrum[(f < low) | (f > high)] = 0
        return np.fft.ifft(spectrum)

    def pair(self, source, gain_db, phase_deg, delay_ns, noise=(3.0, 3.0), dc=(0, 0)):
        a = source + self.noise(noise[0]) + dc[0]
        b = (10 ** (gain_db / 20) * np.exp(1j * math.radians(phase_deg)) * delayed(source, delay_ns * 1e-9 * RATE)
             + self.noise(noise[1]) + dc[1])
        return a, b

    def test_gain_phase_and_delay_come_back_as_they_were_made(self):
        source = self.band_limited(-15e6, 12e6)
        for gain_db, phase_deg, delay_ns in ((0, 0, 0), (-3.5, 147.0, 4.2), (2.25, -91.5, -1.3), (-9, 12, 30.0), (1, -170, -41.0)):
            with self.subTest(gain_db=gain_db, phase_deg=phase_deg, delay_ns=delay_ns):
                r = two_channel.estimate(*self.pair(source, gain_db, phase_deg, delay_ns), RATE, max_delay_ns=50)
                self.assertAlmostEqual(r['gain_db'], gain_db, delta=0.05)
                self.assertAlmostEqual((r['phase_deg'] - phase_deg + 180) % 360 - 180, 0, delta=0.5)
                self.assertAlmostEqual(r['delay_ns'], delay_ns, delta=0.05)
                self.assertAlmostEqual(r['delay_samples'], delay_ns * 1e-9 * RATE, delta=0.005)
                self.assertGreater(r['coherence'], 0.99)
                self.assertAlmostEqual(r['bandwidth_hz'], 27e6, delta=1.5e6, msg='only where the signal is')

    def test_a_few_tones_are_enough(self):
        t = np.arange(self.N) / RATE
        source = sum(200 * np.exp(2j * np.pi * f * t) for f in (-7e6, -3e6, 3e6, 7e6))
        r = two_channel.estimate(*self.pair(source, -1.5, 60.0, 2.5, noise=(20, 20)), RATE)
        self.assertAlmostEqual(r['gain_db'], -1.5, delta=0.1)
        self.assertAlmostEqual(r['phase_deg'], 60.0, delta=0.5)
        self.assertAlmostEqual(r['delay_ns'], 2.5, delta=0.1)
        self.assertLess(r['bandwidth_hz'], 2e6, 'four lines, not a band')

    def test_noise_in_one_channel_does_not_move_the_gain_and_is_said(self):
        """The two ways of taking the gain are pulled apart by the noise in either channel: with
        little noise they agree and are the gain; with much in one channel the gain reported is
        between them, and how far apart they are says how well it is known."""
        source = self.band_limited(-10e6, 10e6)
        quiet = two_channel.estimate(*self.pair(source, 4.0, 30, 1, noise=(1, 1)), RATE)
        noisy = two_channel.estimate(*self.pair(source, 4.0, 30, 1, noise=(1, 400)), RATE, min_coherence=0.2)
        self.assertAlmostEqual(quiet['gain_db'], 4.0, delta=0.05)
        self.assertLess(quiet['gain_spread_db'], 0.05)
        self.assertGreater(noisy['gain_spread_db'], 1.0)
        self.assertAlmostEqual(noisy['gain_db'] - noisy['gain_spread_db'] / 2, 4.0, delta=0.3,
                               msg='channel 1 the noisy one: the lower of the two estimates is the gain')
        self.assertAlmostEqual(noisy['delay_ns'], 1.0, delta=0.3)

    def test_half_a_turn_of_phase_is_not_taken_for_a_delay(self):
        """Two lines 6 MHz apart: turning channel 1 by half a turn looks exactly like delaying it
        by 167 ns. Within the few nanoseconds two chains can differ by, it is a phase."""
        t = np.arange(self.N) / RATE
        source = 200 * np.exp(2j * np.pi * 3e6 * t) + 200 * np.exp(-2j * np.pi * 3e6 * t)
        same = two_channel.estimate(*self.pair(source, 0, 30.0, 2.0, noise=(20, 20)), RATE)
        turned = two_channel.estimate(*self.pair(source, 0, 30.0 + 180, 2.0, noise=(20, 20)), RATE)
        self.assertAlmostEqual(same['delay_ns'], 2.0, delta=0.3)
        self.assertAlmostEqual(turned['delay_ns'], 2.0, delta=0.3)
        self.assertAlmostEqual(((turned['phase_deg'] - same['phase_deg'] + 360) % 360), 180, delta=1.0)

    def test_each_channels_dc_and_level_are_reported_and_do_not_disturb_the_rest(self):
        source = self.band_limited(-10e6, 10e6)
        r = two_channel.estimate(*self.pair(source, 0, 45, 0, dc=(120 - 40j, -75 + 10j)), RATE)
        self.assertAlmostEqual(r['dc'][0][0], 120, delta=3)
        self.assertAlmostEqual(r['dc'][0][1], -40, delta=3)
        self.assertAlmostEqual(r['dc'][1][0], -75, delta=3)
        self.assertAlmostEqual(r['phase_deg'], 45, delta=0.5)
        self.assertAlmostEqual(r['level_dbfs'][0], r['level_dbfs'][1], delta=0.1)

    def test_only_the_band_named_is_gone_by(self):
        """Two sources with different paths to the chains: each band gives its own answer."""
        low, high = self.band_limited(-20e6, -8e6), self.band_limited(6e6, 18e6)
        a = low + high + self.noise(3)
        b = (np.exp(1j * math.radians(20)) * delayed(low, 2e-9 * RATE)
             + 0.5 * np.exp(1j * math.radians(-130)) * delayed(high, -5e-9 * RATE) + self.noise(3))
        first = two_channel.estimate(a, b, RATE, band=(-21e6, -7e6))
        second = two_channel.estimate(a, b, RATE, band=(5e6, 19e6))
        self.assertAlmostEqual(first['delay_ns'], 2.0, delta=0.1)
        self.assertAlmostEqual(first['phase_deg'], 20 - 360 * 14e6 * 2e-9 * 0, delta=1.0)
        self.assertAlmostEqual(second['delay_ns'], -5.0, delta=0.1)
        self.assertAlmostEqual(second['gain_db'], -6.02, delta=0.1)

    def test_a_burst_of_something_else_in_part_of_it_is_left_out(self):
        """A strong signal of another source over a tenth of the samples, with another path to
        the chains: the steady reference's answer stands."""
        source = self.band_limited(-10e6, 10e6)
        a, b = self.pair(source, 1.0, 100, 3.0)
        burst = self.noise(3000, self.N // 10)
        at = self.N // 3
        a[at:at + len(burst)] += burst
        b[at:at + len(burst)] += np.exp(1j * 2.0) * delayed(burst, -3.3)
        r = two_channel.estimate(a, b, RATE)
        self.assertAlmostEqual(r['delay_ns'], 3.0, delta=0.1)
        self.assertAlmostEqual(r['phase_deg'], 100, delta=1.0)
        self.assertAlmostEqual(r['gain_db'], 1.0, delta=0.1)

    def test_channels_with_nothing_in_common_are_said_to_have_none(self):
        with self.assertRaises(two_channel.NoReference):
            two_channel.estimate(self.noise(50), self.noise(50), RATE)
        with self.assertRaises(ValueError):
            two_channel.estimate(self.noise(50, 1000), self.noise(50, 999), RATE)

    def test_what_was_measured_can_be_taken_out_again(self):
        source = self.band_limited(-15e6, 15e6)
        a, b = self.pair(source, -2.0, 75.0, 6.0, noise=(0.5, 0.5))
        r = two_channel.estimate(a, b, RATE)
        back = two_channel.apply(b, RATE, r['gain_db'], r['phase_deg'], r['delay_ns'])
        again = two_channel.estimate(a, back, RATE)
        self.assertAlmostEqual(again['gain_db'], 0, delta=0.02)
        self.assertAlmostEqual(again['phase_deg'], 0, delta=0.2)
        self.assertAlmostEqual(again['delay_ns'], 0, delta=0.02)
        error = np.mean(np.abs(a - back)[1000:-1000] ** 2) / np.mean(np.abs(a) ** 2)
        self.assertLess(error, 1e-3, 'the two channels are then the same signal, but for their noise')



@unittest.skipIf(np is None, 'numpy is not installed')
class NoiseState(unittest.TestCase):
    """Which of the two phase states the channels are in, by the noise they share."""
    RATE = 40e6

    def setUp(self):
        self.rng = np.random.default_rng(5)

    def white(self, n, level=1.0):
        return level * (self.rng.normal(size=n) + 1j * self.rng.normal(size=n))

    def pair(self, n, phase_deg, share=0.5, level=300.0):
        """Two channels of independent noise with a part common to both, channel 1's at `phase_deg`."""
        common = self.white(n, share)
        return (level * (common + self.white(n)),
                level * (common * np.exp(1j * math.radians(phase_deg)) + self.white(n)))

    def raw(self, a, b):
        """As read_raw gives it: int16 (slots, 2 channels, 1024, I and Q)."""
        out = np.zeros((len(a) // 1024, 2, 1024, 2), np.int16)
        for c, x in enumerate((a, b)):
            out[:, c, :, 0], out[:, c, :, 1] = x.real.reshape(-1, 1024), x.imag.reshape(-1, 1024)
        return out

    def test_the_common_parts_phase_and_share_are_read(self):
        a, b = self.pair(1 << 19, -120.0)
        noise = two_channel.Noise(self.RATE)
        noise.add(a, b)
        r = noise.result()
        self.assertAlmostEqual(r['phase_deg'], -120.0, delta=2.0)
        self.assertAlmostEqual(r['correlation'], 0.25 / 1.25, delta=0.02)
        self.assertLess(r['chance'], 0.01)
        self.assertEqual(r['quiet'], 1.0)

    def test_the_other_state_reads_half_a_turn_away(self):
        a, b = self.pair(1 << 18, 40.0)
        one, other = two_channel.Noise(self.RATE), two_channel.Noise(self.RATE)
        one.add(a, b)
        other.add(a, -b)
        off = (other.result()['phase_deg'] - one.result()['phase_deg']) % 360.0
        self.assertAlmostEqual(off, 180.0, delta=0.5)

    def test_the_stream_as_it_lies_reads_the_same_as_its_samples(self):
        a, b = self.pair(1 << 18, 75.0)
        raw = self.raw(a, b)
        z = raw[..., 0].astype(float) + 1j * raw[..., 1]
        by_samples, as_it_lies = two_channel.Noise(self.RATE), two_channel.Noise(self.RATE)
        by_samples.add(z[:, 0].reshape(-1), z[:, 1].reshape(-1))
        as_it_lies.add_raw(raw)
        r, q = by_samples.result(), as_it_lies.result()
        self.assertAlmostEqual(r['phase_deg'], q['phase_deg'], delta=0.3)
        self.assertAlmostEqual(r['correlation'], q['correlation'], delta=0.003)
        self.assertEqual(r['segments'], q['segments'])

    def test_somebodys_frame_is_left_out_even_where_it_fills_a_block(self):
        """A frame is common to both channels too, at a phase of its own, and far stronger than the
        noise. Judged block by block, a block lying wholly inside one has nothing quiet to stand
        against and goes in whole; judged against the floor of everything seen, it stays out."""
        noise = two_channel.Noise(self.RATE)
        n = 1 << 16
        for i in range(12):
            a, b = self.pair(n, 30.0)
            if i % 3 == 1:                                 # a whole block of somebody's frame
                frame = self.white(n, 4.0) * 300.0
                a, b = a + frame, b + frame * np.exp(1j * math.radians(-150.0))
            (noise.look if i < 2 else noise.add)(a, b)
        r = noise.result()
        self.assertAlmostEqual(r['phase_deg'], 30.0, delta=3.0)
        self.assertAlmostEqual(r['quiet'], 7 / 10, delta=0.01)

    def test_only_the_middle_of_the_band_is_gone_by_whatever_the_rate(self):
        for rate, bins in ((40e6, 2 * 8e6 / (40e6 / 4096)), (160e6, 2 * 8e6 / (160e6 / 4096)), (20e6, 0.8 * 4096)):
            self.assertAlmostEqual(len(two_channel.Noise(rate).bins), bins, delta=0.08 * bins)

    def test_state_of(self):
        self.assertEqual(two_channel.state_of(70.0, 75.0), 0)
        self.assertEqual(two_channel.state_of(-110.0, 75.0), 1)
        self.assertEqual(two_channel.state_of(-175.0, 170.0), 0)       # across the turn
        self.assertIsNone(two_channel.state_of(165.0, 75.0))
        self.assertIsNone(two_channel.state_of(-15.0, 75.0))

    def reading(self, phase, determined=True):
        return {'phase_deg': phase, 'correlation': 0.2, 'chance': 0.001, 'determined': determined, 'seconds': 0.1}

    def aligner(self, phases, reference=20.0, **options):
        """align over scripted readings; the rephases it asked for."""
        readings, rephases = iter(phases), []
        result = two_channel.align(lambda: self.reading(*self._next(readings)), lambda: rephases.append(1),
                                   reference, **options)
        return result, len(rephases)

    @staticmethod
    def _next(readings):
        p = next(readings)
        return p if isinstance(p, tuple) else (p,)

    def test_align_rephases_until_the_reference_state_reads_twice(self):
        result, rephases = self.aligner([200.0, 198.0, 22.0, 18.0])
        self.assertEqual((rephases, result['rephased']), (2, 2))
        self.assertAlmostEqual(result['off_deg'], -2.0)
        result, rephases = self.aligner([25.0, 21.0])
        self.assertEqual((rephases, result['rephased']), (0, 0))

    def test_one_reading_of_the_reference_state_is_not_believed(self):
        """It has to say so twice running: the second disagreeing, the first is looked at again."""
        result, rephases = self.aligner([20.0, 200.0, 201.0, 19.0, 23.0])
        self.assertEqual(rephases, 1, 'rephased on the reading after the unconfirmed one')
        self.assertAlmostEqual(result['off_deg'], 3.0)

    def test_align_says_when_the_noise_cannot_tell(self):
        with self.assertRaisesRegex(two_channel.Undetermined, 'too little of the noise is common'):
            self.aligner([(20.0, False), (20.0, False)])
        with self.assertRaisesRegex(two_channel.Undetermined, 'neither the reference .* nor its opposite'):
            self.aligner([110.0, 112.0])
        with self.assertRaisesRegex(two_channel.Undetermined, 'did not come in 3 tries'):
            self.aligner([200.0] * 10, tries=3)
        result, rephases = self.aligner([110.0, 21.0, 19.0])           # one reading that cannot say is read again
        self.assertEqual(rephases, 0)

    def test_references_are_kept_by_band_and_lo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sub' / 'phase-reference.json'
            self.assertEqual(two_channel.load_references(path), {})
            refs = {(1, 5_340_000_000): {'band': 1, 'lo_hz': 5_340_000_000, 'phase_deg': 75.2, 'correlation': 0.19},
                    (0, 2_441_999_999): {'band': 0, 'lo_hz': 2_441_999_999, 'phase_deg': -18.0, 'correlation': 0.45}}
            two_channel.save_references(refs, path)
            self.assertEqual(two_channel.load_references(path), refs)


class FakeController:
    """What align_here and follow use of a Controller."""

    def __init__(self, band=1, lo_hz=5_340_000_000):
        self.band, self.lo_hz, self.relocks, self.running, self.rephases = band, lo_hz, 3, True, 0
        self.flipped = False              # the true state against the first one

    def state(self):
        return {'radio': {'band': self.band, 'sample_rate': 40_000_000},
                'rf': {'lo_hz': self.lo_hz, 'relocks': self.relocks},
                'stream': {'running': self.running, 'channels': 2}}

    def poll(self):
        pass

    def rephase(self, chain):
        assert chain == 1
        self.rephases += 1
        self.relocks += 1
        self.flipped = self.rephases % 3 != 0     # (a pattern in place of chance: the third try comes right)

    def relock(self, lo_hz=None):
        self.lo_hz, self.relocks, self.flipped, self.rephases = lo_hz or self.lo_hz, self.relocks + 1, True, 0


@unittest.skipIf(np is None, 'numpy is not installed')
class AlignHere(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'phase-reference.json'
        self.said = []
        self.phase_of = {5_340_000_000: 75.0, 5_700_000_000: -40.0}    # each LO's noise phase in its first state
        self.determined = True
        self.real = (two_channel.read_noise, two_channel.REPHASE_SETTLE)
        two_channel.read_noise = self.read_noise
        two_channel.REPHASE_SETTLE = 0.0

    def tearDown(self):
        two_channel.read_noise, two_channel.REPHASE_SETTLE = self.real
        self.tmp.cleanup()

    def read_noise(self, shm, rate, **options):
        phase = self.phase_of[self.ctl.lo_hz] + (180.0 if self.ctl.flipped else 0.0)
        return {'phase_deg': (phase + 180.0) % 360.0 - 180.0, 'correlation': 0.2, 'chance': 0.001,
                'determined': self.determined, 'seconds': 0.05, 'quiet': 1.0, 'segments': 500}

    def here(self, **options):
        return two_channel.align_here(self.ctl, None, self.refs, self.path, out=self.said.append, **options)

    def test_the_first_align_at_an_lo_learns_it_and_later_ones_return_to_it(self):
        self.refs = {}
        self.here()
        self.assertIn('learned as the reference', self.said[-1])
        self.assertEqual((self.ctl.rephases, list(self.refs)), (0, [(1, 5_340_000_000)]))
        self.assertEqual(two_channel.load_references(self.path)[(1, 5_340_000_000)]['phase_deg'], 75.0)
        self.ctl.relock()                                  # a new lock: the other state, as chance had it
        r = self.here()
        self.assertEqual((r['rephased'], self.ctl.flipped), (3, False))
        self.assertIn('channel 1 rephased 3 times', self.said[-1])
        self.here()
        self.assertIn('as it was', self.said[-1])
        self.assertEqual(self.ctl.rephases, 3)

    def test_each_lo_has_its_own_reference(self):
        self.refs = {}
        self.here()
        self.ctl.relock(5_700_000_000)
        self.ctl.flipped = False
        self.here()
        self.assertEqual(sorted(lo for _, lo in self.refs), [5_340_000_000, 5_700_000_000])
        self.assertEqual(self.refs[(1, 5_700_000_000)]['phase_deg'], -40.0)

    def test_learn_takes_the_state_found_in_place_of_the_one_kept(self):
        self.refs = {}
        self.here()
        self.ctl.relock()
        self.here(learn=True)
        self.assertEqual((self.ctl.rephases, self.refs[(1, 5_340_000_000)]['phase_deg']), (0, -105.0))

    def test_nothing_is_learned_or_rephased_where_the_noise_cannot_tell(self):
        self.refs, self.determined = {}, False
        with self.assertRaisesRegex(two_channel.Undetermined, 'band 1 at 5340.000 MHz: too little of the noise'):
            self.here()
        self.assertEqual((self.refs, self.path.exists()), ({}, False))
        self.determined = True
        self.here()
        self.determined = False
        self.ctl.relock()
        with self.assertRaisesRegex(two_channel.Undetermined, 'band 1 at 5340.000 MHz'):
            self.here()
        self.assertEqual(self.ctl.rephases, 0)

    def test_a_retune_under_it_decides_nothing(self):
        """Someone retunes the stream while its state is read: the reading is of neither LO, so no
        reference is learned from it and nothing is rephased by it; follow takes the new tuning."""
        self.refs = {}
        real = self.read_noise

        def retuned_meanwhile(shm, rate, **options):
            r = real(shm, rate, **options)
            self.ctl.relock(5_700_000_000)
            return r
        two_channel.read_noise = retuned_meanwhile
        with self.assertRaises(two_channel.Retuned):
            self.here()
        self.assertEqual((self.refs, self.ctl.rephases), ({}, 0))
        two_channel.read_noise = real
        self.ctl.lo_hz, self.ctl.flipped = 5_340_000_000, False
        self.here()                                        # the reference of 5340 MHz
        calls = []

        def once_retuned(shm, rate, **options):
            calls.append(self.ctl.lo_hz)
            r = real(shm, rate, **options)
            if len(calls) == 1:
                self.ctl.relock(5_700_000_000)             # another session, mid-reading
                self.ctl.flipped = False
            return r
        two_channel.read_noise = once_retuned
        self.ctl.relock()
        two_channel.follow(self.ctl, None, self.refs, self.path, every=0.0, out=self.said.append, rounds=3)
        self.assertIn('band 1 at 5700.000 MHz: learned as the reference', self.said[-1])
        self.assertEqual(self.refs[(1, 5_700_000_000)]['phase_deg'], -40.0, 'not 5340 MHz\'s reading under its name')
        self.assertEqual(self.ctl.rephases, 0, 'and nothing was rephased against the wrong reference')

    def test_follow_aligns_after_every_change_of_lo_or_lock_and_only_then(self):
        self.refs = {}
        steps = iter([None, None, self.ctl.relock, None, lambda: self.ctl.relock(5_700_000_000), None,
                      lambda: setattr(self.ctl, 'running', False), None])
        real = self.read_noise

        def read_noise(shm, rate, **options):
            self.after_reading = True
            return real(shm, rate, **options)

        def poll():                                        # (align's own look after a reading is not a round)
            if getattr(self, 'after_reading', False):
                self.after_reading = False
            else:
                (next(steps) or (lambda: None))()
        two_channel.read_noise, self.ctl.poll = read_noise, poll
        two_channel.follow(self.ctl, None, self.refs, self.path, every=0.0, out=self.said.append, rounds=8)
        kinds = ['learned' if 'learned' in line else 'rephased' if 'rephased' in line else 'as it was' for line in self.said]
        self.assertEqual(kinds, ['learned', 'rephased', 'learned'], self.said)


if __name__ == '__main__':
    unittest.main()
