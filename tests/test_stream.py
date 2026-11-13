# SPDX-License-Identifier: MIT
"""w1700k-stream against a fake export over loopback, read through its shared memory: samples
unpacked bit-exact, events and losses tagged, the roll-off correction's gain where it applies and
nowhere else, runs restarted on request, and the export's integrity counted."""
import cmath
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import time
import unittest

import iq_reduce_ref
import stream_shm
from stream_shm import TAG_EVENT, TAG_FLAT, TAG_LOST, Shm, tag

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAM = os.path.join(ROOT, "host", "stream")
BINARY = os.path.join(STREAM, "build", "w1700k-stream")
MAGIC = 0x31524453
SLICE = stream_shm.SLICE

# The 320 Msps roll-off (host/stream/src/flatten.cpp), dB at k/64 of Nyquist.
ROLLOFF_320 = [0.0] * 29 + [
    -0.1, -0.2, -0.3, -0.5, -0.6, -0.8, -1.1, -1.4, -1.7, -2.0, -2.4, -2.8, -3.2, -3.6, -4.1, -4.6, -5.1,
    -5.7, -6.3, -6.9, -7.6, -8.4, -9.2, -10.1, -11.0, -12.0, -13.0, -14.0, -15.2, -16.4, -17.8, -19.1,
    -20.5, -21.7, -22.6, -23.1]


def rolloff_320(x):
    x = min(max(x, 0.0), 1.0) * 64
    k = min(int(x), 63)
    return ROLLOFF_320[k] + (x - k) * (ROLLOFF_320[k + 1] - ROLLOFF_320[k])


def build_problem():
    if not shutil.which("make") or not shutil.which(os.environ.get("CXX", "c++")):
        return "no make or C++ compiler"
    probe = subprocess.run([os.environ.get("CXX", "c++"), "-x", "c++", "-fsyntax-only", "-"],
                           input="#include <fftw3.h>\n", text=True, capture_output=True)
    return "FFTW3 headers missing" if probe.returncode else None


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def word(i, q):
    return ((i & 0x3FFF) << 16) | (q & 0x3FFF)


def signed14(v):
    return v - 0x4000 if v & 0x2000 else v


def noise_words(seed):
    """A slice of distinct pseudo-random 14-bit samples, spare bits clear."""
    return [((seed * 7919 + n) * 2654435761) & 0x3FFF3FFF for n in range(SLICE)]


def tone_words(cycles, amplitude):
    return [word(round(amplitude * math.cos(2 * math.pi * cycles * n / SLICE)),
                 round(amplitude * math.sin(2 * math.pi * cycles * n / SLICE))) for n in range(SLICE)]


def pack(words):
    out = []
    for g in range(0, len(words), 8):
        w = words[g:g + 8]
        out.extend(w[k] | ((w[7] << (14 - 2 * k)) & 0xC000C000) for k in range(7))
    return out


def unpacked(words):
    return [(signed14(w >> 16 & 0x3FFF), signed14(w & 0x3FFF)) for w in words]


def tone_amplitude(samples, cycles):
    """|correlation with the slice's complex exponential at `cycles`| / N."""
    acc = sum(complex(i, q) * cmath.exp(-2j * math.pi * cycles * n / SLICE) for n, (i, q) in enumerate(samples))
    return abs(acc) / SLICE


