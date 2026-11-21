# SPDX-License-Identifier: MIT
"""What the platform build is made from is what is pinned (platform/sources.json): an archive
from whichever of its addresses gives the pinned file, a source checkout kept between builds, and
the host tools built from a pinned source."""
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


class Download(unittest.TestCase):
    """An archive comes from the first of its addresses that gives the pinned file."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        patch = mock.patch.object(fetch, 'DOWNLOADS', self.dir / 'downloads')
        patch.start()
        self.addCleanup(patch.stop)
        self.good, self.other = b'the release' * 100, b'something else under its name'
        self.sha256 = buildlib.sha256_file(self.put('site/u-boot.tar.bz2', self.good))
        self.cached = self.dir / 'downloads/u-boot.tar.bz2'

    def put(self, name, data):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def url(self, name):
        return (self.dir / name).as_uri()

    def fetch(self, *urls):
        with mock.patch('builtins.print'):
            return fetch.download(urls[0], self.sha256, mirrors=urls[1:])

    def test_the_first_address_when_it_answers(self):
        self.put('mirror/u-boot.tar.bz2', self.other)       # never asked
        self.assertEqual(self.fetch(self.url('site/u-boot.tar.bz2'), self.url('mirror/u-boot.tar.bz2')), self.cached)
        self.assertEqual(self.cached.read_bytes(), self.good)

    def test_a_mirror_when_the_first_is_gone(self):
        """As when ftp.denx.de stopped answering (issue 37)."""
        self.assertEqual(self.fetch(self.url('gone/u-boot.tar.bz2'), self.url('site/u-boot.tar.bz2')), self.cached)
        self.assertEqual(self.cached.read_bytes(), self.good)

    def test_an_address_with_another_file_under_the_name_is_passed_over(self):
        self.put('mirror/u-boot.tar.bz2', self.other)
        self.fetch(self.url('mirror/u-boot.tar.bz2'), self.url('site/u-boot.tar.bz2'))
        self.assertEqual(self.cached.read_bytes(), self.good)

    def test_when_none_gives_it_each_is_named_and_where_a_copy_may_be_put(self):
        self.put('mirror/u-boot.tar.bz2', self.other)
        with self.assertRaises(buildlib.BuildError) as raised:
            self.fetch(self.url('gone/u-boot.tar.bz2'), self.url('mirror/u-boot.tar.bz2'))
        text = str(raised.exception)
        self.assertIn('gone/u-boot.tar.bz2: ', text)
        self.assertIn('mirror/u-boot.tar.bz2: its SHA-256 ', text)
        self.assertIn(f'put it at {self.cached}', text)
        self.assertIn(self.sha256, text)
        self.assertFalse(list((self.dir / 'downloads').iterdir()), 'nothing half-fetched is left')

    def test_a_copy_put_there_by_hand_is_taken_if_it_is_the_pinned_file(self):
        self.put('downloads/u-boot.tar.bz2', self.good)
        self.assertEqual(self.fetch(self.url('gone/u-boot.tar.bz2')), self.cached)
        self.cached.write_bytes(self.other)
        with self.assertRaisesRegex(buildlib.BuildError, 'is not the pinned'):
            self.fetch(self.url('gone/u-boot.tar.bz2'))
        self.assertFalse(self.cached.exists(), 'and removed if it is not')

    def test_every_archive_in_the_pins_names_https_addresses(self):
        pins = buildlib.sources()
        for name in ('linux', 'u-boot'):
            for address in (pins[name]['url'], *pins[name].get('mirrors', ())):
                self.assertTrue(address.startswith('https://') and address.endswith(pins[name]['url'].rsplit('/', 1)[1]),
                                f'{name}: {address}')


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
