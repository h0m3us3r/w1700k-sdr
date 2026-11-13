# SPDX-License-Identifier: MIT
"""The ssh transport (unit.py): it must never hand the caller's stdin to ssh, and batched client
runs travel as one script and one tar.

ssh makes an inherited stdin non-blocking; in `w1700k serve` that stdin is the request pipe,
and the next empty read then ends the session as if the web server had closed it.
"""
import io
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from w1700k import config, unit  # noqa: E402
from w1700k.unit import Step, StepResult  # noqa: E402


class StdinTest(unittest.TestCase):
    def setUp(self):
        self.unit = unit.Unit(config.parse({}).unit)

    def run_with(self, **kwargs):
        done = subprocess.CompletedProcess([], 0, b'', b'')
        with mock.patch.object(unit.subprocess, 'run', return_value=done) as run:
            self.unit.run('true', **kwargs)
        return run.call_args.kwargs

    def test_no_input_reads_dev_null(self):
        kwargs = self.run_with()
        self.assertIs(kwargs.get('stdin'), subprocess.DEVNULL)
        self.assertNotIn('input', kwargs)

    def test_input_is_piped(self):
        kwargs = self.run_with(input=b'script')
        self.assertEqual(kwargs.get('input'), b'script')
        self.assertNotIn('stdin', kwargs)


class Batch(unittest.TestCase):
    def test_script_and_archive(self):
        steps = [Step(('/usr/sbin/mt7996-probe', '--out', unit.OUT, '--note', 'ab cd')), Step(('true',))]
        script = unit.batch_script(steps, '/tmp/b-$$')
        self.assertIn('"$d"/000.bin', script)
        self.assertIn("'ab cd'", script)
        self.assertIn('tar cf - .', script)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            for name, data in (('./000.rc', b'0\n'), ('./000.out', b'{}'), ('./000.bin', b'\x01\x02')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        results = unit.parse_batch(buf.getvalue(), 2)
        self.assertEqual(results[0], StepResult(0, b'{}', b'', b'\x01\x02'))
        self.assertIsNone(results[1])
        with self.assertRaises(unit.UnitError):
            unit.parse_batch(b'not a tar', 1)

    def test_batch_runs_locally(self):
        """The generated script under a real sh, with stand-in clients."""
        steps = [Step(('sh', '-c', 'echo hi; printf data > "$0"', unit.OUT)), Step(('false',)), Step(('true',))]
        script = unit.batch_script(steps, '/tmp/w1700k-test-$$')
        out = subprocess.run(['sh', '-s'], input=script.encode(), capture_output=True, check=True).stdout
        first, second, third = unit.parse_batch(out, 3)
        self.assertEqual((first.exit, first.stdout, first.data), (0, b'hi\n', b'data'))
        self.assertNotEqual(second.exit, 0)
        self.assertIsNone(third)


class Configuration(unittest.TestCase):
    def test_link_mbps(self):
        """export.link_mbps: the link's capacity where it is less than the port's speed."""
        self.assertIsNone(config.parse({}).export.link_mbps)
        self.assertEqual(config.parse({'export': {'link_mbps': 7000}}).export.link_mbps, 7000)
        for bad in (0, -1, 2.5, '5000', True):
            with self.assertRaises(config.ConfigError):
                config.parse({'export': {'link_mbps': bad}})

    def test_packing(self):
        """export.packing: set by hand; absent or null: automatic."""
        self.assertIsNone(config.parse({}).export.packing)
        self.assertIsNone(config.parse({'export': {'packing': None}}).export.packing)
        for name in config.PACKINGS:
            self.assertEqual(config.parse({'export': {'packing': name}}).export.packing, name)
        for bad in ('auto', 'FULL', 28, True):
            with self.assertRaises(config.ConfigError):
                config.parse({'export': {'packing': bad}})


if __name__ == '__main__':
    unittest.main()
