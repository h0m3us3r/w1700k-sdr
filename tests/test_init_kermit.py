# SPDX-License-Identifier: MIT
"""The Kermit sender, against U-Boot's `loadb` receiver as its source has it."""
import collections
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'init'), str(Path(__file__).resolve().parent)]
from initkit import kermit  # noqa: E402
import init_fake_loadb as loadb  # noqa: E402


class Line:
    """A sender wired to a receiver; `spoil(packet number, raw)` may damage or
    drop (return b'') what the sender writes."""

    def __init__(self, receiver=None, spoil=None, **sender):
        self.receiver = receiver or loadb.Receiver()
        self.spoil = spoil
        self.replies = collections.deque()
        self.written = []
        self.sender = kermit.Sender(self.write, self.read, timeout=0.002, **sender)

    def write(self, raw):
        number = len(self.written)
        self.written.append(raw)
        if self.spoil:
            raw = self.spoil(number, raw)
        reply = self.receiver.feed(raw)
        if reply:
            self.replies.append(reply)

    def read(self, timeout):
        return self.replies.popleft() if self.replies else b''


def image(size=40000, seed=5):
    return random.Random(seed).randbytes(size)


class Packets(unittest.TestCase):
    def test_a_short_packet_is_laid_out_as_the_protocol_has_it(self):
        # Start, length of what follows it, sequence, type, data, check, carriage return.
        raw = kermit.packet(3, 'D', b'AB')
        self.assertEqual(raw[:4], bytes((1, 32 + 5, 32 + 3, ord('D'))))
        self.assertEqual(raw[4:6], b'AB')
        total = sum(raw[1:6])
        self.assertEqual(raw[6], 32 + ((total + ((total >> 6) & 3)) & 0x3f))
        self.assertEqual(raw[7], 0x0d)

    def test_a_long_packet_has_a_zero_length_and_its_own_header_check(self):
        data = b'x' * 500
        raw = kermit.packet(9, 'D', data, long=True)
        self.assertEqual(raw[1], 32)                       # length zero: extended
        self.assertEqual((raw[4] - 32) * 95 + raw[5] - 32, len(data) + 1)
        self.assertEqual(raw[6], kermit.check(raw[1:6]))
        self.assertEqual(raw[-2], kermit.check(raw[1:-2]))

    def test_no_byte_below_space_goes_on_the_line_but_the_frame(self):
        # U-Boot throws a packet away at the first such byte.
        for raw in kermit.payloads(bytes(range(256)) * 3, 200):
            self.assertFalse([byte for byte in raw if byte < 0x20])

    def test_a_quote_pair_is_never_split_between_packets(self):
        data = bytes((0, 1, 2, 3)) * 50
        for limit in (3, 4, 5, 91):
            with self.subTest(limit):
                pieces = list(kermit.payloads(data, limit))
                self.assertTrue(all(len(piece) % 2 == 0 for piece in pieces))
                self.assertTrue(all(len(piece) <= limit for piece in pieces))

    def test_the_answer_a_real_unit_gave_is_understood(self):
        sender = kermit.Sender(lambda raw: None, lambda timeout: loadb.STOCK_ANSWER,
                               timeout=0.002)
        his = sender.exchange(0, 'S', b'')
        self.assertTrue(kermit.unchar(his[9]) & 2)                       # long packets
        self.assertEqual(kermit.unchar(his[11]) * 95 + kermit.unchar(his[12]), 9024)


