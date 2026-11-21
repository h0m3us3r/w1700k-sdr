# SPDX-License-Identifier: MIT
"""The controller's one word to the host's stream service (host/w1700k/service.py)."""
import os
import struct
import tempfile
import unittest
from pathlib import Path

import stream_shm
from w1700k import service

HEADER = Path(__file__).resolve().parents[1] / 'host' / 'stream' / 'include' / 'w1700k_stream.h'


class Announce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'w1700k-test'

    def make(self, magic=service.MAGIC, version=service.VERSION, size=8192, mode=0o600):
        self.path.write_bytes(struct.pack('<2I', magic, version) + bytes(size - 8))
        os.chmod(self.path, mode)

    def word(self):
        return struct.unpack_from('<Q', self.path.read_bytes(), service.RADIO_CHANNELS_AT)[0]

    def test_the_word_is_where_the_header_has_it(self):
        text = HEADER.read_text()
        self.assertIn('#define W1700K_STREAM_VERSION\t\t%du' % service.VERSION, text)
        self.assertEqual(service.MAGIC, 0x4D525453)
        self.assertEqual(service.RADIO_CHANNELS_AT, stream_shm.FIELDS['radio_channels'])
        self.assertEqual(stream_shm.VERSION, service.VERSION)

    def test_a_service_of_this_user_is_told(self):
        self.make()
        self.assertTrue(service.announce_channels(2, '/w1700k-test', self.tmp.name))
        self.assertEqual(self.word(), 2)
        self.assertTrue(service.announce_channels(1, '/w1700k-test', self.tmp.name))
        self.assertEqual(self.word(), 1)

    def test_nothing_is_written_where_there_is_no_such_service(self):
        self.assertFalse(service.announce_channels(2, '/w1700k-test', self.tmp.name), 'no object')
        for what, kwargs in (('an older layout', dict(version=1)), ('another object', dict(magic=0x12345678)),
                             ('one others may write', dict(mode=0o622)), ('one too short', dict(size=64))):
            with self.subTest(what):
                self.make(**kwargs)
                before = self.path.read_bytes()
                self.assertFalse(service.announce_channels(2, '/w1700k-test', self.tmp.name))
                self.assertEqual(self.path.read_bytes(), before)

    def test_only_one_or_two_channels(self):
        self.make()
        with self.assertRaises(ValueError):
            service.announce_channels(3, '/w1700k-test', self.tmp.name)


if __name__ == '__main__':
    unittest.main()
