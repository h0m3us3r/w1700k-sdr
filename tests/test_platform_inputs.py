# SPDX-License-Identifier: MIT
"""What the platform build is made from is what is pinned (platform/sources.json): a source
checkout kept between builds, and the host tools built from a pinned source."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform'))
import buildlib     # noqa: E402
import fetch        # noqa: E402
import uboot        # noqa: E402

GIT = shutil.which('git')


@unittest.skipUnless(GIT, 'no git')
class KeptCheckout(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.origin = self.dir / 'origin'
        self.origin.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'uploadpack.allowFilter', 'true')
        (self.origin / 'Makefile').write_text('all:\n\ttrue\n')
        (self.origin / 'package').mkdir()
        (self.origin / 'package/one.mk').write_text('ONE = 1\n')
        self.git('add', '.')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'pinned')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.kept = self.dir / 'src/origin'

    def git(self, *args):
        return subprocess.run([GIT, '-C', str(self.origin), *args], check=True, capture_output=True, text=True).stdout

    def checkout(self):
        with mock.patch('builtins.print'):
            return fetch.git_checkout(self.kept, str(self.origin), self.commit)

    def test_a_checkout_at_the_pinned_commit_is_used_again(self):
        self.assertEqual(self.checkout(), self.kept)
        (self.kept / 'marker').write_text('a file of the build, not of the source\n')
        self.assertEqual(self.checkout(), self.kept)
        self.assertTrue((self.kept / 'marker').exists(), 'not fetched again')

    def test_one_whose_files_differ_from_the_pinned_commit_is_refused(self):
        self.checkout()
        (self.kept / 'package/one.mk').write_text('ONE = 2\n')
        with self.assertRaisesRegex(buildlib.BuildError, r'1 of its files differ from it \(package/one\.mk\).*remove the directory'):
            self.checkout()
        (self.kept / 'package/one.mk').write_text('ONE = 1\n')
        self.assertEqual(self.checkout(), self.kept)
        (self.kept / 'Makefile').unlink()
        with self.assertRaisesRegex(buildlib.BuildError, 'Makefile'):
            self.checkout()

    def test_one_at_another_commit_is_fetched_again(self):
        self.checkout()
        (self.origin / 'Makefile').write_text('all:\n\tfalse\n')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-am', 'later')
        pinned, self.commit = self.commit, self.git('rev-parse', 'HEAD').strip()
        self.assertNotEqual(pinned, self.commit)
        self.checkout()
        self.assertIn('false', (self.kept / 'Makefile').read_text())


class Mkimage(unittest.TestCase):
    """mkimage is built from the pinned U-Boot: once for a pin, and again when the pin changes."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.tool = self.dir / 'mkimage/tools/mkimage'
        self.builds = 0
        self.pin = 'a' * 64
        patches = [mock.patch.object(uboot, 'MKIMAGE', self.tool), mock.patch.object(uboot, 'run', self.made),
                   mock.patch.object(uboot, 'LOGS', self.dir), mock.patch.object(uboot, 'build_env', lambda *a: {}),
                   mock.patch.object(uboot, 'sources', lambda: {'u-boot': {'sha256': self.pin}})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def made(self, command, **options):
        if command[-1] == 'tools-only':
            self.builds += 1
            self.tool.parent.mkdir(parents=True, exist_ok=True)
            self.tool.write_text(f'built from {self.pin}\n')

    def test_built_once_for_a_pin_and_again_for_another(self):
        self.assertEqual(uboot.mkimage(self.dir / 'src'), self.tool)
        uboot.mkimage(self.dir / 'src')
        self.assertEqual(self.builds, 1)
        (self.tool.parents[1] / 'left-over.o').write_text('of the first build\n')
        self.pin = 'b' * 64
        uboot.mkimage(self.dir / 'src')
        self.assertEqual(self.builds, 2)
        self.assertEqual(self.tool.read_text(), f'built from {"b" * 64}\n')
        self.assertFalse((self.tool.parents[1] / 'left-over.o').exists(), 'from nothing of the earlier one')

    def test_one_built_before_pins_were_noted_is_built_again(self):
        self.tool.parent.mkdir(parents=True)
        self.tool.write_text('of some U-Boot\n')
        uboot.mkimage(self.dir / 'src')
        self.assertEqual(self.builds, 1)

    def test_a_build_that_did_not_finish_is_not_taken_for_one(self):
        uboot.mkimage(self.dir / 'src')
        (self.tool.parents[1] / 'u-boot.pin').unlink()
        uboot.mkimage(self.dir / 'src')
        self.assertEqual(self.builds, 2)


if __name__ == '__main__':
    unittest.main()
