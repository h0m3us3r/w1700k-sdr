# SPDX-License-Identifier: MIT
"""Occupancy on the radio. The program of the DSP cores (firmware/dsp/occ.c), built for the host
(tests/occ_check.c) and fed chunks made here: its spectrum against the same arithmetic in Python,
and its account of a band whose truth is known because this made it: a receiver whose gain jumps
20 dB from one chunk to the next, a filter's shape over the span, a converter's offset, a tone
that is there in a third of the chunks and a wide signal in a fifth. The WM's side
(firmware/stream/occ.h), built for the host too (tests/occ_wm_check.c) and run against a model of
the DMA engine and the core. And the image itself, where the RISC-V toolchain is installed."""
import cmath
import math
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'firmware'), str(ROOT / 'host')]

import container  # noqa: E402
import own  # noqa: E402
from stream import blob  # noqa: E402
from w1700k import occupancy  # noqa: E402

CC = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
TOOLS = all(shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy'))
BINS, BLOCKS, SAMPLES, LEARN = 64, 32, 2048, 64
WORDS = occupancy.REPORT_WORDS
RESET = struct.pack('<I', 0xFFFFFFFF) * SAMPLES


def word(i, q):
    return (int(i) & 0x3FFF) << 16 | (int(q) & 0x3FFF)


def chunk(samples):
    return struct.pack(f'<{SAMPLES}I', *(word(max(-8192, min(8191, round(z.real))), max(-8192, min(8191, round(z.imag))))
                                         for z in samples))


def noise(rng, sigma):
    """A chunk of noise through y[n] = x[n] + x[n-1]: flat at the LO, nothing at the span's edges."""
    x = [complex(rng.gauss(0, sigma), rng.gauss(0, sigma)) for _ in range(SAMPLES + 1)]
    return [x[n] + x[n + 1] for n in range(SAMPLES)]


def tone(k, amplitude):
    return [amplitude * cmath.exp(2j * math.pi * k * n / BINS) for n in range(SAMPLES)]


def spectrum(samples):
    """occ.c's snapshot(): the mean off, 32 Hann-windowed 64-point transforms, their powers averaged."""
    mean = sum(samples) / len(samples)
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * n / BINS) for n in range(BINS)]
    power = [0.0] * BINS
    for b in range(BLOCKS):
        block = [(samples[b * BINS + n] - mean) * hann[n] for n in range(BINS)]
        for k in range(BINS):
            power[k] += abs(sum(block[n] * cmath.exp(-2j * math.pi * k * n / BINS) for n in range(BINS))) ** 2
    return [p / BLOCKS for p in power]


def unpack(raw):
    """A report's fields as a dict of the program's own numbers."""
    seq, snapshots, peak, cycles, total, level_min, level_max, quiet = struct.unpack_from('<IIIIIffI', raw)
    arrays = [struct.unpack_from(f'<{BINS}{kind}', raw, 32 + 4 * BINS * i) for i, kind in enumerate('ffIfI')]
    return dict(seq=seq, snapshots=snapshots, peak=peak, total=total, level_min=level_min, level_max=level_max, quiet=quiet,
                mean=arrays[0], max=arrays[1], busy=arrays[2], noise=arrays[3], strong=arrays[4])


