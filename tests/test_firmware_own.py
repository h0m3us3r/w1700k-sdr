# SPDX-License-Identifier: MIT
"""Our own MT7996 RAM containers: the sealing (read back with container.read_ram_container) and,
where the RISC-V toolchain is installed, the WM and WA images and our ROM patch themselves."""
import shutil
import struct
import sys
import unittest
import zlib
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parents[1] / 'firmware'
sys.path.insert(0, str(FIRMWARE))

import container  # noqa: E402
import own  # noqa: E402
from rom_patch import patch  # noqa: E402
from stream import blob  # noqa: E402

TOOLS = all(shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy'))
CSRRCI_MSTATUS_MIE = 0x300472F3          # csrrci t0, mstatus, 8: the WM's first instruction


def payloads(image):
    """[(address, feature byte, plaintext)] of a RAM container."""
    return [(address, feature, payload) for address, payload, feature in container.read_ram_container(image)]


class Sealing(unittest.TestCase):
    def test_round_trip(self):
        code = bytes(range(37))
        image = container.ram_container([(0x00916400, code, 0x21), (0x00407200, b'\x55' * 32, 0x01),
                                         (0x00404000, b'plain', 0x00)], '0123456789', '20260101000000')
        self.assertEqual(zlib.crc32(image[:-4]), struct.unpack_from('<I', image, len(image) - 4)[0])
        self.assertEqual(image[-36:-31], bytes((container.CHIP_ID, 0, 3, 2, 1)))
        self.assertEqual(image[-29:-19], b'0123456789')
        regions = payloads(image)
        self.assertEqual([(a, f) for a, f, _ in regions], [(0x00916400, 0x21), (0x00407200, 0x01), (0x00404000, 0)])
        self.assertEqual(regions[0][2], code + bytes(11))
        self.assertEqual(regions[1][2], b'\x55' * 32)
        self.assertEqual(regions[2][2], b'plain' + bytes(11))
        self.assertNotIn(code, image, 'the entry region is sealed encrypted')
        envelope = image[sum(16 + len(p) for _, _, p in regions):][:36]
        self.assertEqual(envelope, container.release_envelope())

    def test_refusals(self):
        with self.assertRaises(ValueError):
            container.ram_container([(0, b'a', 0x21), (16, b'b', 0x21)], '0123456789', '1')
        with self.assertRaises(ValueError):
            container.ram_container([(0, b'a', 0x21)], 'short', '1')
        with self.assertRaises(ValueError):
            container.ram_container([], '0123456789', '1')

    def test_reader_refusals(self):
        image = container.ram_container([(0x00916400, b'x' * 40, 0x21)], '0123456789', '1')
        bad = bytearray(image)
        bad[0] ^= 1                                          # the CRC no longer holds
        with self.assertRaises(ValueError):
            container.read_ram_container(bytes(bad))
        length = len(image) - 36 - 40 + 0x14
        for delta in (1, 16):                               # not AES blocks; past the envelope
            bad = bytearray(image)
            struct.pack_into('<I', bad, length, struct.unpack_from('<I', image, length)[0] + delta)
            struct.pack_into('<I', bad, len(bad) - 4, zlib.crc32(bad[:-4]))
            with self.assertRaises(ValueError):
                container.read_ram_container(bytes(bad))
        with self.assertRaises(ValueError):
            container.read_ram_container(image[:60])


@unittest.skipUnless(TOOLS, 'clang, ld.lld and llvm-objcopy are needed to build the images')
class Images(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.images = {role: (name, data, flags) for role, name, data, flags in own.build()}

    def test_wm(self):
        """The entry region in ULM1, the stream producer above it (one image, every packing
        schedule), then the radio code in CRAM above the ROM's download buffers."""
        name, data, flags = self.images['wm']
        self.assertEqual((name, flags), ('w1700k_wm.bin', ()))
        (entry, feature, plain), (producer, producer_feature, code), (cram, cram_feature, cram_plain) = payloads(data)
        self.assertEqual((entry, feature), (own.WM_ENTRY, 0x21))
        self.assertEqual(struct.unpack_from('<I', plain)[0], CSRRCI_MSTATUS_MIE)
        self.assertLessEqual(entry + len(plain), own.PRODUCER_BASE)
        self.assertEqual((producer, producer_feature), (own.PRODUCER_BASE, 0x01))
        self.assertLessEqual(len(code), own.PRODUCER_SIZE)
        self.assertLessEqual(own.PRODUCER_BASE + own.PRODUCER_SIZE, own.WM_LIMIT)
        self.assertEqual((cram, cram_feature), (own.WM_CRAM, 0x01))
        self.assertLessEqual(cram + len(cram_plain), own.WM_CRAM_LIMIT)

    def test_the_set_in_download_order(self):
        self.assertEqual([(role, name, flags) for role, (name, _, flags) in self.images.items()],
                         [('wm', 'w1700k_wm.bin', ()), ('dsp', 'w1700k_dsp.bin', ('--dsp',)),
                          ('wa', 'w1700k_wa.bin', ('--wa',))])

    def test_wa(self):
        """The entry region, then the stream packer where the producer raises the WA into it."""
        name, data, flags = self.images['wa']
        self.assertEqual((name, flags), ('w1700k_wa.bin', ('--wa',)))
        (entry, feature, plain), (packer, packer_feature, code) = payloads(data)
        self.assertEqual((entry, feature), (own.WA_ENTRY, 0x21))
        self.assertEqual(struct.unpack_from('<I', plain)[0], 0x30047073)     # csrci mstatus, 8
        self.assertLessEqual(entry + len(plain), blob.WA_CODE)
        self.assertEqual((packer, packer_feature), (blob.WA_CODE, 0x01))
        self.assertLessEqual(packer + len(code), blob.WA_LIMIT)


@unittest.skipUnless(TOOLS, 'clang and llvm-objcopy are needed to assemble the hooks')
class OwnPatch(unittest.TestCase):
    def test_only_our_hooks(self):
        image = patch.build_own()
        offset, plain = patch.read_container(image, 'own patch')
        bitmap_lo, bitmap_hi = struct.unpack_from('<II', plain, 0)
        n = len(patch.OWN)
        self.assertEqual((bitmap_lo, bitmap_hi), ((1 << n) - 1, 0))
        frm = struct.unpack_from(f'<{n}I', plain, patch.FROM_OFF)
        self.assertEqual(frm, tuple(h.from_addr for h in patch.OWN))
        for table in (patch.FROM_OFF, patch.TO_OFF):              # the other entries unused
            self.assertEqual(plain[table + 4 * n:table + 4 * patch.N_ENTRY], bytes(4 * (patch.N_ENTRY - n)))
        self.assertLess(len(plain), 0x800, 'no vendor body')
        self.assertEqual(plain[-patch.FOOTER_LEN + 8:], bytes(patch.FOOTER_LEN - 8), 'a blank footer')
        self.assertEqual(image[16:20], b'ALPS')


if __name__ == '__main__':
    unittest.main()
