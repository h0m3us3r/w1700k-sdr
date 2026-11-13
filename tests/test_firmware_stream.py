# SPDX-License-Identifier: MIT
"""The stream firmware's packing schedules: the table stream/blob.py generates against the
RADIO_PACKING_* codes the host sends, and stream.h's helpers built natively (tests/stream_sched.c)
against that table: every chunk's role, its index among its packer's chunks and its buffer."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'firmware'))
sys.path.insert(0, str(ROOT / 'host'))

import host_fake  # noqa: E402
from stream import blob  # noqa: E402
from w1700k import stream as host_stream, wm  # noqa: E402

CC = shutil.which('cc')
POOL, RING, CHUNK, PACKED = 0xE0400000, 0x100000, 8192, 7168      # mcu_map.h, layout.h, iq_format.h
WA_NBUF, WM_NBUF = 3, 4                                           # probe_page.h *_PACKBUF_COUNT


class Table(unittest.TestCase):
    def test_codes(self):
        """A packing's index in blob.PACKINGS is its RADIO_PACKING_* code, and the host's."""
        h = host_fake.header_defines(ROOT / 'firmware' / 'radio' / 'radio_cmd.h')
        names = [p.name for p in blob.PACKINGS]
        self.assertEqual([h[f'RADIO_PACKING_{n.upper()}'] for n in names], list(range(len(names))))
        self.assertEqual(h['RADIO_PACKINGS'], len(names))
        self.assertEqual(wm.RADIO_PACKING, {n: i for i, n in enumerate(names)})
        self.assertEqual({p.name: (p.wa + p.wm) / blob.PERIOD for p in blob.PACKINGS}, host_stream.PACKED_SHARE)

    def test_schedules(self):
        """Each packer's positions are disjoint, spread evenly and numbered in order."""
        for packing in blob.PACKINGS:
            wa_mask, wm_mask, slots = blob.schedule(packing)
            self.assertEqual(wa_mask & wm_mask, 0, packing.name)
            self.assertEqual((bin(wa_mask).count('1'), bin(wm_mask).count('1')), (packing.wa, packing.wm))
            for mask in (wa_mask, wm_mask):
                at = [i for i in range(blob.PERIOD) if mask >> i & 1]
                self.assertEqual([slots[i] for i in at], list(range(len(at))), packing.name)
                if at:
                    gaps = {(b - a) % blob.PERIOD or blob.PERIOD for a, b in zip(at, at[1:] + at[:1])}
                    self.assertLessEqual(max(gaps) - min(gaps), 1, f'{packing.name}: spread as evenly as it goes')
        self.assertEqual(blob.schedule(blob.RAW)[:2], (0, 0))
        self.assertEqual(blob.schedule(blob.HALF)[:2], (0x4444, 0x1111), 'half is what it always was')
        wa_mask, wm_mask, _ = blob.schedule(blob.FULL)
        self.assertEqual(wa_mask | wm_mask, (1 << blob.PERIOD) - 1, 'full packs every chunk')
        self.assertEqual((wa_mask, wm_mask), (0xAAAA, 0x5555), 'the packers alternate')
        wa_mask, wm_mask, _ = blob.schedule(blob.SPLIT)
        self.assertEqual((wa_mask, wm_mask), (0x4912, 0x1249), 'split: 5 chunks of 16 each, 6 left raw for the SoC')
        self.assertTrue(blob.SPLIT.reduce and not any(p.reduce for p in (blob.RAW, blob.HALF, blob.FULL)))

    def test_defines(self):
        defines = blob.schedule_defines()
        self.assertEqual(defines['PERIOD'], '16u')
        self.assertEqual(defines['SCHEDULES'].count('{'), 2 * len(blob.PACKINGS))


@unittest.skipUnless(CC, 'a C compiler is needed to build stream.h natively')
class Helpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp, 'stream_sched')
            defines = [f'-D{k}={v}' for k, v in blob.schedule_defines().items()]
            subprocess.run([CC, '-std=gnu11', '-O1', '-Wall', '-Werror', '-Wno-unused-function',
                            '-Wno-int-to-pointer-cast', f'-I{ROOT / "include"}', f'-I{ROOT / "firmware" / "stream"}',
                            *defines, str(ROOT / 'tests' / 'stream_sched.c'), '-o', str(exe)], check=True)
            lines = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout.splitlines()
        cls.rows = [tuple(int(v, 0) for v in line.split()) for line in lines if not line.startswith('first')]
        cls.first = {int(p): (int(wa), int(wm)) for _, p, wa, wm in (l.split() for l in lines if l.startswith('first'))}

    def test_roles_and_indices(self):
        for code, packing in enumerate(blob.PACKINGS):
            wa_mask, wm_mask, slots = blob.schedule(packing)
            rows = [r for r in self.rows if r[0] == code]
            self.assertEqual(len(rows), 4 * blob.PERIOD)
            for _, k, role, index, address in rows:
                bit = 1 << k % blob.PERIOD
                want = 2 if wa_mask & bit else 1 if wm_mask & bit else 0
                self.assertEqual(role, want, (packing.name, k))
                if role:
                    n = packing.wa if role == 2 else packing.wm
                    self.assertEqual(index, k // blob.PERIOD * n + slots[k % blob.PERIOD], (packing.name, k))
                else:
                    self.assertEqual(address, POOL + k * CHUNK % RING, 'a raw chunk goes out of the capture ring')
            if wa_mask:
                self.assertEqual(self.first[code][0], min(i for i in range(blob.PERIOD) if wa_mask >> i & 1))
            if wm_mask:
                self.assertEqual(self.first[code][1], min(i for i in range(blob.PERIOD) if wm_mask >> i & 1))

    def test_buffers_cycle(self):
        """A packer's chunks take its buffers in turn: a buffer comes round again only after the
        packer's whole set, and no buffer overlaps another or the capture ring."""
        for code, packing in enumerate(blob.PACKINGS):
            for role, count in ((2, WA_NBUF), (1, WM_NBUF)):
                used = [(index, address) for p, _, r, index, address in self.rows if p == code and r == role]
                if not used:
                    continue
                self.assertEqual([i for i, _ in used], list(range(len(used))), 'indices run on from 0')
                addresses = [a for _, a in used]
                self.assertEqual(len(set(addresses)), count, packing.name)
                for i, address in enumerate(addresses):
                    self.assertEqual(address, addresses[i % count], (packing.name, role, i))
                spans = sorted(set(addresses))
                for a, b in zip(spans, spans[1:]):
                    self.assertGreaterEqual(b - a, PACKED, 'buffers do not overlap')
                self.assertFalse(any(POOL <= a < POOL + RING for a in spans))


if __name__ == '__main__':
    unittest.main()
