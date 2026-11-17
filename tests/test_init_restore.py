# SPDX-License-Identifier: MIT
"""init/restore, run end to end against the simulated unit."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_init_install import Bench  # noqa: E402
import init_fake_unit as fake  # noqa: E402
from initkit import backup, config, restore  # noqa: E402
from initkit.config import partitions  # noqa: E402
from initkit.console import StepFailed  # noqa: E402

KV, LV = partitions.NANDS
SIZE = 0x800000
UENV = b'bootcmd=' + fake.STOCK_BOOTCMD.encode() + b'\nbootdelay=3\n'
OURS = b'bootcmd=flash read 0x600000 0x40000 0x89000000; bootm 0x89000000\n'


def stock_backup(identity=fake.IDENTITY):
    data = bytearray(b'\xff' * SIZE)
    data[0x200000:0x200000 + len(UENV)] = UENV
    data[0x400000:0x400000 + len(identity)] = identity
    data[0x405000:0x405000 + len(fake.CALIBRATION)] = fake.CALIBRATION
    data[0x600000:] = bytes((index * 13 + 5) & 0xff for index in range(SIZE - 0x600000))
    return bytes(data)


class Restore(Bench):
    def setUp(self):
        super().setUp()
        restorer = self.work / 'images' / 'restorer'
        restorer.write_bytes(fake.fit('restorer', 0x2d000))
        for patcher in (mock.patch.object(restore, 'RESTORER', restorer),
                        mock.patch.object(backup, 'DUMP_BYTES', SIZE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.backup = self.work / 'nand.bin'
        self.backup.write_bytes(stock_backup())

    def installed_unit(self, part=KV):
        """A unit running our system: stock's data except our bootcmd in `uenv`
        and our images over 1 MiB at 0x600000."""
        unit = self.make_unit(chip_bytes=SIZE, installed=True, part=part)
        unit.nand[:] = self.backup.read_bytes()
        unit.nand[0x200000:0x200000 + len(OURS)] = OURS
        unit.nand[0x600000:0x700000] = b'\x5a' * 0x100000
        unit.state = 'linux'           # the session opens armed, as the real one does
        return unit

    def restore(self, backup_path='given', **options):
        with mock.patch.object(restore, 'open_session', self.open_session):
            return restore.run(self.settings, self.backup if backup_path == 'given'
                               else backup_path, **options)

    def test_the_backup_is_chosen_by_the_serial_the_unit_reports(self):
        unit = self.installed_unit()
        directory = config.unit_dir(self.settings.data_dir, 'W1700KTEST0001')
        directory.mkdir(parents=True)
        (directory / backup.BACKUP_NAME).write_bytes(self.backup.read_bytes())
        self.backup.unlink()
        self.assertEqual(self.restore(backup_path=None), 0)
        self.assertEqual(bytes(unit.nand), (directory / backup.BACKUP_NAME).read_bytes())

    def test_a_unit_without_a_backup_is_not_written(self):
        unit = self.installed_unit()
        with self.assertRaisesRegex(StepFailed, 'no backup of W1700KTEST0001'):
            self.restore(backup_path=None)
        self.assertEqual(unit.nand_writes, [])

    def test_force_skips_the_serial_check(self):
        unit = self.installed_unit()
        self.assertEqual(self.restore(serial='W1700KTEST0009', force=True), 0)
        self.assertEqual(bytes(unit.nand), self.backup.read_bytes())

    def test_force_restores_another_units_backup_but_never_its_kept_partitions(self):
        unit = self.installed_unit()
        own = bytes(unit.nand[0x400000:0x600000])
        self.backup.write_bytes(stock_backup(fake.IDENTITY.replace(b'TEST0001', b'TEST0002')))
        self.assertEqual(self.restore(force=True), 0)       # refused without: see above
        self.assertEqual(bytes(unit.nand[0x400000:0x600000]), own)
        wanted = self.backup.read_bytes()
        self.assertEqual(bytes(unit.nand[0x600000:]), wanted[0x600000:])
        self.assertEqual(bytes(unit.nand[0x200000:0x400000]), wanted[0x200000:0x400000])

    def test_force_continues_past_a_damaged_calibration(self):
        unit = self.installed_unit()
        unit.nand[0x405100] ^= 1
        self.assertEqual(self.restore(force=True), 0)       # refused without: see above
        self.assertNotEqual(unit.nand[0x405100], self.backup.read_bytes()[0x405100])

    def test_a_unit_other_than_the_named_one_is_not_written(self):
        unit = self.installed_unit()
        with self.assertRaisesRegex(StepFailed, 'not W1700KTEST0009'):
            self.restore(serial='W1700KTEST0009')
        self.assertEqual(unit.nand_writes, [])

    def test_a_unit_is_restored_from_its_backup(self):
        unit = self.installed_unit()
        self.assertEqual(self.restore(), 0)
        self.assertEqual(bytes(unit.nand), self.backup.read_bytes())
        self.assertEqual(unit.saved['bootcmd'], fake.STOCK_BOOTCMD)
        self.assertEqual(unit.state, 'stock-linux')

    def test_stocks_second_slot_alone_is_written_back_and_ours_stays(self):
        """--stock-slot: what an old layout's `durable` overwrote in stock's second slot comes
        back from the backup; the loader, our system and our boot command are not touched."""
        unit = self.installed_unit()
        span = (0x700000, SIZE - 0x700000)                     # `stock`, on this small chip
        damaged = span[0] + 2 * KV.block
        unit.nand[damaged:damaged + 3 * KV.block] = b'\xa5' * (3 * KV.block)
        with mock.patch.dict(partitions.GEOMETRY, {'stock': span}):
            self.assertEqual(restore.stock_span(SIZE), span)
            self.assertEqual(self.restore(stock_only=True, boot=False), 0)
            self.assertEqual(unit.nand_writes, [(damaged + k * KV.block, KV.block) for k in range(3)])
            self.assertEqual(bytes(unit.nand[span[0]:]), self.backup.read_bytes()[span[0]:])
            self.assertEqual(bytes(unit.nand[0x600000:0x700000]), b'\x5a' * 0x100000, 'our images')
            self.assertEqual(bytes(unit.nand[0x200000:0x200000 + len(OURS)]), OURS, 'our boot command')

    def test_an_intact_second_slot_is_compared_and_not_written(self):
        unit = self.installed_unit()
        with mock.patch.dict(partitions.GEOMETRY, {'stock': (0x700000, SIZE - 0x700000)}):
            self.assertEqual(self.restore(stock_only=True, boot=False), 0)
        self.assertEqual(unit.nand_writes, [])

    def test_only_the_blocks_that_differ_are_rewritten(self):
        unit = self.installed_unit()
        self.restore()
        self.assertEqual(unit.nand_writes,
                         [(0x600000 + block * KV.block, KV.block) for block in range(8)] +
                         [(0x200000, KV.block)])
        sent = [line.split()[2] for line in self.commands('restorer')
                if line.startswith('tftpboot')]
        self.assertEqual(sent, ['backup.gz'])
        self.assertFalse(any(line.startswith(('mtd erase', 'mtd write', 'tftpput'))
                             for line in self.commands('restorer')))

    def test_an_untested_nand_is_not_written_without_the_flag(self):
        unit = self.installed_unit(part=LV)
        with self.assertRaisesRegex(StepFailed, 'UNTESTED NAND IC DETECTED!'):
            self.restore()
        self.assertEqual(unit.nand_writes, [])
        self.assertFalse(any(line.startswith(('tftpboot', 'nandsync'))
                             for line in self.commands('restorer')))

    def test_a_pool_stock_does_not_share_writes_nothing(self):
        unit = self.installed_unit(part=LV)
        unit.stock_pool = 170
        with self.assertRaisesRegex(StepFailed, 'pool of 170 blocks and ours computes 163'):
            self.restore(accept_untested=True)
        self.assertEqual(unit.nand_writes, [])

    def test_a_w25n04lv_is_compared_and_rewritten_in_its_256_kib_blocks(self):
        unit = self.installed_unit(part=LV)
        self.assertEqual(self.restore(accept_untested=True), 0)
        self.assertEqual(bytes(unit.nand), self.backup.read_bytes())
        self.assertEqual(unit.nand_writes,
                         [(0x600000 + block * LV.block, LV.block) for block in range(4)] +
                         [(0x200000, LV.block)])

    def test_a_unit_that_already_holds_the_backup_is_not_written(self):
        unit = self.installed_unit()
        unit.nand[:] = self.backup.read_bytes()
        unit.saved['bootcmd'] = OURS.decode().split('=', 1)[1].strip()
        self.restore(boot=False)
        self.assertEqual(unit.nand_writes, [])

    def test_stock_u_boot_only_stages_and_boots_the_restorer(self):
        self.installed_unit()
        self.restore()
        stock = self.commands('stock')
        self.assertTrue(any(line.startswith('bootm ') for line in stock))
        self.assertFalse([line for line in stock
                          if line.startswith(('flash', 'saveenv', 'run', 'setenv bootcmd'))])
        self.assertEqual(self.commands('loader'), [], 'the installed loader is not used')

    def test_a_unit_whose_installed_loader_is_broken_is_restored(self):
        unit = self.installed_unit()
        unit.loader_slot = b'\xff' * len(unit.loader_slot)
        self.assertEqual(self.restore(), 0)
        self.assertEqual(bytes(unit.nand), self.backup.read_bytes())

    def test_vendor_partitions_are_never_written_and_uenv_comes_last(self):
        unit = self.installed_unit()
        self.restore()
        for offset, length in unit.nand_writes:
            for kept, kept_length in restore.KEPT:
                self.assertFalse(offset < kept + kept_length and kept < offset + length,
                                 f'{offset:#x}+{length:#x} overlaps {kept:#x}')
        syncs = [line for line in self.commands('restorer') if line.startswith('nandsync ')]
        self.assertEqual([line.split()[3] for line in syncs], ['0x600000', '0x200000'])
        last_sync = self.index(r'^nandsync spi-nand0 0x90200000 0x200000 ')
        self.assertGreater(self.index(r'^nandsync\.check spi-nand0 0x90600000 0x600000 '),
                           last_sync, 'everything is compared again after the writes')

    def test_another_units_backup_writes_nothing(self):
        unit = self.installed_unit()
        self.backup.write_bytes(stock_backup(fake.IDENTITY.replace(b'TEST0001', b'TEST0002')))
        with self.assertRaisesRegex(StepFailed, 'W1700KTEST0001'):
            self.restore()
        self.assertEqual(unit.nand_writes, [])

    def test_a_calibration_that_differs_from_the_backup_writes_nothing(self):
        unit = self.installed_unit()
        unit.nand[0x405100] ^= 1
        with self.assertRaisesRegex(StepFailed, 'nothing written'):
            self.restore()
        self.assertEqual(unit.nand_writes, [])

    def test_a_unit_that_runs_stock_is_restored_too(self):
        unit = self.installed_unit()
        unit.saved['bootcmd'] = fake.STOCK_BOOTCMD
        self.assertEqual(self.restore(), 0)
        self.assertEqual(bytes(unit.nand), self.backup.read_bytes())


class Plan(unittest.TestCase):
    def test_the_writes_and_kept_partitions_tile_the_real_backup(self):
        self.assertTrue(restore.plan_is_safe(backup.DUMP_BYTES))
        self.assertEqual(restore.writes(backup.DUMP_BYTES)[-1], (0x200000, 0x200000))

    def test_nandsync_writes_only_in_a_writing_session(self):
        from initkit import uart
        self.assertTrue(uart.MUTATING.search('nandsync spi-nand0 0x90000000 0 0x20000'))
        self.assertFalse(uart.MUTATING.search('nandsync.check spi-nand0 0x90000000 0 0x20000'))


if __name__ == '__main__':
    unittest.main()