class Transfers(unittest.TestCase):
    def test_every_byte_value_arrives(self):
        data = bytes(range(256)) * 20
        line = Line()
        stats = line.sender.send(data, b'all.bin')
        self.assertEqual(bytes(line.receiver.memory), data)
        self.assertTrue(line.receiver.done)
        self.assertEqual(stats['resent'], 0)
        self.assertTrue(stats['long_packets'])

    def test_the_packets_go_in_the_order_u_boot_expects(self):
        line = Line()
        line.sender.send(b'abc')
        self.assertEqual(line.receiver.kinds, list('SFDZB'))

    def test_an_empty_file_and_a_single_quote_character(self):
        for data in (b'', b'#', b'\x00', b'\xff'):
            with self.subTest(data):
                line = Line()
                line.sender.send(data)
                self.assertEqual(bytes(line.receiver.memory), data)

    def test_long_packets_are_used_only_where_u_boot_offers_them(self):
        data = image()
        line = Line(loadb.Receiver(long=False))
        stats = line.sender.send(data)
        self.assertEqual(bytes(line.receiver.memory), data)
        self.assertFalse(stats['long_packets'])
        # A length character counts at most 94 after it; the start and the end add three.
        self.assertEqual(max(len(raw) for raw in line.written), 97)

    def test_no_packet_is_longer_than_u_boot_says_it_takes(self):
        line = Line(long_limit=20000)                       # U-Boot's own limit is 9024
        line.sender.send(image(60000))
        self.assertLessEqual(max(len(raw) for raw in line.written), 9024 + 8)

    def test_the_sequence_number_wraps_at_64(self):
        data = image(30000)
        line = Line(long_limit=200)
        stats = line.sender.send(data)
        self.assertGreater(stats['packets'], 64 * 2)
        self.assertEqual(bytes(line.receiver.memory), data)

    def test_a_lost_packet_is_sent_again(self):
        data = image()
        line = Line(spoil=lambda number, raw: b'' if number in (0, 7, 8) else raw)
        stats = line.sender.send(data)
        self.assertEqual(bytes(line.receiver.memory), data)
        self.assertEqual(stats['timeouts'], 3)
        self.assertEqual(stats['resent'], 3)

    def test_a_packet_cut_short_is_refused_then_sent_again(self):
        # What a unit did to the first packet when it arrived while U-Boot printed.
        data = image()
        line = Line(spoil=lambda number, raw: raw[5:] if number == 0 else raw)
        stats = line.sender.send(data)
        self.assertEqual(bytes(line.receiver.memory), data)
        self.assertEqual(stats['resent'], 1)

    def test_a_damaged_packet_is_refused_then_sent_again(self):
        data = image()

        def spoil(number, raw):
            if number in (3, 4, 11):                        # a control byte in the data
                return raw[:10] + b'\x05' + raw[11:]
            return raw
        line = Line(spoil=spoil)
        stats = line.sender.send(data)
        self.assertEqual(bytes(line.receiver.memory), data)
        self.assertEqual(stats['refused'], 3)
        self.assertEqual(stats['resent'], 3)

    def test_a_line_that_never_answers_ends_the_transfer(self):
        line = Line(spoil=lambda number, raw: b'', tries=4)
        with self.assertRaisesRegex(kermit.Failed, r'packet 0 \(S\).*4 tries'):
            line.sender.send(b'data')
        self.assertEqual(len(line.written), 4)

    def test_an_acknowledgement_that_comes_twice_is_not_taken_for_the_next(self):
        line = Line()
        answer = line.receiver.answer
        line.receiver.answer = lambda kind, sequence, data=b'': answer(kind, sequence, data) * 2
        data = image(5000)
        line.sender.send(data)
        self.assertEqual(bytes(line.receiver.memory), data)

    def test_text_before_an_answer_is_skipped(self):
        line = Line()
        line.replies.append(b'## Ready for binary (kermit) download to 0x89000000...\r\n')
        line.sender.send(b'abc')
        self.assertEqual(bytes(line.receiver.memory), b'abc')

    def test_the_time_estimate_counts_the_quoting(self):
        self.assertEqual(kermit.line_bytes(b'ab'), 2)
        self.assertEqual(kermit.line_bytes(b'\x00#\xff\x80'), 8)


if __name__ == '__main__':
    unittest.main()
