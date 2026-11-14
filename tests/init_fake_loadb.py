# SPDX-License-Identifier: MIT
"""U-Boot's `loadb` receiver (cmd/load.c, `k_recv`), for testing the sender offline.

`feed()` takes what a sender wrote and returns what U-Boot would write back. It
follows U-Boot's code decision for decision: a character below space spoils a
packet, a packet with the previous one's sequence number replaces it, the first
packet is answered with what U-Boot takes, and the one-character checksum is all
that vouches for a packet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'init'))
from initkit.kermit import CR, QUOTE, SOH, check, tochar, unchar  # noqa: E402

# What stock U-Boot on a W1700K answered a first packet with (from a console log).
STOCK_ANSWER = b'\x010 Y~! @-#N1N" ~~#\r'


class Spoiled(Exception):
    pass


class Receiver:
    def __init__(self, long=True):
        self.long = long
        self.memory = bytearray()
        self.saved = 0
        self.last = -1
        self.buffer = bytearray()
        self.done = False
        self.kinds = []                 # the type of every packet acknowledged

    @staticmethod
    def translate(char):
        if (char & 0x60) == 0x40:
            return char & ~0x40
        if (char & 0x7f) == 0x3f:
            return char | 0x40
        return char

    @staticmethod
    def answer(kind, sequence, data=b''):
        body = bytes((tochar(len(data) + 3), tochar(sequence), ord(kind))) + data
        return bytes((SOH,)) + body + bytes((check(body), CR))

    def packet(self, raw):
        """`raw` follows a start character. Returns (bytes consumed, reply), or
        None while the packet is not all here."""
        position = total = 0
        sequence = 0

        def take():
            nonlocal position, total
            char = raw[position]
            position += 1
            if (char & 0xe0) == 0:
                raise Spoiled
            total += char
            return char

        def checksum():
            nonlocal position
            char = raw[position]
            position += 1
            if (char & 0xe0) == 0 or char != tochar((total + ((total >> 6) & 3)) & 0x3f):
                raise Spoiled
            return char

        try:
            if len(raw) < 3:
                return None
            length = unchar(take())
            sequence = unchar(take())
            if sequence == self.last:
                del self.memory[self.saved:]
            else:
                self.last = sequence
                self.saved = len(self.memory)
            kind = chr(take())
            length -= 2
            if length == -2:
                if len(raw) < position + 3:
                    return None
                length = unchar(take()) * 95
                length += unchar(take())
                total += checksum()
            if len(raw) < position + max(length, 1) + 1:
                return None
            data, parameters, quoted = bytearray(), bytearray(), False
            while length > 1:
                char = take()
                length -= 1
                if kind == 'S':
                    parameters.append(char)
                elif kind != 'D':
                    pass
                elif quoted:
                    data.append(self.translate(char))
                    quoted = False
                elif char == QUOTE:
                    quoted = True
                else:
                    data.append(char)
            checksum()
            if raw[position] != CR:
                position += 1
                raise Spoiled
            position += 1
        except Spoiled:
            del self.memory[self.saved:]
            return position, self.answer('N', sequence)
        self.memory += data
        self.kinds.append(kind)
        if kind == 'S':
            takes = bytes((tochar(94), tochar(1), tochar(0), 0x40, tochar(CR), QUOTE,
                           ord('N'), ord('1'), ord('N'), tochar(2 if self.long else 0),
                           tochar(0), tochar(94), tochar(94)))
            return position, self.answer('Y', sequence, takes if self.long else takes[:9])
        if kind == 'B':
            self.done = True
        return position, self.answer('Y', sequence)

    def feed(self, data):
        self.buffer += data
        replies = bytearray()
        while True:
            start = self.buffer.find(SOH)
            if start < 0:
                self.buffer.clear()
                return bytes(replies)
            del self.buffer[:start]
            result = self.packet(bytes(self.buffer[1:]))
            if result is None:
                return bytes(replies)
            consumed, reply = result
            del self.buffer[:1 + consumed]
            replies += reply
