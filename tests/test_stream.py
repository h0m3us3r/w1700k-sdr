# SPDX-License-Identifier: MIT
"""w1700k-stream against a fake export over loopback, read through its shared memory: samples
unpacked bit-exact, events and losses tagged, the roll-off correction's gain where it applies and
nowhere else, runs restarted on request, and the export's integrity counted."""
import cmath
import fcntl
import math
import os
import resource
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
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
class Service(unittest.TestCase):
    """A w1700k-stream of the test's own, on a free port and under a name of its own."""
    counter = 0

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["make", "-C", STREAM], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("build failed:\n" + result.stdout + result.stderr)

    RECEIVE_MIB, RX_THREADS = 64, 2
    OPTIONS = []
    before_exec = None                 # run in the service's process before it starts

    def setUp(self):
        Service.counter += 1
        self.name = "/w1700k-test-%d-%d" % (os.getpid(), self.counter)
        self.port = free_udp_port()
        self.proc = subprocess.Popen([BINARY, "--bind", "127.0.0.1", "--port", str(self.port), "--name", self.name,
                                      "--ring-mib", "16", "--receive-mib", str(self.RECEIVE_MIB), "--workers", "2",
                                      "--rx-threads", str(self.RX_THREADS), *self.OPTIONS],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, preexec_fn=self.before_exec)
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


