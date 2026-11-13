# SPDX-License-Identifier: MIT
"""The packers' kernels, run from their machine code (tests/rv32.py): pack.S against the 8-to-7
pack, reduce14.S against the radio-reduced chunk written out in iq_reduce_ref.py and against
include/hw/mt7996/iq_reduce.h's model and decoder. Bytes outside what a kernel may touch trap."""
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'firmware'))

import iq_reduce_ref as ref  # noqa: E402
import rv32  # noqa: E402
from stream import blob  # noqa: E402

TOOLS = [shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy')]
CC = shutil.which(os.environ.get('CC', 'cc'))
BASE, SRC, DST = 0x00100000, 0xE0400000, 0x00910000
SLICE = 2048


def word(i, q):
    return ((i & 0x3FFF) << 16) | (q & 0x3FFF)


def assemble(source, tmp):
    """The machine code of one stream .S, linked at BASE."""
    obj, elf, raw = Path(tmp, 'k.o'), Path(tmp, 'k.elf'), Path(tmp, 'k.bin')
    subprocess.run([TOOLS[0], *blob.CFLAGS, '-c', str(ROOT / 'firmware' / 'stream' / source), '-o', str(obj)], check=True)
    subprocess.run([TOOLS[1], '--no-relax', '-Ttext', hex(BASE), '-e', hex(BASE), '-o', str(elf), str(obj)], check=True)
    subprocess.run([TOOLS[2], '-O', 'binary', '-j', '.text', str(elf), str(raw)], check=True)
    return raw.read_bytes()


def run(code, words, out_bytes, *extra):
    """kernel(SRC, DST, len, *extra) on `words`; (the bytes it wrote, instructions executed)."""
    m = rv32.Machine(code, BASE)
    m.load(SRC, struct.pack('<%dI' % len(words), *words))
    m.load(DST, bytes(out_bytes))                  # exactly the bytes it may write
    m.call(BASE, SRC, DST, 4 * len(words), *extra)
    return m.dump(DST, out_bytes), m.executed


def slices():
    rng = random.Random(5)
    out = [[word(rng.randint(-8192, 8191), rng.randint(-8192, 8191)) for _ in range(SLICE)]]
    for peak in (0, 1, 63, 64, 127, 1000, 8191):
        out.append([word(rng.randint(-peak - 1, peak), rng.randint(-peak - 1, peak)) for _ in range(SLICE)])
    out.append([word(-8192, 8191), word(8191, -8192)] * (SLICE // 2))
    out.append([word(n, -n) for n in range(SLICE)])
    return out


@unittest.skipUnless(all(TOOLS), 'needs clang, ld.lld and llvm-objcopy')
class Kernels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pack = assemble('pack.S', cls.tmp.name)
        cls.reduce = assemble('reduce14.S', cls.tmp.name)
        cls.slices = slices()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pack_is_the_8_to_7_pack(self):
        for k, w in enumerate(self.slices[:3]):
            got, executed = run(self.pack, w, 7168)
            self.assertEqual(got, struct.pack('<%dI' % (SLICE // 8 * 7), *ref.pack28(w)), 'slice %d' % k)
        self.assertEqual(executed, 128 * 75 + 3, '75 instructions a pass of 16 samples, and three around them')

    def test_reduce14_is_the_radio_reduced_chunk(self):
        for shift in range(8):
            for k, w in enumerate(self.slices):
                got, executed = run(self.reduce, w, ref.RADIO14_BYTES - 4, shift)
                self.assertEqual(got, ref.radio14_encode(w, shift)[:-4], 'slice %d, shift %d' % (k, shift))
        self.assertEqual(executed, 128 * 87 + 8, '87 instructions a pass of 16 samples, and eight around them')

    def test_reduce14_in_the_wm_pieces(self):
        """The WM reduces a chunk a quarter at a time, each piece's output after the last's."""
        w = self.slices[0]
        whole, _ = run(self.reduce, w, 3584, 3)
        m = rv32.Machine(self.reduce, BASE)
        m.load(SRC, struct.pack('<%dI' % SLICE, *w))
        m.load(DST, bytes(3584))
        for piece in range(4):
            m.call(BASE, SRC + piece * 2048, DST + piece * 896, 2048, 3)
        self.assertEqual(m.dump(DST, 3584), whole)

    def test_a_kernel_touches_nothing_else(self):
        m = rv32.Machine(self.reduce, BASE)
        m.load(SRC, struct.pack('<%dI' % SLICE, *self.slices[0]))
        m.load(DST, bytes(3584 - 4))               # one word short
        with self.assertRaisesRegex(rv32.Trap, 'store to'):
            m.call(BASE, SRC, DST, 8192, 0)

    @unittest.skipUnless(CC, 'no C compiler')
    def test_the_model_and_the_decoder_agree_with_the_kernel(self):
        tool = os.path.join(self.tmp.name, 'iq_reduce_check')
        subprocess.run([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-I' + str(ROOT / 'include'),
                        str(ROOT / 'tests' / 'iq_reduce_check.c'), '-o', tool], check=True)
        raw = b''.join(struct.pack('<%dI' % SLICE, *w) for w in self.slices)
        for shift in (0, 3, 7):
            chunks = subprocess.run([tool, 'radio14', str(shift)], input=raw, capture_output=True, check=True).stdout
            back = subprocess.run([tool, 'radio14', str(shift), 'decode'], input=raw, capture_output=True, check=True).stdout
            for k, w in enumerate(self.slices):
                got, _ = run(self.reduce, w, 3584, shift)
                chunk = chunks[k * ref.RADIO14_BYTES:(k + 1) * ref.RADIO14_BYTES]
                self.assertEqual(chunk, got + struct.pack('<I', ref.RADIO14_TRAILER | shift), 'slice %d, shift %d' % (k, shift))
                held = [(ref.signed14(v >> 16), ref.signed14(v))
                        for v in struct.unpack_from('<%dI' % SLICE, back, k * SLICE * 4)]
                self.assertEqual(held, ref.radio14_restored(w, shift), 'slice %d, shift %d' % (k, shift))
                if ref.radio14_fits(w, shift):     # within the shift's range: within half a step
                    for v, (i, q) in zip(w, held):
                        self.assertLessEqual(abs(i - ref.signed14(v >> 16)), max(1 << shift, 2) // 2)
                        self.assertLessEqual(abs(q - ref.signed14(v)), max(1 << shift, 2) // 2)


if __name__ == '__main__':
    unittest.main()