@unittest.skipUnless(CC, 'a C compiler is needed to build the occupancy program natively')
class Program(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / 'tables.h').write_text(own.dsp_tables())
        cls.exe = tmp / 'occ_check'
        subprocess.run([CC, '-std=gnu11', '-O2', '-Wall', '-Werror', f'-I{tmp}', f'-I{ROOT / "firmware" / "dsp"}',
                        str(ROOT / 'tests' / 'occ_check.c'), '-o', str(cls.exe), '-lm'], check=True)
        cls.band, cls.truth = cls.make_band()
        cls.reports, cls.log = cls.run_program(b''.join(cls.band), 4.0, 100)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def run_program(cls, data, threshold, every):
        ran = subprocess.run([str(cls.exe), str(threshold), str(every)], input=data, capture_output=True, check=True)
        reports = [dict(unpack(ran.stdout[at:at + 4 * WORDS]), raw=ran.stdout[at:at + 4 * WORDS])
                   for at in range(0, len(ran.stdout), 4 * WORDS)]
        return reports, [tuple(map(int, line.split()[1::2])) for line in ran.stderr.decode().splitlines()]

    @staticmethod
    def make_band(snapshots=300, seed=7):
        """Chunks of a band, and for each what was put in it: the gain (1 or 0.1 in amplitude), a
        tone at bin 10 of 15 dB over the noise there, a wide signal over bins 40..47."""
        rng = random.Random(seed)
        chunks, truth = [], []
        for _ in range(snapshots):
            gain = 1.0 if rng.random() < 0.6 else 0.1
            has_tone, has_wide = rng.random() < 1 / 3, rng.random() < 0.2
            samples = noise(rng, 300.0 * gain)
            if has_tone:
                samples = [a + b for a, b in zip(samples, tone(10, 520.0 * gain))]
            if has_wide:
                for k in range(40, 48):
                    phase = rng.random() * 2 * math.pi
                    samples = [a + b * cmath.exp(1j * phase) for a, b in zip(samples, tone(k + 0.5, 900.0 * gain))]
            chunks.append(chunk([s + complex(700, -450) for s in samples]))      # the converter's offset
            truth.append((gain, has_tone, has_wide))
        return chunks, truth

    def test_a_chunks_spectrum_is_the_same_arithmetic_as_here(self):
        rng = random.Random(3)
        samples = [a + b + complex(90, -40) for a, b in zip(noise(rng, 200.0), tone(5, 1500.0))]
        samples = [complex(round(s.real), round(s.imag)) for s in samples]
        (report,), _ = self.run_program(chunk(samples) * LEARN, 4.0, LEARN)
        want = spectrum(samples)
        self.assertEqual(report['snapshots'], LEARN)
        for k in range(BINS):
            self.assertAlmostEqual(report['mean'][k] / want[k], 1.0, delta=2e-4, msg=f'bin {k}')
            self.assertAlmostEqual(report['max'][k] / want[k], 1.0, delta=2e-4, msg=f'bin {k}')
        self.assertEqual(report['peak'], max(max(abs(int(s.real)), abs(int(s.imag))) for s in samples))

    def test_the_first_report_comes_when_the_shape_is_learned_and_then_every_so_many(self):
        self.assertEqual([r['snapshots'] for r in self.reports], [100, 100, 100])
        self.assertEqual([r['seq'] for r in self.reports], [1, 2, 3])
        self.assertEqual([r['total'] for r in self.reports], [100, 200, 300])
        first, second = self.run_program(b''.join(self.band[:100]), 4.0, 10)[0][:2]
        self.assertEqual((first['snapshots'], second['snapshots']), (LEARN, 10),
                         'the snapshots it learned from are in the first report, however few were asked for')

    def test_no_report_before_the_shape_is_learned(self):
        _, log = self.run_program(b''.join(self.band[:LEARN + 2]), 4.0, 1)
        self.assertEqual([reports for _, reports in log], [0] * (LEARN - 1) + [1, 2, 3])

    def test_a_signal_is_busy_as_often_as_it_was_there_at_either_gain(self):
        tone_share = sum(t for _, t, _ in self.truth) / len(self.truth)
        wide_share = sum(w for _, _, w in self.truth) / len(self.truth)
        busy = [sum(r['busy'][k] for r in self.reports) / len(self.truth) for k in range(BINS)]
        self.assertAlmostEqual(busy[10], tone_share, delta=0.01)
        for k in range(41, 47):
            self.assertAlmostEqual(busy[k], wide_share, delta=0.02, msg=f'bin {k}')

    def test_a_transmitters_skirts_are_busy_and_only_where_it_sits_is_strong(self):
        """A tone 67 dB over the noise at bin 10 in every other chunk, and with it, 40 dB under it,
        something in every second bin from 14 to 40: its skirts."""
        rng = random.Random(11)
        skirts = range(14, 41, 2)
        chunks = []
        for t in range(LEARN + 100):
            samples = noise(rng, 2.0)
            if t % 2:
                for k, amplitude in [(10, 2000.0)] + [(k, 20.0) for k in skirts]:
                    turn = cmath.exp(2j * math.pi * rng.random())
                    samples = [a + b * turn for a, b in zip(samples, tone(k, amplitude))]
            chunks.append(chunk(samples))
        reports, _ = self.run_program(b''.join(chunks), 4.0, LEARN + 100)
        report, half = reports[0], (LEARN + 100) // 2
        self.assertEqual((report['busy'][10], report['strong'][10]), (half, half))
        self.assertEqual([report['busy'][k] for k in skirts], [half] * len(skirts), 'the skirts are busy')
        self.assertEqual(sum(report['strong'][k] for k in range(BINS) if not 8 <= k <= 12), 0,
                         'and strong nowhere but where the transmitter sits')
        for r in self.reports:
            self.assertEqual(r['strong'][10], r['busy'][10], 'alone in the span, a signal is strong whenever it is busy')

    def test_the_shape_is_learned_from_the_quiet_snapshots_whatever_their_gain(self):
        quiet = sum(1 for _, has_tone, has_wide in self.truth[:LEARN] if not has_tone and not has_wide)
        gains = {gain for gain, has_tone, has_wide in self.truth[:LEARN] if not has_tone and not has_wide}
        self.assertEqual(gains, {1.0, 0.1}, 'the band made here is quiet at both gains while it learns')
        for r in self.reports:        # a few bins' worth of signal still counts as alike: the median over them is the shape
            self.assertGreaterEqual(r['quiet'], quiet - 2)
            self.assertLessEqual(r['quiet'], LEARN)

    def test_a_band_too_busy_to_learn_from_is_said_and_learned_when_it_quietens(self):
        """Every chunk with something else in it, over most of the span: no two are alike. Then
        the band made above."""
        rng = random.Random(5)
        crowded = []
        for _ in range(LEARN + 40):
            samples = noise(rng, 30.0)
            for k in rng.sample(range(BINS), 40):
                samples = [a + b for a, b in zip(samples, tone(k, rng.uniform(100.0, 3000.0)))]
            crowded.append(chunk(samples))
        reports, _ = self.run_program(b''.join(crowded + self.band[:2 * LEARN + 24]), 4.0, LEARN)
        self.assertLess(reports[0]['quiet'], 8, 'it has nothing to go by, and says so')
        self.assertGreaterEqual(reports[-1]['quiet'], 8, 'the next 64 it kept had quiet ones among them: 24 of the band')
        last = reports[-1]
        self.assertLessEqual(sum(last['busy'][k] for k in range(BINS) if not (8 <= k <= 12 or 38 <= k <= 49)), 2,
                             'and from then on noise is not busy')

    def test_noise_alone_is_never_busy(self):
        busy = [sum(r['busy'][k] for r in self.reports) for k in range(BINS)]
        quiet = [k for k in range(BINS) if not (8 <= k <= 12 or 38 <= k <= 49)]
        self.assertLessEqual(sum(busy[k] for k in quiet), 2, [busy[k] for k in quiet])

    def test_the_converters_offset_is_not_a_signal(self):
        busy = [sum(r['busy'][k] for r in self.reports) for k in (63, 0, 1)]
        self.assertEqual(busy, [0, 0, 0])
        for r in self.reports:
            self.assertLess(r['mean'][0], 3 * r['mean'][3], 'the bin at the LO holds noise, as its neighbours do')

    def test_the_noise_it_reports_is_the_filters_shape(self):
        """y[n] = x[n] + x[n-1] passes |1 + e^(-jw)|^2 = 4 cos^2(w / 2): bin 16 is half of bin 0."""
        noise_ = self.reports[-1]['noise']
        for k in (4, 16, 20, 24):
            want = math.cos(math.pi * k / BINS) ** 2 / math.cos(math.pi * 2 / BINS) ** 2
            self.assertAlmostEqual(noise_[k] / noise_[2], want, delta=0.25 * want, msg=f'bin {k}')
            self.assertAlmostEqual(noise_[BINS - k] / noise_[2], want, delta=0.25 * want, msg=f'bin {BINS - k}')

    def test_how_far_the_gain_moved_is_in_the_report(self):
        for r in self.reports:
            self.assertAlmostEqual(10 * math.log10(r['level_max'] / r['level_min']), 20.0, delta=2.5)

    def test_a_reset_forgets_the_shape_and_the_sums(self):
        data = b''.join(self.band[:LEARN + 5]) + RESET + b''.join(self.band[:LEARN])
        reports, log = self.run_program(data, 4.0, 200)
        self.assertEqual([reports_ for _, reports_ in log][-2:], [0, 0], 'no report of 200 from 64 + 5 + 64 snapshots')
        reports, _ = self.run_program(data + b''.join(self.band[LEARN:LEARN + 136]), 4.0, 200)
        self.assertEqual((reports[0]['snapshots'], reports[0]['total']), (200, 200), 'counted from the reset')

    def test_silence_is_survived(self):
        (report,), _ = self.run_program(chunk([0j] * SAMPLES) * (LEARN + 1), 4.0, LEARN + 1)
        self.assertEqual(sum(report['busy']), 0)
        self.assertTrue(all(math.isfinite(v) for v in report['noise'] + report['mean']))

    def test_the_hosts_reading_of_a_report(self):
        report = self.reports[-1]
        decoded = occupancy.decode(list(struct.unpack(f'<{WORDS}I', report['raw'])), 40_000_000, 2_462_000_000)
        at = {b['hz']: b for b in decoded['bins']}
        self.assertEqual(at[2_462_000_000 + 10 * 625_000]['busy'], round(report['busy'][10] / 100, 3))
        self.assertEqual(decoded['bins'][0]['hz'], 2_462_000_000 - 32 * 625_000)
        self.assertEqual((decoded['report'], decoded['snapshots'], decoded['snapshots_since_on']), (3, 100, 300))
        self.assertEqual(decoded['learned_from'], report['quiet'])
        self.assertAlmostEqual(decoded['gain_span_db'], 20.0, delta=2.5)