@unittest.skipIf(build_problem(), build_problem())
class StreamServiceTest(Service):
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
        self.assertEqual(self.shm.get("receive_cpu"), 0, "loopback has no CPU of its own to stay off")

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

    def test_radio_reduced_chunks_of_a_split_stream(self):
        """Every other chunk reduced by the radio (3588 bytes under SDR1, its shift in the trailer),
        the ones between by the unit's senders (SDR2): both come out as 14-bit samples. A completion
        of that length without the trailer is an event, as any short one."""
        words = {s: [word(round(900 * math.cos(0.21 * n + s)), round(900 * math.sin(0.05 * n * n))) for n in range(SLICE)]
                 for s in range(40)}
        shifts = {s: (4, 5, 7, 6)[s // 2 % 4] for s in range(0, 40, 2)}
        quiet = [word(round(100 * math.cos(0.3 * n)), round(100 * math.sin(0.3 * n))) for n in range(SLICE)]
        for s in range(1100):
            if s == 38:
                self.send(s, b"\x00" * iq_reduce_ref.RADIO14_BYTES)       # no trailer: an event
            elif s < 40 and s % 2 == 0:
                self.send(s, iq_reduce_ref.radio14_encode(words[s], shifts[s]))
            elif s < 40:
                self.send_reduced(s, words[s], 14)
            elif s in (50, 60):                 # radio chunks under a small shift (range 255) ...
                self.send(s, iq_reduce_ref.radio14_encode(quiet, 2))
            elif s == 51:                       # ... one beside a slice of the unit's that peaks at 900
                self.send_reduced(s, words[1], 14)
            elif s == 59:                       # ... one beside a quiet slice
                self.send_reduced(s, quiet, 14)
            else:
                self.send_words(s, noise_words(s))
        run = self.wait_newest(1100)
        self.assertEqual(self.shm.get("suspect"), 1, "the radio chunk beside the louder slice may have wrapped")
        self.assertEqual(self.shm.samples(50), iq_reduce_ref.radio14_restored(quiet, 2))
        for s in range(38):
            self.assertEqual(self.shm.tag(s), tag(run, s), "slot %d" % s)
            want = iq_reduce_ref.radio14_restored(words[s], shifts[s]) if s % 2 == 0 else iq_reduce_ref.restored(words[s], 14)
            self.assertEqual(self.shm.samples(s), want, "slot %d" % s)
        self.assertEqual(self.shm.tag(38), tag(run, 38) | TAG_EVENT)
        self.assertEqual((self.shm.get("reduced"), self.shm.get("events"), self.shm.get("lost"), self.shm.get("bad")),
                         (19 + 20 + 4, 1, 0, 0))
        # in range (900 fits every shift used): within half a step of the samples sent
        for s in (0, 2, 4, 6):
            for (i, q), v in zip(self.shm.samples(s), words[s]):
                self.assertLessEqual(abs(i - signed14(v >> 16 & 0x3FFF)), (1 << shifts[s]) // 2)
                self.assertLessEqual(abs(q - signed14(v & 0x3FFF)), (1 << shifts[s]) // 2)

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


def two_channels(words):
    """A two-channel slice's words as (channel 0's samples, channel 1's): a beat is four words,
    words 0 and 1 channel 0 at two sample times, words 2 and 3 channel 1 at the same two."""
    pairs = unpacked(words)
    return ([p for b in range(0, SLICE, 4) for p in pairs[b:b + 2]], [p for b in range(0, SLICE, 4) for p in pairs[b + 2:b + 4]])


def two_channel_tone_words(cycles0, cycles1, amplitude):
    """Channel 0 a tone of `cycles0` per slot (1024 sample times), channel 1 one of `cycles1`."""
    per = SLICE // 2
    tone = lambda cycles, t: word(round(amplitude * math.cos(2 * math.pi * cycles * t / per)),
                                  round(amplitude * math.sin(2 * math.pi * cycles * t / per)))
    out = []
    for t in range(0, per, 2):
        out += [tone(cycles0, t), tone(cycles0, t + 1), tone(cycles1, t), tone(cycles1, t + 1)]
    return out


@unittest.skipIf(build_problem(), build_problem())
class TwoChannels(Service):
    """A stream of two channels (the radio's two capture paths, announced in radio_channels before
    it starts): each slot is laid out by channel, its one tag covering both."""

    def test_a_slot_is_channel_0_then_channel_1_and_an_event_or_a_loss_is_both_channels(self):
        self.shm.set("radio_channels", 2)
        time.sleep(0.3)
        words = {s: noise_words(s) for s in range(1200)}
        for s in range(1200):
            if s == 20:
                self.send(s, b"\x00" * 64)             # a firmware event
            elif s == 30:
                continue                               # lost
            elif s == 10:
                self.send_words(s, pack(words[s]))     # packed: the packing does not know of channels
            else:
                self.send_words(s, words[s])
        run = self.wait_newest(1200)   # a loss is settled once a thousand newer are in
        self.assertEqual(self.shm.layout(), (run, 1200, 2))
        self.assertEqual(self.shm.tag(20), tag(run, 20) | TAG_EVENT)
        self.assertEqual(self.shm.tag(30), tag(run, 30) | TAG_LOST)
        for s in (0, 9, 10, 11, 31, 1199):
            want0, want1 = two_channels(words[s])
            self.assertEqual(self.shm.tag(s), tag(run, s), "slot %d" % s)
            self.assertEqual(self.shm.channel(s, 0), want0, "slot %d, channel 0" % s)
            self.assertEqual(self.shm.channel(s, 1), want1, "slot %d, channel 1" % s)
        self.assertEqual((self.shm.get("slices"), self.shm.get("events"), self.shm.get("lost")), (1198, 1, 1))

    def test_no_run_is_of_both_a_change_of_the_channels_begins_a_new_run(self):
        words = noise_words(3)
        for s in range(40):
            self.send_words(s, words)
        run = self.wait_newest(40)
        self.assertEqual(self.shm.layout(), (run, 40, 1))
        self.assertEqual(self.shm.samples(5), unpacked(words), "one channel: as it always was")
        self.shm.set("radio_channels", 2)
        time.sleep(0.3)
        for s in range(40, 80):
            self.send_words(s, noise_words(s))
        deadline = time.monotonic() + 10
        while self.shm.position()[0] == run:
            self.assertLess(time.monotonic(), deadline, "no new run")
            time.sleep(0.02)
        self.wait_newest(40)
        self.assertEqual(self.shm.layout(), (run + 1, 40, 2))
        self.assertEqual(self.shm.channel(0, 1), two_channels(noise_words(40))[1])
        self.shm.set("radio_channels", 0)              # 0 and 1 both say one channel
        time.sleep(0.3)
        for s in range(80, 120):
            self.send_words(s, noise_words(s))
        deadline = time.monotonic() + 10
        while self.shm.position()[0] == run + 1:
            self.assertLess(time.monotonic(), deadline, "no new run")
            time.sleep(0.02)
        self.wait_newest(40)
        self.assertEqual(self.shm.layout(), (run + 2, 40, 1))
        self.assertEqual(self.shm.samples(0), unpacked(noise_words(80)))

    def test_each_channel_is_corrected_as_a_stream_of_its_own(self):
        """The roll-off's correction at a channel's sample rate, applied to each channel: a tone in
        one channel comes out with that tone's gain and leaves nothing of itself in the other."""
        for field, value in (("radio_sample_rate", 320000000), ("radio_capture_rate", 320000000),
                             ("radio_filter_hz", 320000000), ("flatten", 1), ("radio_channels", 2)):
            self.shm.set(field, value)
        deadline = time.monotonic() + 5
        while self.shm.get("flatten_active") != 1:
            self.assertLess(time.monotonic(), deadline, "the correction never applied")
            time.sleep(0.01)
        time.sleep(0.3)
        amplitude, per = 600, SLICE // 2
        plan = [(128, 384), (-384, 461), (481, 128)]      # cycles per slot of 1024 sample times: channel 0, channel 1
        seq = 0
        for c0, c1 in plan:
            w = two_channel_tone_words(c0, c1, amplitude)
            for _ in range(48):
                self.send_words(seq, w)
                seq += 1
        run = self.wait_newest(seq)
        self.assertEqual(self.shm.layout(), (run, seq, 2))

        def amplitude_of(samples, cycles):
            acc = sum(complex(i, q) * cmath.exp(-2j * math.pi * cycles * n / per) for n, (i, q) in enumerate(samples))
            return abs(acc) / per

        for k, (c0, c1) in enumerate(plan):
            s = 48 * k + 24
            self.assertEqual(self.shm.tag(s), tag(run, s) | TAG_FLAT)
            for channel, cycles, other in ((0, c0, c1), (1, c1, c0)):
                samples = self.shm.channel(s, channel)
                got = 20 * math.log10(amplitude_of(samples, cycles) / amplitude)
                want = -rolloff_320(abs(cycles) / (per / 2))
                self.assertAlmostEqual(got, want, delta=0.4, msg="channel %d, %d cycles: %.2f dB, want %.2f" % (channel, cycles, got, want))
                self.assertLess(amplitude_of(samples, other), amplitude * 0.01, "nothing of the other channel's tone")
        self.assertEqual(self.shm.get("saturated"), 0)
        self.assertEqual(self.shm.get("flattened"), seq)


@unittest.skipIf(build_problem(), build_problem())
class IndexesNotBelieved(Service):
    """A slice's index is the sender's word. One that puts the stream somewhere it cannot be is not
    taken at it; the service goes on publishing either way, and stops when asked (tearDown)."""
    FAR = 1 << 20           # ingest.cpp's kRunJumpAhead

    def test_one_slice_far_ahead_of_its_stream_moves_nothing(self):
        for s in range(200):
            self.send_words(1000 + s, noise_words(s))
        run = self.wait_newest(200)
        # just under 2^39 past the run's first, which was once filed as the stream's newest slice:
        # every real slice after it was then "late", and the publisher set out to walk there
        for forged in (1000 + (1 << 39) - 1, 1000 + (1 << 30), 1000 + 200 + self.FAR + 1):
            self.send_words(forged, noise_words(7))
            time.sleep(0.05)
        for s in range(200, 400):
            self.send_words(1000 + s, noise_words(s))
        self.assertEqual(self.wait_newest(400), run, "the same run, and it went on being published")
        self.assertEqual(self.shm.samples(399), unpacked(noise_words(399)))
        self.assertEqual((self.shm.get("bad"), self.shm.get("lost"), self.shm.get("late")), (3, 0, 0))

    def test_a_stream_that_really_is_far_ahead_begins_a_run_once_enough_slices_say_so(self):
        for s in range(100):
            self.send_words(1000 + s, noise_words(s))
        run = self.wait_newest(100)
        there = 1000 + 5 * self.FAR
        for s in range(60):
            self.send_words(there + s, noise_words(500 + s))
        deadline = time.monotonic() + 10
        while self.shm.position()[0] == run:
            self.assertLess(time.monotonic(), deadline, "no new run")
            time.sleep(0.02)
        self.assertEqual(self.wait_newest(45), run + 1)
        self.assertEqual(self.shm.get("bad"), 15, "the slices it took to be believed")
        self.assertEqual(self.shm.samples(0), unpacked(noise_words(515)), "the run begins at the sixteenth")

    def test_a_stream_further_ahead_than_the_rings_hold_is_published_from_there_at_once(self):
        # within a run, and ahead by less than is refused: what lies between is lost in one step
        # (the publisher used to step to it a slot at a time)
        for s in range(100):
            self.send_words(s, noise_words(s))
        self.wait_newest(100)
        ahead, more = 500000, 1200  # more than the publisher's horizon: the gap's end is then given up too
        for s in range(more):
            self.send_words(ahead + s, noise_words(1000 + s % 100))
        run = self.wait_newest(ahead + more)
        self.assertEqual(self.shm.get("lost"), ahead - 100, "all of the gap, and none of what came")
        self.assertEqual(self.shm.samples(ahead + more - 1), unpacked(noise_words(1000 + (more - 1) % 100)))
        self.assertEqual(tag(run, ahead + more - 1), self.shm.tag(ahead + more - 1))

    def test_a_slice_after_a_record_of_odd_length_is_filed_as_it_came(self):
        # an event of 13 bytes and then a slice, raw in one datagram and packed in the next: neither
        # slice begins on a word boundary
        def record(seq, payload):
            return struct.pack("<IHHQ", MAGIC, 0, len(payload), seq) + payload
        raw, packed = noise_words(1), noise_words(2)
        squeezed = pack(packed)
        self.send_words(0, noise_words(0))
        self.sock.sendto(record(1, b"e" * 13) + record(2, struct.pack("<%dI" % len(raw), *raw)), ("127.0.0.1", self.port))
        self.sock.sendto(record(3, b"e" * 13) + record(4, struct.pack("<%dI" % len(squeezed), *squeezed)),
                         ("127.0.0.1", self.port))
        self.send_words(5, noise_words(5))
        run = self.wait_newest(6)
        self.assertEqual([self.shm.tag(n) for n in (1, 3)], [tag(run, 1) | TAG_EVENT, tag(run, 3) | TAG_EVENT])
        self.assertEqual(self.shm.samples(2), unpacked(raw))
        self.assertEqual(self.shm.samples(4), unpacked(packed))
        self.assertEqual(self.shm.get("bad"), 0)


@unittest.skipIf(build_problem(), build_problem())
class OnlyTheUnit(Service):
    """--unit: slices from the address named, and nobody else's word for anything."""
    OPTIONS = ["--unit", "127.0.0.2"]

    def test_a_named_unit_is_the_only_sender_heard(self):
        for s in range(40):
            self.send_words(s, noise_words(100 + s))       # from 127.0.0.1: somebody else
        time.sleep(0.5)
        self.assertEqual(self.shm.position(), (0, 0), "no run begun on a stranger's word")
        self.assertEqual(self.shm.get("bad"), 40)
        self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.2", 0))
        for s in range(40):
            self.send_words(s, noise_words(s))
        self.wait_newest(40)
        self.assertEqual(self.shm.samples(39), unpacked(noise_words(39)))
        self.assertEqual((self.shm.get("bad"), self.shm.get("slices")), (40, 40))


@unittest.skipIf(build_problem(), build_problem())
class TransformPlans(unittest.TestCase):
    """The service is ready at once, on FFTW's estimated plans of the correction's transforms,
    and moves to its measured ones, which a process of their own finds and keeps between runs.
    Where FFTW measures by the clock (arm64 as distributions build it) measuring takes most of a
    minute, and the service used to take that long to start: every test that starts one failed."""

    MEASURING = 300     # seconds a host is given to measure them; 54 where this was found

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["make", "-C", STREAM], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("build failed:\n" + result.stdout + result.stderr)

    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        self.kept = os.path.join(self.cache, "w1700k", "stream-fftwf-wisdom")
        self.env = dict(os.environ, XDG_CACHE_HOME=self.cache)
        self.count = 0

    def someone_measures(self):
        """As while another process measures the plans: none started now does."""
        os.makedirs(os.path.dirname(self.kept), exist_ok=True)
        held = open(self.kept + ".lock", "w")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)

    def service(self, env=None):
        """A service started; (process, the lines it has said, seconds until it served)."""
        self.count += 1
        name = "/w1700k-test-%d-plans-%d" % (os.getpid(), self.count)
        started = time.monotonic()
        proc = subprocess.Popen([BINARY, "--bind", "127.0.0.1", "--port", str(free_udp_port()), "--name", name,
                                 "--ring-mib", "16", "--receive-mib", "64", "--workers", "2", "--rx-threads", "2"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                env=self.env if env is None else env)
        said = []
        reader = threading.Thread(target=lambda: said.extend(iter(proc.stderr.readline, "")), daemon=True)
        reader.start()

        def stop():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.wait(10)
            reader.join(10)
            proc.stderr.close()
        self.addCleanup(stop)
        while not stream_shm.ready(name):
            self.assertIsNone(proc.poll(), "".join(said))
            self.assertLess(time.monotonic() - started, 10, "w1700k-stream did not start: " + "".join(said))
            time.sleep(0.02)
        return proc, said, time.monotonic() - started

    def wait_for(self, said, text, seconds):
        deadline = time.monotonic() + seconds
        while not any(text in line for line in said):
            self.assertLess(time.monotonic(), deadline, "never said %r:\n%s" % (text, "".join(said)))
            time.sleep(0.1)

    def measure(self, env=None):
        return subprocess.run([BINARY, "--measure-plans"], capture_output=True, text=True, timeout=self.MEASURING,
                              env=self.env if env is None else env)

    def test_it_serves_at_once_with_no_plans_kept(self):
        self.someone_measures()
        _, said, took = self.service()
        self.assertLess(took, 5)
        self.wait_for(said, "serving", 5)
        self.assertIn("estimated plans for now", "".join(said))
        self.assertIn(self.kept, "".join(said))

    def test_the_measured_plans_are_found_kept_and_taken_up(self):
        proc, said, _ = self.service()
        self.wait_for(said, "now run on FFTW's measured plans", self.MEASURING)
        self.assertGreater(os.path.getsize(self.kept), 0)
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(10), 0)
        # The next service has them from its first transform, and has nothing measured.
        written = os.stat(self.kept).st_mtime_ns
        _, said, took = self.service()
        self.wait_for(said, "serving", 5)
        self.assertLess(took, 5)
        self.assertNotIn("estimated", "".join(said))
        # Asked to measure again, there is nothing to do.
        started = time.monotonic()
        self.assertEqual(self.measure().returncode, 0)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(os.stat(self.kept).st_mtime_ns, written)

    def test_one_process_measures_them_at_a_time(self):
        self.someone_measures()
        started = time.monotonic()
        self.assertEqual(self.measure().returncode, 0)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(os.path.exists(self.kept))

    def test_kept_plans_that_are_not_plans_are_passed_over(self):
        os.makedirs(os.path.dirname(self.kept))
        with open(self.kept, "w") as f:
            f.write("not what FFTW wrote\n")
        self.someone_measures()
        _, said, took = self.service()
        self.assertLess(took, 5)
        self.wait_for(said, "serving", 5)
        self.assertIn("estimated plans for now", "".join(said))

    def test_with_nowhere_to_keep_them_it_serves_on_the_estimate_and_says_so(self):
        homeless = {k: v for k, v in os.environ.items() if k not in ("HOME", "XDG_CACHE_HOME")}
        _, said, took = self.service(homeless)
        self.assertLess(took, 5)
        self.wait_for(said, "serving", 5)
        self.assertIn("nowhere to keep", "".join(said))
        self.assertEqual(self.measure(homeless).returncode, 1)


def soft_nice_limit_none():
    """As `init/host/link keep` leaves a user: whatever hard limit there is, and no soft one."""
    resource.setrlimit(resource.RLIMIT_NICE, (0, resource.getrlimit(resource.RLIMIT_NICE)[1]))


@unittest.skipIf(build_problem(), build_problem())
@unittest.skipIf(os.geteuid() == 0, "root raises itself whatever the limits")
class OwnPriority(Service):
    """The service raises itself to nice -10 by the user's hard limit, taking its own soft limit
    up to it; a program that does not is refused, which is why the user is given the hard one alone."""
    before_exec = staticmethod(soft_nice_limit_none)

    def test_it_takes_the_hard_limit_and_another_program_is_refused(self):
        hard = resource.getrlimit(resource.RLIMIT_NICE)[1]
        ours = os.getpriority(os.PRIO_PROCESS, 0)
        lowest = -20 if hard == resource.RLIM_INFINITY else 20 - hard        # the nice level the hard limit allows
        its = os.getpriority(os.PRIO_PROCESS, self.proc.pid)
        if lowest <= -10:
            self.assertEqual(its, min(ours, -10))
            with open("/proc/%d/limits" % self.proc.pid) as limits:
                soft, hard_now = [line.split()[3:5] for line in limits if line.startswith("Max nice")][0]
            self.assertEqual(soft, hard_now, "its soft limit is now its hard one")
        else:
            self.assertEqual(its, ours, "a host whose limit allows no -10: it runs as it was started")
        raise_itself = "import os; os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0) - 1)"
        other = subprocess.run([sys.executable, "-c", raise_itself], capture_output=True, text=True,
                               preexec_fn=soft_nice_limit_none)
        self.assertNotEqual(other.returncode, 0, "a program that asks without raising its soft limit")
        self.assertIn("PermissionError", other.stderr)


class Options(unittest.TestCase):
    def test_a_unit_that_is_not_an_address_is_refused(self):
        result = subprocess.run([BINARY, "--unit", "the-unit", "--name", "/w1700k-test-%d-unit" % os.getpid(),
                                 "--bind", "127.0.0.1", "--port", str(free_udp_port()), "--ring-mib", "16",
                                 "--receive-mib", "64"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an IPv4 address: the-unit", result.stderr)


@unittest.skipIf(build_problem() or not shutil.which("cc"), build_problem() or "no C compiler")
class NewRunWhileReceiving(Service):
    """A new run asked for while slices arrive on every receive thread at once, as the spectrum server
    asks when it notices late that one stream ended and the next has begun."""
    RECEIVE_MIB, RX_THREADS = 128, 4       # a reorder ring of 16384 slices
    RATE, SECONDS, EVERY = 120_000, 5.0, 0.3

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tools = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tools.cleanup)
        cls.blast = os.path.join(cls.tools.name, "stream_blast")
        result = subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread", "-o", cls.blast,
                                 os.path.join(ROOT, "tests", "stream_blast.c")], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("stream_blast did not build:\n" + result.stderr)

    def test_a_slice_of_the_run_that_ended_does_not_move_the_new_runs_position(self):
        """Each receive thread reads the run and then files its slice. One that read it just before
        another began a new run files a slice of the old run, at the old run's index; that index must
        not become the new run's newest slot, or everything the new run receives is older than the
        ring holds and is dropped as late until its numbering has caught up: all of a 40 Msps stream
        for a quarter of a minute, when this was found on a unit. Here a run is twice the ring long
        when the next is asked for, so one such slice costs the new run some 20,000 slices."""
        blaster = subprocess.Popen([self.blast, str(self.port), str(MAGIC), str(self.RX_THREADS), str(self.RATE),
                                    str(self.SECONDS)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(blaster.stdout.close)
        asked, due = 0, time.monotonic() + self.EVERY
        while blaster.poll() is None:
            if time.monotonic() >= due:
                self.shm.set("restart_requests", self.shm.get("restart_requests") + 1)
                asked, due = asked + 1, due + self.EVERY
            time.sleep(0.005)
        self.assertEqual(blaster.returncode, 0)
        sent = int(blaster.stdout.read())
        time.sleep(0.6)                                 # the last slices are published, the counters refreshed
        run, _ = self.shm.position()
        self.assertGreaterEqual(run, asked, "every request began a run")
        self.assertGreater(self.shm.get("slices"), sent // 2, "the slices were received, not dropped on the way in")
        # A receive thread kept from running for longer than the ring is deep would file what it
        # held as late too: tens of slices, where the fault files tens of thousands.
        self.assertLess(self.shm.get("late"), 1000,
                        "slices of the new run were dropped as late: a slice of the run before moved its position")


if __name__ == "__main__":
    unittest.main()