@unittest.skipIf(build_problem(), build_problem())
class StreamServiceTest(unittest.TestCase):
    counter = 0

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["make", "-C", STREAM], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("build failed:\n" + result.stdout + result.stderr)

    def setUp(self):
        StreamServiceTest.counter += 1
        self.name = "/w1700k-test-%d-%d" % (os.getpid(), self.counter)
        self.port = free_udp_port()
        self.proc = subprocess.Popen([BINARY, "--bind", "127.0.0.1", "--port", str(self.port), "--name", self.name,
                                      "--ring-mib", "16", "--workers", "2", "--rx-threads", "2"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.shm = Shm(self.name)
                break
            except (OSError, AssertionError):
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    raise AssertionError("w1700k-stream did not start: " + self.proc.stderr.read().decode())
                time.sleep(0.05)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        time.sleep(0.2)

    def tearDown(self):
        self.sock.close()
        self.shm.close()
        self.proc.send_signal(signal.SIGINT)
        self.assertEqual(self.proc.wait(10), 0)
        self.proc.stderr.close()
        self.assertFalse(os.path.exists("/dev/shm" + self.name), "the object is unlinked at exit")

    def send(self, seq, payload):
        self.sock.sendto(struct.pack("<IHHQ", MAGIC, 0, len(payload), seq) + payload, ("127.0.0.1", self.port))

    def send_pieces(self, magic, last, payload, piece, skip=()):
        """A slice as datagrams of at most `piece` payload bytes; `skip`: pieces not sent."""
        for n, at in enumerate(range(0, len(payload), piece)):
            if n not in skip:
                self.sock.sendto(struct.pack("<IHHQ", magic, at, len(payload), last) + payload[at:at + piece],
                                 ("127.0.0.1", self.port))

    def send_reduced(self, seq, words, fmt, piece=None):
        code, data = iq_reduce_ref.encode(words, fmt)
        self.send_pieces(iq_reduce_ref.MAGIC, iq_reduce_ref.header_word(seq, fmt, code), data, piece or len(data))

    def send_words(self, seq, words):
        self.send(seq, struct.pack("<%dI" % len(words), *words))
        if seq % 32 == 31:
            time.sleep(0.002)  # loopback: let the receivers keep up

    def wait_newest(self, n, timeout=10):
        """The run once `n` slots are published, and the counters refreshed (every 100 ms)."""
        deadline = time.monotonic() + timeout
        while True:
            run, newest = self.shm.position()
            if newest >= n:
                time.sleep(0.25)
                return run
            if time.monotonic() > deadline:
                raise AssertionError("published %d of %d" % (newest, n))
            time.sleep(0.02)

    def test_slices_unpacked_bit_exact_events_and_losses_tagged(self):
        words = {s: noise_words(s) for s in range(1200)}
        for s in range(1200):
            if s == 20:
                self.send(s, b"\x00" * 64)             # a firmware event
            elif s == 30:
                continue                               # lost
            elif s == 10:
                self.send_words(s, pack(words[s]))     # packed
            else:
                self.send_words(s, words[s])
        run = self.wait_newest(1200)
        self.assertEqual(self.shm.tag(20), tag(run, 20) | TAG_EVENT)
        self.assertEqual(self.shm.tag(30), tag(run, 30) | TAG_LOST)
        for s in (0, 9, 10, 11, 31, 1199):
            self.assertEqual(self.shm.tag(s), tag(run, s), "slot %d" % s)
            self.assertEqual(self.shm.samples(s), unpacked(words[s]), "slot %d" % s)
        self.assertEqual((self.shm.get("packed"), self.shm.get("events"), self.shm.get("lost")), (1, 1, 1))
        self.assertEqual(self.shm.get("slices"), 1198)
        self.assertEqual(self.shm.get("flatten_active"), 0)

    def test_reduced_slices_are_restored_by_their_scale(self):
        formats = (16, 14, 8, 7)
        words = {}
        for s in range(64):
            amplitude = (3, 60, 700, 8000)[s // 4 % 4]
            words[s] = [word(round(amplitude * math.cos(0.37 * n + s)), round(amplitude * math.sin(0.11 * n * n)))
                        for n in range(SLICE)]
        for s in range(1100):
            if s < 64:
                self.send_reduced(s, words[s], formats[s % 4])
            else:
                self.send_words(s, noise_words(s))
        run = self.wait_newest(1100)
        for s in range(64):
            self.assertEqual(self.shm.tag(s), tag(run, s), "slot %d" % s)
            self.assertEqual(self.shm.samples(s), iq_reduce_ref.restored(words[s], formats[s % 4]),
                             "slot %d, format %d" % (s, formats[s % 4]))
        self.assertEqual(self.shm.samples(100), unpacked(noise_words(100)))
        self.assertEqual((self.shm.get("reduced"), self.shm.get("slices"), self.shm.get("lost")), (64, 1100, 0))
        self.assertEqual(self.shm.get("bad"), 0)

    def test_slices_in_pieces_are_assembled_and_a_broken_one_is_lost(self):
        words = {s: noise_words(s) for s in range(1100)}
        for s in range(1100):
            raw = struct.pack("<%dI" % SLICE, *words[s])
            if s == 5:
                self.send_pieces(MAGIC, s, raw, 1456)                          # raw, six pieces
            elif s == 6:
                self.send_pieces(MAGIC, s, struct.pack("<%dI" % (SLICE // 8 * 7), *pack(words[s])), 1456)
            elif s == 7:
                self.send_reduced(s, words[s], 14, piece=1456)
            elif s == 8:
                self.send_reduced(s, words[s], 7, piece=700)
            elif s == 9:
                self.send_pieces(MAGIC, s, raw, 1456, skip=(2,))               # a piece lost on the way
            elif s == 10:
                self.send_pieces(MAGIC, s, raw, 1456, skip=(0,))               # its first piece lost
            else:
                self.send_words(s, words[s])
        run = self.wait_newest(1100)
        for s in (4, 5, 6, 11):
            self.assertEqual(self.shm.tag(s), tag(run, s), "slot %d" % s)
            self.assertEqual(self.shm.samples(s), unpacked(words[s]), "slot %d" % s)
        self.assertEqual(self.shm.samples(7), iq_reduce_ref.restored(words[7], 14))
        self.assertEqual(self.shm.samples(8), iq_reduce_ref.restored(words[8], 7))
        for s in (9, 10):
            self.assertEqual(self.shm.tag(s), tag(run, s) | TAG_LOST, "slot %d" % s)
        self.assertEqual((self.shm.get("lost"), self.shm.get("slices"), self.shm.get("bad")), (2, 1098, 0))
        self.assertEqual(self.shm.get("pieces"), 2)

    def test_several_slices_in_one_datagram(self):
        """The unit's senders put as many whole slices in a datagram as fit, each behind its header."""
        words = {s: noise_words(s + 50) for s in range(8)}

        def record(s, fmt=None):
            if fmt is None:
                payload = struct.pack("<%dI" % SLICE, *words[s])
                return struct.pack("<IHHQ", MAGIC, 0, len(payload), s) + payload
            code, data = iq_reduce_ref.encode(words[s], fmt)
            return struct.pack("<IHHQ", iq_reduce_ref.MAGIC, 0, len(data), iq_reduce_ref.header_word(s, fmt, code)) + data
        to = ("127.0.0.1", self.port)
        self.sock.sendto(record(0, 14) + record(1, 14), to)
        self.sock.sendto(record(2, 7) + record(3, 7) + record(4, 8), to)
        self.sock.sendto(record(6), to)
        self.sock.sendto(record(5, 16) + record(7, 14) + b"\x00" * 5, to)  # and five bytes that are no record
        for s in range(8, 1100):
            self.send_words(s, noise_words(s))
        run = self.wait_newest(1100)
        formats = {0: 14, 1: 14, 2: 7, 3: 7, 4: 8, 5: 16, 7: 14}
        for s in range(8):
            self.assertEqual(self.shm.tag(s), tag(run, s), "slot %d" % s)
            want = iq_reduce_ref.restored(words[s], formats[s]) if s in formats else unpacked(words[s])
            self.assertEqual(self.shm.samples(s), want, "slot %d" % s)
        self.assertEqual((self.shm.get("reduced"), self.shm.get("lost"), self.shm.get("bad")), (7, 0, 1))

    def test_a_reduced_slice_of_the_wrong_length_is_refused(self):
        code, data = iq_reduce_ref.encode(noise_words(1), 14)
        self.send_pieces(iq_reduce_ref.MAGIC, iq_reduce_ref.header_word(0, 16, code), data, len(data))
        self.send_pieces(iq_reduce_ref.MAGIC, iq_reduce_ref.header_word(1, 12, code), data, len(data))
        for s in range(2, 1100):
            self.send_words(s, noise_words(s))
        self.wait_newest(1098)                         # the run starts with the first slice taken
        self.assertEqual((self.shm.get("bad"), self.shm.get("reduced")), (2, 0))

    def test_flatten_gain_follows_the_measured_rolloff(self):
        for field, value in (("radio_sample_rate", 320000000), ("radio_capture_rate", 320000000),
                             ("radio_filter_hz", 320000000), ("flatten", 1)):
            self.shm.set(field, value)
        deadline = time.monotonic() + 5
        while self.shm.get("flatten_active") != 1:
            self.assertLess(time.monotonic(), deadline, "the correction never applied")
            time.sleep(0.01)
        # Tones of integer cycles per slice (continuous across slices), 48 slices each.
        plan = [256, 768, -768, 922, 963]
        amplitude = 600
        seq = 0
        for cycles in plan:
            w = tone_words(cycles, amplitude)
            for _ in range(48):
                self.send_words(seq, w)
                seq += 1
        run = self.wait_newest(seq)
        self.assertEqual((self.shm.get("flatten_active"), self.shm.get("flatten_rate")), (1, 320000000))
        for k, cycles in enumerate(plan):
            s = 48 * k + 24                            # well inside the tone's block
            self.assertEqual(self.shm.tag(s), tag(run, s) | TAG_FLAT)
            got = 20 * math.log10(tone_amplitude(self.shm.samples(s), cycles) / amplitude)
            want = -rolloff_320(abs(cycles) / 1024)
            self.assertAlmostEqual(got, want, delta=0.4, msg="%d cycles: %.2f dB, want %.2f" % (cycles, got, want))
        self.assertEqual(self.shm.get("saturated"), 0)
        self.assertEqual(self.shm.get("flattened"), seq)

    def test_no_correction_unless_the_filter_matches_the_rate(self):
        for field, value in (("radio_sample_rate", 320000000), ("radio_capture_rate", 320000000),
                             ("radio_filter_hz", 160000000), ("flatten", 1)):
            self.shm.set(field, value)
        words = noise_words(7)
        for s in range(40):
            self.send_words(s, words)
        run = self.wait_newest(40)
        self.assertEqual(self.shm.get("flatten_active"), 0)
        self.assertEqual(self.shm.tag(5), tag(run, 5))
        self.assertEqual(self.shm.samples(5), unpacked(words))
        self.shm.set("radio_filter_hz", 320000000)
        self.shm.set("radio_capture_rate", 640000000)  # 640/2: flat already, no correction
        time.sleep(0.3)
        self.assertEqual(self.shm.get("flatten_active"), 0)

    def test_restart_and_integrity_counters(self):
        base = [noise_words(s) for s in range(128)]
        for s in range(300):
            w = list(base[s % 128])                     # every slice repeats one lap later: stale
            if s == 3:
                w[0] |= 0xC0000000                      # spare bits set in a raw slice
            self.send_words(s, w)
        run = self.wait_newest(300)
        self.assertEqual(self.shm.get("spare_words"), 1)
        self.assertEqual(self.shm.get("stale_slices"), 300 - 128 - 1)  # 131 differs from 3 by the spare bit
        self.assertGreater(self.shm.get("stale_lines"), 0)
        self.shm.set("restart_requests", self.shm.get("restart_requests") + 1)
        time.sleep(0.3)
        for s in range(300, 340):
            self.send_words(s, noise_words(s))
        deadline = time.monotonic() + 10
        while self.shm.position()[0] == run:
            self.assertLess(time.monotonic(), deadline, "no new run")
            time.sleep(0.02)
        run2 = self.wait_newest(40)
        self.assertEqual(run2, run + 1)
        self.assertEqual(self.shm.samples(0), unpacked(noise_words(300)), "the new run counts from its first slice")


if __name__ == "__main__":
    unittest.main()
