# SPDX-License-Identifier: MIT
"""include/hw/mt7996/iq_reduce.h against the formats written out in iq_reduce_ref.py: the encoder
byte for byte, the decoder sample for sample, and the error within half a step."""
import os
import random
import shutil
import struct
import subprocess
import tempfile
import unittest

import iq_reduce_ref as ref

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLICE = 2048


def word(i, q):
    return ((i & 0x3FFF) << 16) | (q & 0x3FFF)


def slices():
    rng = random.Random(11)
    out = []
    for peak in (0, 1, 5, 6, 7, 8, 63, 64, 127, 128, 500, 3000, 8191):
        w = [word(rng.randint(-peak, peak), rng.randint(-peak, peak)) for _ in range(SLICE)]
        w[rng.randrange(SLICE)] = word(peak, -peak)
        out.append(w)
    out.append([word(-8192, -8192)] * SLICE)
    out.append([word(0, 0)] * (SLICE - 1) + [word(0, -8192)])
    out.append([word(round(rng.gauss(0, 700)), round(rng.gauss(0, 700))) for _ in range(SLICE)])
    return out


@unittest.skipUnless(shutil.which(os.environ.get('CC', 'cc')), 'no C compiler')
class ReduceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.tool = os.path.join(cls.dir.name, 'iq_reduce_check')
        subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-I' + os.path.join(ROOT, 'include'), os.path.join(ROOT, 'tests', 'iq_reduce_check.c'),
                        '-o', cls.tool], check=True)
        cls.slices = slices()
        cls.raw = b''.join(struct.pack('<%dI' % SLICE, *w) for w in cls.slices)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def run_tool(self, *args):
        return subprocess.run([self.tool, *map(str, args)], input=self.raw, capture_output=True, check=True).stdout

    def test_the_multipliers_are_the_formula(self):
        with open(os.path.join(ROOT, 'include', 'hw', 'mt7996', 'iq_reduce.h')) as f:
            text = f.read()
        table = text[text.index('iq_reduce_m[IQ_REDUCE_SCALE_MAX + 1] = {'):]
        table = table[table.index('{') + 1:table.index('}')]
        self.assertEqual([int(v) for v in table.replace('\n', ' ').split(',') if v.strip()], ref.M)

    def test_encoder_matches_the_format_byte_for_byte(self):
        for fmt in ref.BYTES:
            out = self.run_tool(fmt)
            size = 1 + ref.BYTES[fmt]
            self.assertEqual(len(out), size * len(self.slices))
            for k, w in enumerate(self.slices):
                code, data = ref.encode(w, fmt)
                self.assertEqual(out[k * size], code, 'format %d slice %d scale' % (fmt, k))
                self.assertEqual(out[k * size + 1:(k + 1) * size], data, 'format %d slice %d' % (fmt, k))

    def test_decoder_restores_within_half_a_step(self):
        for fmt in ref.BYTES:
            out = self.run_tool(fmt, 'decode')
            for k, w in enumerate(self.slices):
                got = [(ref.signed14(v >> 16), ref.signed14(v))
                       for v in struct.unpack_from('<%dI' % SLICE, out, k * SLICE * 4)]
                self.assertEqual(got, ref.restored(w, fmt), 'format %d slice %d' % (fmt, k))
                bound = ref.step_of(w, fmt) / 2 + 1
                for v, (i, q) in zip(w, got):
                    # -8192 has no positive twin: it comes back one step short at most
                    slack = ref.step_of(w, fmt) if -8192 in (ref.signed14(v >> 16), ref.signed14(v)) else 0
                    self.assertLessEqual(abs(i - ref.signed14(v >> 16)), bound + slack)
                    self.assertLessEqual(abs(q - ref.signed14(v)), bound + slack)

    def test_a_slice_within_the_range_is_exact(self):
        for fmt, limit in ref.RANGE.items():
            for k, w in enumerate(self.slices[:5]):
                peak = max(max(abs(ref.signed14(v >> 16)), abs(ref.signed14(v))) for v in w)
                if peak <= limit:
                    self.assertEqual(ref.restored(w, fmt), [(ref.signed14(v >> 16), ref.signed14(v)) for v in w])
                    self.assertEqual(ref.scale(w, fmt), 0)


if __name__ == '__main__':
    unittest.main()
