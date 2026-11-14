# SPDX-License-Identifier: MIT
"""platform/partitions.py: the NAND layout every stage and the installer share."""
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform'))
import partitions  # noqa: E402


class Layout(unittest.TestCase):
    def test_the_table_covers_the_chip_contiguously(self):
        partitions.check_table()
        self.assertEqual(partitions.GEOMETRY['loader'], (0x600000, 0x200000))
        self.assertEqual(partitions.GEOMETRY['system'], (0x800000, 0x2200000))
        self.assertEqual(partitions.GEOMETRY['durable'], (0x2a00000, 0x1c00000))
        # Ours ends where stock's first firmware slot does: its second slot and its UBI are
        # one partition nobody writes.
        self.assertEqual(sum(partitions.GEOMETRY['durable']), 0x4600000)
        self.assertEqual(partitions.GEOMETRY['stock'], (0x4600000, 0x17800000))
        for stage in ('loader', 'linux'):
            self.assertIn('stock', partitions.READ_ONLY[stage])

    def test_the_units_own_copy_of_the_durable_window_is_the_tables(self):
        """/etc/w1700k/storage.conf in the root: durable-open compares the partition with it."""
        conf = dict(line.split('=', 1) for line in
                    (ROOT / 'platform/rootfs/overlay/etc/w1700k/storage.conf').read_text().splitlines()
                    if '=' in line and not line.startswith('#'))
        self.assertEqual((int(conf['DURABLE_OFFSET'], 16), int(conf['DURABLE_SIZE'], 16)),
                         partitions.GEOMETRY['durable'])

    def test_gaps_overlaps_and_short_tables_are_refused(self):
        for name, value, message in [
                ('system', (0x900000, 0x2f00000), 'gap or overlaps'),
                ('system', (0x800000, 0x3000001), 'block-aligned'),
                ('reserved-bmt', (0x1be00000, 0x4100000), 'covers')]:
            with self.subTest(name=name), \
                    mock.patch.dict(partitions.GEOMETRY, {name: value}), \
                    self.assertRaisesRegex(ValueError, message):
                partitions.check_table()

    def test_vendor_partitions_are_read_only_in_every_stage(self):
        for stage, read_only in partitions.READ_ONLY.items():
            self.assertLessEqual({'bootloader', 'uenv', 'calibration', 'reserved-bmt'},
                                 read_only, stage)
        self.assertNotIn('system', partitions.READ_ONLY['loader'])
        self.assertIn('system', partitions.READ_ONLY['linux'])
        self.assertNotIn('durable', partitions.READ_ONLY['linux'])

    def test_ubi_volumes_fit_with_the_bad_block_reserve(self):
        # 20 per 1024 PEBs of the whole chip, plus 4 for UBI itself.
        kv, lv = partitions.NANDS
        self.assertEqual((partitions.ubi_bad_reserve(kv), partitions.ubi_bad_reserve(lv)),
                         (80, 40))
        self.assertEqual(partitions.ubi_lebs('system', kv), 188)
        self.assertEqual(partitions.ubi_lebs('system', lv), 92)
        self.assertEqual((partitions.durable_lebs(kv), partitions.durable_lebs(lv)), (140, 68))
        for nand in partitions.NANDS:
            volumes = partitions.system_volumes(nand)
            self.assertEqual(sum(lebs for _, _, lebs in volumes),
                             partitions.ubi_lebs('system', nand))
            self.assertEqual([name for name, _, _ in volumes],
                             ['ubootenv', 'ubootenv2', 'fit', 'rootfs'])
            self.assertGreaterEqual(volumes[2][2] * nand.leb, partitions.FIT_VOLUME_BYTES)
            self.assertGreaterEqual(volumes[3][2] * nand.leb, partitions.ROOTFS_VOLUME_BYTES)
        self.assertGreater(partitions.FIT_VOLUME_BYTES, 12 << 20)
        # What the first slot leaves the root once the FIT has its 15.5 MiB: twice the root of
        # today (2.9 MiB), and a staged update of it (the new root and the old) fits `durable`.
        self.assertGreater(partitions.ROOTFS_VOLUME_BYTES, 6 << 20)
        for nand in partitions.NANDS:
            self.assertGreater(partitions.durable_lebs(nand) * nand.leb, 2 * partitions.ROOTFS_VOLUME_BYTES)

    def test_the_parts(self):
        kv, lv = partitions.NANDS
        self.assertEqual((kv.name, kv.id, kv.blocks, kv.leb), ('W25N04KV', b'\xef\xaa\x23',
                                                               4096, 0x1f000))
        self.assertEqual((lv.name, lv.id, lv.blocks, lv.leb), ('W25N04LV', b'\xef\x8b\x23',
                                                               2048, 0x3e000))
        self.assertTrue(kv.tested)
        self.assertFalse(lv.tested)
        self.assertEqual(partitions.nand_by_id(b'\xef\x8b\x23'), lv)
        self.assertIsNone(partitions.nand_by_id(b'\xef\xab\x23'))
        self.assertEqual(partitions.nand_by_geometry(0x800, 0x20000, 128), kv)
        # Every boundary lands on a 256 KiB block, and the layout ends inside
        # the vendor's usable area of both parts at their worst.
        self.assertEqual(partitions.ALIGNMENT, 0x40000)
        self.assertEqual((partitions.least_usable(kv), partitions.least_usable(lv)), (3806, 1845))

    def test_the_bmt_write_policy(self):
        header = partitions.policy_header()
        first = partitions.GEOMETRY['system'][0]
        end = sum(partitions.GEOMETRY['durable'])
        self.assertEqual(end, 0x4600000, 'the loader and Linux write nothing past what is ours')
        self.assertIn(f'#define BMT_WRITE_FIRST_BYTES {first:#x}ull', header)
        self.assertIn(f'#define BMT_WRITE_END_BYTES {end:#x}ull', header)
        self.assertIn('#define BMT_MIN_BLOCK_BYTES 0x20000u', header)
        # The journal is the block after the BBT: 3847 on the W25N04KV.
        self.assertIn('#define BMT_JOURNAL_AFTER_BBT 1u', header)
        restorer = partitions.policy_header('uenv', 'stock')
        self.assertIn(f'#define BMT_WRITE_FIRST_BYTES {0x200000:#x}ull', restorer)
        self.assertIn(f'#define BMT_WRITE_END_BYTES {0x1be00000:#x}ull', restorer, 'the restorer: stock too')


