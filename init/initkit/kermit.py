# SPDX-License-Identifier: MIT
"""A Kermit sender for U-Boot's `loadb`.

U-Boot's receiver (cmd/load.c, `k_recv`) takes one-character checksums, control
characters quoted with `#`, no eighth-bit prefix, no repeat counts, and long
packets where it says so in its answer to the first packet. This sends exactly
that and nothing else of the protocol.

The one-character checksum is six bits. A noisy line can therefore get a wrong
packet accepted, so the caller must check what arrived (stock's `crc32` over the
image in RAM) before anything uses it.
"""
import time

SOH, CR, QUOTE = 0x01, 0x0d, 0x23
# The most a packet's data may hold when U-Boot does not offer long packets.
SHORT_PAYLOAD = 91


def tochar(value):
    return value + 32


def unchar(char):
    return (char - 32) & 0xff


def check(body):
    total = sum(body)
    return tochar((total + ((total >> 6) & 3)) & 0x3f)


def quote(data):
    """One encoded unit per byte of `data`, so a packet never ends inside a quote."""
    units = []
    for byte in data:
        if (byte & 0x7f) < 0x20 or (byte & 0x7f) == 0x7f:
            units.append(bytes((QUOTE, byte ^ 0x40)))
        elif byte == QUOTE:
            units.append(bytes((QUOTE, QUOTE)))
        else:
            units.append(bytes((byte,)))
    return units


def packet(sequence, kind, data=b'', long=False):
    if long:
        size = len(data) + 1
        header = bytes((tochar(0), tochar(sequence), ord(kind),
                        tochar(size // 95), tochar(size % 95)))
        body = header + bytes((check(header),)) + data
    else:
        body = bytes((tochar(len(data) + 3), tochar(sequence), ord(kind))) + data
    return bytes((SOH,)) + body + bytes((check(body), CR))


def payloads(data, limit):
    """`data` as the quoted contents of data packets, each at most `limit` long."""
    current = bytearray()
    for unit in quote(data):
        if len(current) + len(unit) > limit:
            yield bytes(current)
            current = bytearray()
        current += unit
    if current:
        yield bytes(current)


def line_bytes(data):
    """About how many bytes `data` is on the line, for an estimate of the time."""
    return sum(len(unit) for unit in quote(data))


class Failed(RuntimeError):
    """A packet was not acknowledged in the tries allowed."""


class Sender:
    """Send one file. `write(bytes)` transmits; `read(seconds)` returns what has
    arrived, waiting up to that long for the first byte."""

    def __init__(self, write, read, long_limit=1000, timeout=2.0, tries=12):
        self.write, self.read = write, read
        self.long_limit, self.timeout, self.tries = long_limit, timeout, tries
        self.pending = bytearray()
        self.stats = {'packets': 0, 'resent': 0, 'refused': 0, 'timeouts': 0, 'line_bytes': 0}

    def _reply(self, deadline):
        """The next whole, intact packet from the receiver as (type, sequence,
        data), or None at the deadline."""
        while True:
            start = self.pending.find(SOH)
            if start < 0:
                self.pending.clear()
            else:
                del self.pending[:start]
                if len(self.pending) >= 2:
                    size = unchar(self.pending[1])
                    if len(self.pending) >= size + 3:
                        body = bytes(self.pending[1:size + 1])
                        intact = size >= 3 and self.pending[size + 1] == check(body)
                        del self.pending[:size + 2]
                        if intact:
                            return chr(body[2]), unchar(body[1]), body[3:]
                        continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.pending += self.read(remaining)

    def exchange(self, sequence, kind, data=b'', long=False):
        """Send one packet until it is acknowledged; returns the acknowledgement's data."""
        raw = packet(sequence, kind, data, long)
        for attempt in range(self.tries):
            if attempt:
                self.stats['resent'] += 1
            self.write(raw)
            self.stats['line_bytes'] += len(raw)
            deadline = time.monotonic() + self.timeout
            while True:
                reply = self._reply(deadline)
                if reply is None:
                    self.stats['timeouts'] += 1
                    break
                answer, number, payload = reply
                if answer == 'Y' and number == sequence:
                    self.stats['packets'] += 1
                    return payload
                if answer == 'N':
                    self.stats['refused'] += 1
                    break
                # The acknowledgement of an earlier packet, late: keep waiting.
        raise Failed(f'packet {sequence} ({kind}) was not acknowledged in {self.tries} tries')

    def send(self, data, name=b'image'):
        mine = bytes((tochar(94), tochar(5), tochar(0), 0x40, tochar(CR), QUOTE,
                      ord('N'), ord('1'), ord(' '), tochar(2), tochar(0),
                      tochar(self.long_limit // 95), tochar(self.long_limit % 95)))
        his = self.exchange(0, 'S', mine)
        # His answer is what he takes: bit 1 of the capabilities is long packets,
        # and two characters after it their largest size.
        long = len(his) >= 13 and bool(unchar(his[9]) & 2)
        limit = SHORT_PAYLOAD
        if long:
            limit = min(self.long_limit, unchar(his[11]) * 95 + unchar(his[12])) - 1
        self.stats['long_packets'] = long
        sequence = 1
        self.exchange(sequence, 'F', name)
        for payload in payloads(data, limit):
            sequence = (sequence + 1) % 64
            self.exchange(sequence, 'D', payload, long=long)
        for kind in 'ZB':
            sequence = (sequence + 1) % 64
            self.exchange(sequence, kind)
        return self.stats
