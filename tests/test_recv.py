# SPDX-License-Identifier: MIT
"""sdr-recv as a consumer of w1700k-stream: a fake export over loopback into the service, the
recorder reading its shared memory. What it keeps, in order, and what it counts."""
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import unittest

from stream_shm import ready as stream_ready
from test_stream import BINARY as STREAM_BINARY, MAGIC, STREAM, build_problem, free_udp_port, noise_words, pack, unpacked

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECV = os.path.join(ROOT, "host", "recv")
BINARY = os.path.join(RECV, "build", "sdr-recv")


@unittest.skipIf(build_problem(), build_problem())
class SdrRecvTest(unittest.TestCase):
    counter = 0

    @classmethod
    def setUpClass(cls):
        for directory in (STREAM, RECV):
            result = subprocess.run(["make", "-C", directory], capture_output=True, text=True)
            if result.returncode:
                raise AssertionError("build failed:\n" + result.stdout + result.stderr)

    def setUp(self):
        SdrRecvTest.counter += 1
        self.name = "/w1700k-rtest-%d-%d" % (os.getpid(), self.counter)
        self.port = free_udp_port()
        self.service = subprocess.Popen([STREAM_BINARY, "--bind", "127.0.0.1", "--port", str(self.port),
                                         "--name", self.name, "--ring-mib", "16", "--workers", "2",
                                         "--rx-threads", "2"], stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not stream_ready(self.name):
            self.assertLess(time.monotonic(), deadline, "w1700k-stream did not start")
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tearDown(self):
        self.sock.close()
        self.service.send_signal(signal.SIGINT)
        self.service.wait(10)

    def recorder(self, *options):
        process = subprocess.Popen([BINARY, "--stream", self.name, "--idle-ms", "800", "--wait", "10", *options],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertIn("reading", process.stderr.readline())
        return process

    def finish(self, process):
        out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, err)
        return json.loads(out.strip().splitlines()[-1])

    def send(self, seq, payload):
        self.sock.sendto(struct.pack("<IHHQ", MAGIC, 0, len(payload), seq) + payload, ("127.0.0.1", self.port))
        if seq % 32 == 31:
            time.sleep(0.002)

    def test_records_the_next_run_from_its_start(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "stream.ci16")
        process = self.recorder("--out", path, "--capacity-mib", "64", "--seconds", "30")
        words = {s: noise_words(s) for s in range(1200)}
        for s in range(1200):
            if s == 20:
                self.send(s, b"\x00" * 64)                                   # a firmware event
            elif s == 30:
                continue                                                     # lost
            elif s == 10:
                self.send(s, struct.pack("<1792I", *pack(words[s])))         # packed
            else:
                self.send(s, struct.pack("<2048I", *words[s]))
        report = self.finish(process)
        self.assertEqual((report["run"], report["first_slice"], report["slices"]), (1, 0, 1199))
        self.assertEqual((report["events"], report["lost"], report["lost_examples"], report["overruns"]),
                         (1, 1, [30], 0))
        self.assertEqual(report["ended"], "idle")
        with open(path + ".json") as f:
            self.assertEqual(json.load(f)["slices"], 1199)
        expected = b"".join(
            b"\x00" * 8192 if s == 30 else struct.pack("<4096h", *(v for iq in unpacked(words[s]) for v in iq))
            for s in range(1200) if s != 20)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), expected)

    def test_counts_without_keeping(self):
        process = self.recorder("--seconds", "30")
        for s in range(200):
            self.send(s, struct.pack("<2048I", *noise_words(s)))
        report = self.finish(process)
        self.assertEqual((report["slices"], report["lost"], report["overruns"], report["out"]), (200, 0, 0, ""))



@unittest.skipIf(build_problem(), build_problem())
class NotTheServicesRing(unittest.TestCase):
    """The ring's name is one any user of the host can make an object under. A consumer takes its
    layout and its samples on trust, so it opens only an object that is this user's, that nobody
    else may write, and whose layout lies inside it; the service says when the name is held."""

    @classmethod
    def setUpClass(cls):
        for directory in (STREAM, RECV):
            result = subprocess.run(["make", "-C", directory], capture_output=True, text=True)
            if result.returncode:
                raise AssertionError("build failed:\n" + result.stdout + result.stderr)

    def setUp(self):
        self.name = "/w1700k-ftest-%d-%s" % (os.getpid(), self._testMethodName[-12:])
        self.path = "/dev/shm" + self.name

    def forged(self, slices, tags, samples, size=1 << 20, mode=0o600):
        """An object that says it is the ring (magic, version, slice size, its own size) and is
        served: a heartbeat now, and a run with slices published."""
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        self.addCleanup(os.unlink, self.path)
        try:
            os.ftruncate(fd, size)
            head = struct.pack("<IIIIQQQQQ", 0x4D525453, 1, 4096, 2048, slices, tags, samples, size, os.getpid())
            os.pwrite(fd, head, 0)
            os.pwrite(fd, struct.pack("<Q", time.clock_gettime_ns(time.CLOCK_MONOTONIC)), 56)
            os.pwrite(fd, struct.pack("<QQQ", 0, 1, 64), 128)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)

    def read(self):
        return subprocess.run([BINARY, "--stream", self.name, "--seconds", "1", "--wait", "1"], capture_output=True,
                              text=True, timeout=30)

    def test_a_layout_that_lies_outside_the_object_is_not_read(self):
        size = 1 << 20
        for what, (slices, tags, samples) in {
                "offsets far outside (this killed the reader)": (1 << 20, 1 << 40, 1 << 41),
                "more slices than the object holds": (1 << 10, 4096, 16384),
                "not a power of two": (96, 4096, 8192),
                "no slices": (0, 4096, 8192),
                "tags inside the header": (64, 512, 8192),
                "tags over the samples": (64, 4096, 4096 + 256),
                "samples past the end": (64, 4096, size - 4096)}.items():
            with self.subTest(what):
                self.forged(slices, tags, samples, size)
                result = self.read()
                self.doCleanups()
                self.assertEqual(result.returncode, 1, "refused, not killed: %s" % result.stderr)
                self.assertIn("its layout is not the ring's", result.stderr)

    def test_a_layout_that_fits_is_read(self):
        self.forged(64, 4096, 8192)          # 64 slices of 8192 bytes after 8 KiB: within 1 MiB
        result = self.read()
        self.assertIn("sdr-recv: reading " + self.name, result.stderr)

    def test_an_object_others_may_write_is_not_read(self):
        self.forged(64, 4096, 8192, mode=0o666)
        result = self.read()
        self.assertEqual(result.returncode, 1)
        self.assertIn("it belongs to someone else, or others may write it", result.stderr)

    def test_the_service_says_when_the_name_is_not_its_to_take(self):
        os.mkdir(self.path)                  # stands in for another user's object: it cannot be unlinked either
        self.addCleanup(os.rmdir, self.path)
        result = subprocess.run([STREAM_BINARY, "--bind", "127.0.0.1", "--port", str(free_udp_port()), "--name", self.name,
                                 "--ring-mib", "16"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn(self.name + " exists and is not this user's to replace", result.stderr)
        self.assertIn("--name", result.stderr)


if __name__ == "__main__":
    unittest.main()
