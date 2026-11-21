# SPDX-License-Identifier: MIT
"""The DSP copy tools (tools/dsp): the MCU memory they put code, rings and a test pattern in is
memory nothing of ours uses, and, where the RISC-V toolchain is installed, they build."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'firmware'))

import container  # noqa: E402
import own  # noqa: E402

DSP = ROOT / 'tools' / 'dsp'
TOOLS = all(shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy'))
ULM1 = re.compile(r'0x009[0-9a-fA-F]{5}\b')          # an address in the WM's code memory
HELPER_LIMIT = 2048                                   # the helper's code ends where its rings begin
PATTERN, PATTERN_SIZE = 0x00936000, 8192              # dma_bench.py's largest pattern


def macro(name):
    text = (ROOT / 'include' / 'hw' / 'mt7996' / 'mcu_map.h').read_text()
    return int(re.search(rf'#define\s+{name}\s+(0x[0-9a-fA-F]+)u', text).group(1), 16)


BASE, SIZE = macro('MT7996_WM_ULM1_TOP_FREE_BASE'), macro('MT7996_WM_ULM1_TOP_FREE_SIZE')


class Memory(unittest.TestCase):
    def test_the_range_is_above_everything_our_firmware_puts_in_ulm1(self):
        self.assertGreaterEqual(BASE, own.PRODUCER_BASE + own.PRODUCER_SIZE)
        self.assertEqual(BASE + SIZE, own.WM_LIMIT)
        self.assertEqual(own.WM_LIMIT, macro('MT7996_WM_ULM1_BASE') + macro('MT7996_WM_ULM1_SIZE'))

    def test_every_address_the_tools_use_there_is_in_the_range(self):
        found = {}
        for path in sorted(DSP.glob('*.py')) + sorted(DSP.glob('*.c')) + sorted(DSP.glob('*.S')):
            for text in ULM1.findall(path.read_text()):
                found.setdefault(int(text, 16), path.name)
        self.assertIn(PATTERN, found)
        for address, name in found.items():
            self.assertTrue(BASE <= address < BASE + SIZE, f'{address:#010x} in {name}')

    def test_the_largest_pattern_ends_inside_it(self):
        self.assertLessEqual(PATTERN + PATTERN_SIZE, BASE + SIZE)

    def test_none_of_it_is_in_the_packers_buffers(self):
        low, size = macro('MT7996_WM_ULM1_FREE_BASE'), macro('MT7996_WM_ULM1_FREE_SIZE')
        self.assertLessEqual(low + size, BASE)


@unittest.skipUnless(TOOLS, 'needs clang, ld.lld and llvm-objcopy')
class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.out = Path(cls.work.name)
        cls.ran = subprocess.run([sys.executable, str(DSP / 'build.py'), '--out', str(cls.out)],
                                 capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_it_builds(self):
        self.assertEqual(self.ran.returncode, 0, self.ran.stderr)
        self.assertFalse(json.loads(self.ran.stdout)['hardware_validated'])

    def test_the_helper_fits_below_its_rings(self):
        self.assertTrue(0 < (self.out / 'wm-dma.bin').stat().st_size <= HELPER_LIMIT)

    def test_the_helper_is_linked_where_the_tools_put_it(self):
        symbols = subprocess.run(['llvm-objdump', '-t', str(self.out / 'wm-dma.elf')], capture_output=True,
                                 text=True) if shutil.which('llvm-objdump') else None
        if symbols is None:
            self.skipTest('needs llvm-objdump')
        entry = [line for line in symbols.stdout.splitlines() if line.endswith(' dma_trial')]
        self.assertEqual(len(entry), 1, symbols.stdout)
        self.assertEqual(int(entry[0].split()[0], 16), 0x00934000)

    def test_the_container_gives_each_dsp_core_its_code_and_a_cleared_page(self):
        code = (self.out / 'dsp.bin').read_bytes()
        regions = container.read_ram_container((self.out / 'w1700k_dma_test.bin').read_bytes())
        self.assertEqual([address for address, _, _ in regions],
                         [0x86000000 + (band << 22) + data for band in range(3) for data in (0, 0x200000)])
        for address, payload, _ in regions:
            if address & 0x200000:
                self.assertEqual(payload, bytes(4096))
            else:
                self.assertEqual(payload[:len(code)], code)


if __name__ == '__main__':
    unittest.main()
