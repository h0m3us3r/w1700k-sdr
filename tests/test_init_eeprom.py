# SPDX-License-Identifier: MIT
"""The radio calibration is taken from the NAND backup, and the host tools find it."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'init'))
sys.path.insert(0, str(ROOT / 'host'))
from initkit import backup                              # noqa: E402
from initkit.console import StepFailed                   # noqa: E402
from w1700k import config as host_config                 # noqa: E402

IMAGE = b'\x90\x79' + bytes(range(256)) * 29 + b'\xff' * (0x4000 - 2 - 256 * 29)


def fake_backup(path, image=IMAGE):
    """A sparse backup file with the calibration in place."""
    with open(path, 'wb') as stream:
        stream.seek(backup.EEPROM_OFFSET)
        stream.write(image)
        stream.truncate(backup.DUMP_BYTES)


class ExtractTest(unittest.TestCase):
    def test_extracts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / backup.BACKUP_NAME
            fake_backup(source)
            out = backup.extract_eeprom(source, tmp)
            self.assertEqual(out.read_bytes(), IMAGE)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.extract_eeprom(source, tmp), out)

    def test_refuses_a_backup_without_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / backup.BACKUP_NAME
            fake_backup(source, b'\xff' * 0x4000)
            with self.assertRaises(StepFailed):
                backup.extract_eeprom(source, tmp)


class HostLookupTest(unittest.TestCase):
    def lookup(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            for name in files:
                path = Path(tmp) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'x')
            data = Path(tmp) / 'data/w1700k'
            user = Path(tmp) / 'config/w1700k/eeprom.bin'
            cfg = host_config.parse({})
            with mock.patch.object(host_config, 'DATA_DIR', data), \
                    mock.patch.object(host_config, 'USER_EEPROM', user):
                found = host_config.eeprom_path(cfg) or host_config.saved_eeprom()
            return os.path.relpath(found, tmp)

    def test_explicit_setting_wins(self):
        cfg = host_config.parse({'eeprom': '/somewhere/e.bin'})
        self.assertEqual(host_config.eeprom_path(cfg), Path('/somewhere/e.bin'))

    def test_user_copy_then_single_unit(self):
        self.assertEqual(self.lookup(['config/w1700k/eeprom.bin', 'data/w1700k/units/A/eeprom.bin']),
                         'config/w1700k/eeprom.bin')
        self.assertEqual(self.lookup(['data/w1700k/units/A/eeprom.bin']), 'data/w1700k/units/A/eeprom.bin')

    def test_without_a_named_file_the_unit_is_asked_first(self):
        with mock.patch.object(host_config, 'USER_EEPROM', Path('/nowhere/eeprom.bin')):
            self.assertIsNone(host_config.eeprom_path(host_config.parse({})))

    def test_where_the_unit_keeps_it_is_where_the_backup_has_it(self):
        from w1700k import radio
        sys.path.insert(0, str(ROOT / 'platform'))
        import partitions
        self.assertEqual(partitions.GEOMETRY['calibration'][0] + radio.UNIT_EEPROM_OFFSET, backup.EEPROM_OFFSET)
        self.assertEqual(radio.EEPROM_FILE_BYTES, backup.EEPROM_BYTES)
        self.assertEqual(radio.EEPROM_CHIP_ID, backup.EEPROM_CHIP_ID)
        self.assertIn('"calibration"', radio.UNIT_EEPROM)
        self.assertIn('skip=5 count=4', radio.UNIT_EEPROM)

    def test_ambiguous_or_missing_is_an_error(self):
        with self.assertRaises(host_config.ConfigError):
            self.lookup(['data/w1700k/units/A/eeprom.bin', 'data/w1700k/units/B/eeprom.bin'])
        with self.assertRaises(host_config.ConfigError):
            self.lookup([])


if __name__ == '__main__':
    unittest.main()