POOL, RING, CHUNK = 0xE0400000, 0x100000, 8192
CORE_SAMPLES = 0x86200000 + 0xD000
EN, RESET_FLAG = 5, 1
OFF, RUNNING, NO_DSP, DMA_BUSY, DMA_FAILED, DSP_SILENT = range(6)


@unittest.skipUnless(CC, 'a C compiler is needed to build the WM\'s occupancy steps natively')
class Steps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exe = Path(cls.tmp.name) / 'occ_wm_check'
        defines = [f'-D{k}={v}' for k, v in blob.schedule_defines().items()]
        subprocess.run([CC, '-std=gnu11', '-O1', '-Wall', '-Werror', '-Wno-unused-function', '-Wno-int-to-pointer-cast',
                        f'-I{ROOT / "include"}', f'-I{ROOT / "firmware" / "stream"}', *defines,
                        str(ROOT / 'tests' / 'occ_wm_check.c'), '-o', str(cls.exe)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_steps(self, period, every, steps, *how):
        ran = subprocess.run([str(self.exe), str(period), str(every), str(steps), *how], capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stdout[-300:])
        lines = [line.split() for line in ran.stdout.splitlines()]
        for line in lines:
            self.assertIn(line[0], ('cfg', 'copy', 'ask', 'state', 'end'), ran.stdout)
        of = lambda kind: [tuple(int(v, 0) for v in line[1:]) for line in lines if line[0] == kind]
        return of('copy'), of('ask'), [s for s, in of('state')], [c for c, in of('cfg')], of('end')[0]

    def test_a_run(self):
        copies, asks, states, cfgs, (state, snapshots, seq, same, cfg) = self.run_steps(10, 4, 400)
        self.assertEqual(states, [RUNNING])
        self.assertGreater(len(copies), 30)
        for at, source, dest, length in copies:
            self.assertEqual((source, dest, length), (POOL + (at - 1) * CHUNK % RING, CORE_SAMPLES, CHUNK),
                             'the chunk published last, whole, into the core\'s sample memory')
        self.assertEqual([source for _, _, _, _, source in asks], [source for _, source, _, _ in copies[:len(asks)]],
                         'every snapshot is asked for once, over its own copy')
        self.assertEqual([flags for _, _, flags, _, _ in asks], [RESET_FLAG] + [0] * (len(asks) - 1),
                         'the core is told to forget once, at the start')
        self.assertEqual([req for _, req, _, _, _ in asks], list(range(42, 42 + len(asks))), 'from the number it found')
        self.assertEqual({every for _, _, _, every, _ in asks}, {4})
        self.assertIn(snapshots, (len(asks), len(asks) - 1))
        self.assertEqual((state, seq, same), (RUNNING, 2 * (snapshots // 4), 1), 'the last report is in the page, whole')
        self.assertEqual(cfg & EN, EN)

    def test_snapshots_are_the_period_apart_give_or_take_half(self):
        copies = self.run_steps(60, 1000, 3000)[0]
        gaps = [b[0] - a[0] for a, b in zip(copies, copies[1:])]
        self.assertGreater(len(set(gaps)), 8, 'not a fixed beat: what repeats in the air is not always met or missed')
        self.assertGreaterEqual(min(gaps), 30)
        self.assertLessEqual(max(gaps), 90 + 3 * 4, 'a snapshot takes three steps, and the next waits from its end')
        self.assertAlmostEqual(sum(gaps) / len(gaps), 60 + 3 * 3, delta=6)

    def test_the_engine_is_set_up_stopped_and_enabled_last(self):
        cfgs = self.run_steps(10, 4, 20)[3]
        self.assertEqual([c & EN for c in cfgs], [0, EN])
        self.assertEqual([c & 0xA for c in cfgs], [0, 0], 'the busy bits are the engine\'s to set')

    def test_a_core_that_does_not_run_is_never_touched(self):
        for how, why in (('nodsp', 'in reset'), ('otherimage', 'another program')):
            copies, asks, states, cfgs, end = self.run_steps(10, 4, 50, how)
            self.assertEqual((copies, asks, states, cfgs), ([], [], [NO_DSP], []), why)
            self.assertEqual(end[0], NO_DSP)

    def test_the_core_is_asked_only_when_the_copy_says_it_is_done(self):
        copies, asks, states, _, end = self.run_steps(10, 4, 400, 'lateddone')      # run_steps refuses an 'early' line
        self.assertEqual(states, [RUNNING])
        self.assertGreater(len(asks), 20)

    def test_what_ends_it(self):
        for how, want, copies_made in (('dmabusy', DMA_BUSY, 0), ('stuckcopy', DMA_FAILED, 0), ('silent', DSP_SILENT, 1)):
            copies, asks, states, cfgs, end = self.run_steps(10, 4, 400, how)
            self.assertEqual(states[-1], want, how)
            self.assertEqual(len(copies), copies_made, how)
            self.assertEqual(end[4] & EN, 0, f'{how}: the engine is left stopped')

    def test_off_stops_the_engine_and_on_starts_afresh(self):
        copies, asks, states, cfgs, end = self.run_steps(10, 4, 400, 'off@200', 'on@260')
        self.assertEqual(states, [RUNNING, OFF, RUNNING])
        self.assertEqual([c & EN for c in cfgs], [0, EN, 0, 0, EN])
        self.assertEqual(sum(1 for _, _, flags, _, _ in asks if flags == RESET_FLAG), 2, 'the core forgets at each start')
        self.assertFalse([c for c in copies if 3 * 201 < c[0] < 3 * 260], 'nothing is copied while it is off')

    def test_a_failure_is_forgotten_when_turned_off_and_found_again_if_its_cause_lasts(self):
        copies, asks, states, cfgs, end = self.run_steps(10, 4, 400, 'silent', 'off@250', 'on@300')
        self.assertEqual(states, [RUNNING, DSP_SILENT, OFF, DSP_SILENT])
        self.assertEqual(len(copies), 1, 'a core that still owes an answer is given no second snapshot')

    def test_a_streams_end_leaves_the_engine_stopped(self):
        *_, states, cfgs, end = self.run_steps(10, 4, 100, 'settle')
        self.assertEqual((states, end[0], end[4] & EN), ([RUNNING, OFF], OFF, 0))


@unittest.skipUnless(TOOLS, 'needs clang, ld.lld and llvm-objcopy')
class Image(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.itcm, cls.dtcm = own.dsp_code()

    def test_the_code_fits_a_cores_itcm_and_begins_with_the_start(self):
        self.assertLessEqual(len(self.itcm), own.DSP_TCM_SIZE)
        self.assertEqual(struct.unpack_from('<I', self.itcm)[0], 0x30047073, 'csrci mstatus, 8: interrupts off first')

    def test_the_box_and_the_request_block_are_loaded_cleared(self):
        self.assertEqual(self.dtcm[:own.DSP_DATA], bytes(own.DSP_DATA))
        self.assertGreater(len(self.dtcm), own.DSP_DATA)

    def test_every_core_gets_the_same_code_and_tables(self):
        regions = container.read_ram_container(own.dsp_container())
        self.assertEqual([address for address, _, _ in regions],
                         [base + (core << 22) for core in range(own.DSP_CORES) for base in (own.DSP_ITCM, own.DSP_DTCM)])
        for address, payload, _ in regions:
            self.assertEqual(payload, self.dtcm if address & 0x200000 else self.itcm)

    def test_the_tables(self):
        text = own.dsp_tables()
        self.assertIn('static const float hann[64]', text)
        self.assertIn('static const unsigned char rev[64] = { 0, 32, 16, 48,', text)
        with self.assertRaises(ValueError):
            own.dsp_tables(48)


if __name__ == '__main__':
    unittest.main()
