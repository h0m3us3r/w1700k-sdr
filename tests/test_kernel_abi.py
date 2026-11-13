# SPDX-License-Identifier: MIT
"""Driver ABI layout, the unit-side clients, and source hygiene."""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from kernel_build import CC, INCLUDE, ROOT, SDR, TESTS, UAPI, CProgram, have_compiler

CLIENTS = {'probe': 'mt7996-probe', 'download': 'mt7996-download',
           'stream': 'mt7996-stream', 'ctl': 'mt7996-ctl', 'ledd': 'w1700k-led'}

# The ABI the clients are built against (W1700K_SDR_ABI_VERSION 5). A change
# here is an ABI change: bump the version and rebuild module and clients.
# Additions a module announces by a capability (LED: W1700K_SDR_CAP_LED) or
# refuses by -EINVAL where it does not know them (a stream export's flags) leave
# the version: a client that needs them asks, and says so when they are missing.
LAYOUT = {
    'w1700k_sdr_info': 32, 'w1700k_sdr_download': 64, 'w1700k_sdr_reset': 32,
    'w1700k_sdr_mem_read': 24,
    'w1700k_sdr_stream_ring': 64, 'w1700k_sdr_stream_export': 32, 'w1700k_sdr_led': 4,
    'w1700k_sdr_export_header': 16, 'w1700k_sdr_stream_rx': 208, 'w1700k_sdr_ctl': 800,
    'w1700k_sdr_info.bar0_size': 24, 'w1700k_sdr_download.result': 56,
    'w1700k_sdr_stream_ring.ctrl': 64, 'w1700k_sdr_stream_rx.export': 32,
    'w1700k_sdr_stream_rx.buffers': 64, 'w1700k_sdr_stream_rx.export_slices': 168,
    'w1700k_sdr_stream_export.dst_mac': 18, 'w1700k_sdr_stream_export.drain_ms': 28,
    'w1700k_sdr_stream_export.flags': 26, 'w1700k_sdr_led.white': 3,
    'w1700k_sdr_export_header.seq': 8, 'w1700k_sdr_ctl.read': 528,
    'w1700k_sdr_ctl.result': 784,
    'W1700K_SDR_GET_INFO': 0x80205100, 'W1700K_SDR_ACQUIRE_HOST': 0x5111,
    'W1700K_SDR_DOWNLOAD': 0xc0405115, 'W1700K_SDR_RESET_MT': 0x80205116,
    'W1700K_SDR_READ_MEM': 0x40185117, 'W1700K_SDR_STREAM_RX': 0xc0d0511f,
    'W1700K_SDR_CTL': 0xc3205120, 'W1700K_SDR_LED': 0x40045121, 'abi': 5,
}


@unittest.skipUnless(have_compiler(), 'needs a C compiler')
class KernelAbiTests(unittest.TestCase):
    def test_layout_and_ioctl_numbers(self):
        program = CProgram('uapi-test.c')
        self.addCleanup(program.close)
        result = program.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), LAYOUT)

    def test_header_is_strict_c99(self):
        result = subprocess.run([CC, '-std=c99', '-pedantic', '-Wall', '-Wextra', '-Werror',
                                 '-fsyntax-only', '-I', str(UAPI), str(TESTS / 'uapi-test.c')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(have_compiler(), 'needs a C compiler')
class KernelClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='w1700k-sdr-clients-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.bin = Path(cls.tmp.name)
        for source, name in CLIENTS.items():
            result = subprocess.run(
                [CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(UAPI),
                 '-I', str(INCLUDE),
                 str(SDR / f'{source}.c'), '-lpthread', '-lm', '-o', str(cls.bin / name)],
                capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(f'{source}.c: {result.stderr}')

    def run_client(self, name, *args):
        return subprocess.run([str(self.bin / name), *args], capture_output=True,
                              text=True, timeout=10)

    def test_usage_errors_exit_2(self):
        bad = {'mt7996-probe': ('/dev/null', '0x00800001', '4'),
               'mt7996-download': ('/dev/null', 'image.bin'),
               'mt7996-stream': ('/dev/null', '--seconds', '0'),
               'mt7996-ctl': ('/dev/null',),
               'w1700k-led': ('/dev/null', '--colour', 'mauve')}
        for name in CLIENTS.values():
            with self.subTest(client=name):
                result = self.run_client(name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('usage', result.stderr.lower())
                self.assertEqual(self.run_client(name, *bad[name]).returncode, 2)
        # The vendor producer's IRQ5 entry is gone with ABI 5.
        self.assertEqual(self.run_client('mt7996-stream', '/dev/null', '--seconds', '1',
                                         '--pulse').returncode, 2)

    def test_missing_device_exits_1(self):
        device = '/nonexistent/w1700k-sdr'
        calls = {'mt7996-probe': (device, '0x000e00f0', '4', '--acquire'),
                 'mt7996-download': (device, '--reset'),
                 'mt7996-stream': (device, '--seconds', '1', '--go', '0x0090f008',
                                   '0x474f474f', '--stop', '0x0090f008', '0x504f5453'),
                 'mt7996-ctl': (device, '--read', '0x0090f400', '1'),
                 'w1700k-led': (device,)}
        for name, args in calls.items():
            with self.subTest(client=name):
                result = self.run_client(name, *args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(device, result.stderr)

    def test_every_client_checks_abi_and_capabilities(self):
        for source in CLIENTS:
            text = (SDR / f'{source}.c').read_text()
            self.assertRegex(text, r'client_open\([^;]*W1700K_SDR_CAP_', source)
            self.assertTrue(text.split('\n', 1)[1].lstrip().startswith(('/*', '#include "client.h"')))


class KernelSourceTests(unittest.TestCase):
    SOURCES = sorted([*SDR.glob('*.[ch]'), *TESTS.glob('*.[ch]'), UAPI / 'w1700k_sdr.h'])

    def test_build_lists_existing_sources(self):
        objects = re.search(r'w1700k_sdr-y := (.*?)\nccflags', (SDR / 'Makefile').read_text(),
                            re.S).group(1).replace('\\', ' ').split()
        for obj in objects:
            self.assertTrue((SDR / obj.replace('.o', '.c')).is_file(), obj)
        clients = re.search(r'SDR_CLIENTS := (.*?)\nmodule:',
                            (ROOT / 'platform/linux/Makefile').read_text(), re.S).group(1)
        pairs = dict(p.split(':') for p in clients.replace('\\', ' ').split())
        self.assertEqual(pairs, CLIENTS)
        self.assertFalse(set(pairs) & {o[:-2] for o in objects})

    def test_spdx_headers(self):
        for path in self.SOURCES:
            first = path.read_text().splitlines()[0]
            self.assertRegex(first, r'SPDX-License-Identifier: ', path.name)

    def test_no_provenance_markers(self):
        forbidden = re.compile(r'\bD\d{3}\b|research/|vendor-stack|unit[ -]A\b|'
                               r'\b20\d\d-\d\d-\d\d\b|"claim"|W1700K2[45]|/home/')
        for path in self.SOURCES:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(forbidden.search(line), f'{path.name}:{number}: {line}')


if __name__ == '__main__':
    unittest.main()
