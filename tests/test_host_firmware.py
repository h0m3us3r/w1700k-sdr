# SPDX-License-Identifier: MIT
"""firmware.json, schema 3 (firmware/build.py: our ROM patch, WM and WA in download order, and the
ROM they are written for), as the host reads it; where the RISC-V toolchain is installed, the built set itself."""
import hashlib
import importlib.util
import json
import shutil
import sys
import unittest
from pathlib import Path

import host_fake
from host_fake import ROOT
from w1700k import firmware

TOOLS = all(shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy'))


class Manifest(unittest.TestCase):
    def setUp(self):
        self.tmp = host_fake.temp_dir()
        self.dir = Path(self.tmp.name)
        self.manifest = host_fake.make_firmware(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        fw = firmware.load(self.dir)
        self.assertEqual([c.role for c in fw.containers], ['patch', 'wm', 'wa'])
        self.assertEqual([c.path for c in fw.containers],
                         [self.dir / name for name in ('w1700k_rom_patch.bin', 'w1700k_wm.bin', 'w1700k_wa.bin')])
        self.assertEqual([c.flags for c in fw.containers], [(), (), ('--wa',)])
        self.assertEqual(fw.containers[1].data(), b'wm' * 64)
        self.assertEqual((fw.rom.base, fw.rom.size, fw.rom.sha256),
                         (0x00800000, 0x10000, (hashlib.sha256(host_fake.ROM).hexdigest(),)))
        (self.dir / 'w1700k_wm.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(firmware.FirmwareError, 'does not match firmware.json'):
            fw.containers[1].data()

    def test_refusals(self):
        containers = self.manifest['containers']
        rom = self.manifest['rom']
        bad = [dict(self.manifest, schema=1), dict(self.manifest, schema=2), {'containers': containers}, [],
               {k: v for k, v in self.manifest.items() if k != 'rom'},
               dict(self.manifest, rom=dict(rom, sha256=[])), dict(self.manifest, rom=dict(rom, sha256=['00'])),
               dict(self.manifest, rom=dict(rom, size=1000)), dict(self.manifest, rom=dict(rom, base='0x800000')),
               dict(self.manifest, rom=[]),
               dict(self.manifest, containers=containers[::-1]),
               dict(self.manifest, containers=containers[:2]),
               dict(self.manifest, containers=containers + [dict(containers[0], role='dsp')]),
               dict(self.manifest, containers=[dict(containers[0], file='../x.bin')] + containers[1:]),
               dict(self.manifest, containers=[dict(containers[0], size='64')] + containers[1:]),
               dict(self.manifest, containers=[dict(containers[0], sha256='00')] + containers[1:]),
               dict(self.manifest, containers=[{'role': 'patch'}] + containers[1:]),
               dict(self.manifest, containers=None)]
        for manifest in bad:
            with self.assertRaises(firmware.FirmwareError, msg=json.dumps(manifest)[:120]):
                firmware.parse(manifest, self.dir)

    def test_missing_or_unreadable(self):
        with host_fake.temp_dir() as empty:
            with self.assertRaisesRegex(firmware.FirmwareError, 'make -C firmware'):
                firmware.load(empty)
        (self.dir / 'firmware.json').write_text('{')
        with self.assertRaises(firmware.FirmwareError):
            firmware.load(self.dir)


@unittest.skipUnless(TOOLS, 'clang, ld.lld and llvm-objcopy are needed to build the firmware')
class Built(unittest.TestCase):
    def test_the_build_is_what_the_host_reads(self):
        sys.path.insert(0, str(ROOT / 'firmware'))
        spec = importlib.util.spec_from_file_location('firmware_build', ROOT / 'firmware' / 'build.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        self.assertEqual(build.SCHEMA, firmware.SCHEMA)
        with host_fake.temp_dir() as tmp:
            build.build(Path(tmp))
            fw = firmware.load(tmp)
            self.assertEqual([(c.role, c.path.name, c.flags) for c in fw.containers],
                             [('patch', 'w1700k_rom_patch.bin', ()), ('wm', 'w1700k_wm.bin', ()),
                              ('wa', 'w1700k_wa.bin', ('--wa',))])
            self.assertEqual((fw.rom.base, fw.rom.size), (0x00800000, 0x10000), 'the part of the ROM a host may read')
            self.assertEqual(len(fw.rom.sha256), 1)
            for c in fw.containers:
                self.assertEqual(len(c.data()), c.size)


if __name__ == '__main__':
    unittest.main()