class DeviceTrees(unittest.TestCase):
    def test_both_device_trees_carry_the_current_region(self):
        for path, stage in partitions.DEVICE_TREES.items():
            with self.subTest(path=path):
                partitions.check(path, stage)
                text = (ROOT / path).read_text()
                for name in partitions.GEOMETRY:
                    self.assertEqual(text.count(f'label = "{name}";'), 1, name)

    def test_a_stale_region_is_reported(self):
        path, stage = next(iter(partitions.DEVICE_TREES.items()))
        current = '0x00800000 0x02200000'
        self.assertIn(current, (ROOT / path).read_text())
        text = (ROOT / path).read_text().replace(current, '0x00700000 0x02300000', 1)
        with mock.patch.object(Path, 'read_text', return_value=text), \
                self.assertRaisesRegex(ValueError, 'stale partition table'):
            partitions.check(path, stage)

    def test_read_only_flags_follow_the_stage(self):
        loader = partitions.fragment('loader')
        linux = partitions.fragment('linux')
        system_loader = loader[loader.index('system@'):loader.index('durable@')]
        system_linux = linux[linux.index('system@'):linux.index('durable@')]
        self.assertNotIn('read-only', system_loader)
        self.assertIn('read-only', system_linux)


if __name__ == '__main__':
    unittest.main()
