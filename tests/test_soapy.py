# SPDX-License-Identifier: MIT
"""The SoapySDR module (host/soapy) through SoapySDR's own API (tests/soapy_check.cpp): w1700k-stream
fed by a fake export over loopback, and the fake controller of the spectrum server's tests standing
in for `w1700k serve`. What the device says of the receiver, which requests its setters make, and
that the samples come in order, bit for bit, with times that account for every one lost."""
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from stream_shm import Shm, ready as stream_ready
from test_stream import (BINARY as STREAM_BINARY, MAGIC, STREAM, build_problem, free_udp_port, noise_words, pack,
                         two_channels, unpacked)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOAPY = os.path.join(ROOT, "host", "soapy")
FAKE_CONTROLLER = os.path.join(ROOT, "tests", "spectrum_fake_controller.py")
SLICE = 2048


def soapy_problem():
    problem = build_problem()
    if problem:
        return problem
    probe = subprocess.run([os.environ.get("CXX", "c++"), "-x", "c++", "-fsyntax-only", "-"],
                           input="#include <SoapySDR/Device.hpp>\n", text=True, capture_output=True)
    return "SoapySDR headers missing" if probe.returncode else None


def samples_of(words):
    """A slice's samples as the stream serves them: int16 I, Q."""
    return struct.pack("<4096h", *(v for iq in unpacked(words) for v in iq))


class Arguments(str):
    """A device's arguments, and what the environment holds beside them."""
    def __new__(cls, text, **environment):
        made = super().__new__(cls, text)
        made.environment = environment
        return made


@unittest.skipIf(soapy_problem(), soapy_problem())
class SoapyTest(unittest.TestCase):
    counter = 0

    @classmethod
    def setUpClass(cls):
        for directory in (STREAM, SOAPY):
            result = subprocess.run(["make", "-C", directory], capture_output=True, text=True)
            if result.returncode:
                raise AssertionError("build failed:\n" + result.stdout + result.stderr)
        cls.tmp = tempfile.mkdtemp()
        cls.tool = os.path.join(cls.tmp, "soapy_check")
        subprocess.run([os.environ.get("CXX", "c++"), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        os.path.join(ROOT, "tests", "soapy_check.cpp"), "-lSoapySDR", "-o", cls.tool], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        SoapyTest.counter += 1
        self.name = "/w1700k-soapytest-%d-%d" % (os.getpid(), self.counter)
        self.port = free_udp_port()
        self.service = subprocess.Popen([STREAM_BINARY, "--bind", "127.0.0.1", "--port", str(self.port),
                                         "--name", self.name, "--ring-mib", "16", "--workers", "2",
                                         "--rx-threads", "2"], stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not stream_ready(self.name):
            self.assertLess(time.monotonic(), deadline, "w1700k-stream did not start")
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log = os.path.join(self.tmp, "controller-%d.jsonl" % self.counter)
        self.env = dict(os.environ, SOAPY_SDR_PLUGIN_PATH=os.path.join(SOAPY, "build"), SOAPY_SDR_LOG_LEVEL="ERROR")

    def tearDown(self):
        self.sock.close()
        self.service.send_signal(signal.SIGINT)
        self.service.wait(10)

    def args(self, controller="", more=""):
        """The device's arguments: this test's stream, with the fake controller in the environment
        the tool is then started in (None: no controller). Commands are the environment's to give,
        not an argument's."""
        if controller is None:
            return Arguments("driver=w1700k,stream=%s,controller=none%s" % (self.name, more))
        return Arguments("driver=w1700k,stream=%s%s" % (self.name, more),
                         W1700K_CONTROLLER="exec %s %s --log %s --progress-delay 0.01 %s" % (
                             sys.executable, FAKE_CONTROLLER, self.log, controller))

    def tool_start(self, args, *ops):
        return subprocess.Popen([self.tool, args, *ops], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(self.env, **getattr(args, "environment", {})))

    def run_tool(self, args, *ops):
        """Each op's JSON line."""
        process = self.tool_start(args, *ops)
        out, err = process.communicate(timeout=60)
        return [json.loads(line) for line in out.splitlines()], err

    def requests(self):
        """What the controller was asked, without the ids (and the first line, its pid)."""
        with open(self.log) as f:
            lines = [json.loads(line) for line in f]
        return [{k: v for k, v in line.items() if k != "id"} for line in lines[1:]]

    def send(self, seq, payload):
        self.sock.sendto(struct.pack("<IHHQ", MAGIC, 0, len(payload), seq) + payload, ("127.0.0.1", self.port))
        if seq % 32 == 31:
            time.sleep(0.002)

    def send_slices(self, seqs, words):
        for s in seqs:
            self.send(s, struct.pack("<2048I", *words[s]))

    def wait_newest(self, newest):
        shm = Shm(self.name)
        try:
            deadline = time.monotonic() + 10
            while shm.position()[1] < newest:
                self.assertLess(time.monotonic(), deadline, "the stream did not reach slice %d" % newest)
                time.sleep(0.01)
        finally:
            shm.close()

    def reading(self, process):
        self.assertEqual(json.loads(process.stdout.readline()).get("reading"), True)

    def finish(self, process):
        out, err = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, err)
        return json.loads(out.strip().splitlines()[-1])

    def test_the_device_describes_the_receiver(self):
        (state,), _ = self.run_tool(self.args(), "state")
        self.assertEqual((state["driver"], state["hardware"], state["channels"]), ("w1700k", "W1700K", 2))
        self.assertEqual(state["antennas"], ["2.4G", "5G", "6G"])
        self.assertEqual((state["antenna"], state["freq"], state["rate"], state["bw"]), ("5G", 5180e6, 40e6, 20e6))
        self.assertEqual(state["rates"], 4)
        self.assertEqual((state["formats"], state["native"], state["full_scale"]), (["CS16", "CF32"], "CS16", 8192))
        self.assertEqual(state["agc"], True)
        low, high = state["freq_ranges"]
        self.assertLess(low[1], 3e9)                            # the 2.4 GHz radio
        self.assertEqual(high, [4650e6, 7576.2e6],
                         "the 5 and 6 GHz radios as one span: the band in use as the unit reports it, the other's default")
        self.assertEqual(state["sensors"]["service"], "true")
        self.assertEqual(state["sensors"]["streaming"], "false")
        self.assertEqual(state["settings"], {"flatten": "false", "capture_640": "false"})
        self.assertEqual(self.requests(), [], "describing it asks the controller for nothing")

    def test_setters_are_the_controllers_requests(self):
        lines, _ = self.run_tool(
            self.args(),
            "freq=5300000000",          # inside the band in use: a tune
            "freq=2450000000",          # another band: the switch, with the LO, at the device's sample rate
            "rate=80000000",
            "antenna=6G",               # a band by name: the sample rate goes with it
            "bw=30000000",              # the narrowest filter that passes it
            "bw=0",                     # as wide as the sample rate again
            "agc=0", "gain:LNA=18", "gain:LPF=4", "agc=1",
            "setting:capture_640=true",
            "state")
        self.assertEqual([line for line in lines[:-1] if line != {"ok": True}], [])
        self.assertEqual(self.requests(), [
            {"op": "tune", "hz": 5300000000},
            {"op": "configure", "band": 0, "sample_rate": 40000000, "capture_rate": 40000000, "lo_hz": 2450000000},
            {"op": "configure", "band": 0, "sample_rate": 80000000, "capture_rate": 80000000},
            {"op": "configure", "band": 2, "sample_rate": 80000000, "capture_rate": 80000000},
            {"op": "filter", "mhz": 40},
            {"op": "filter_follow", "on": True},
            {"op": "gain", "lna": 2, "lpf": 5},
            {"op": "gain", "lna": 3, "lpf": 5},
            {"op": "gain", "lna": 3, "lpf": 2},
            {"op": "gain", "agc": True},
            {"op": "configure", "band": 2, "sample_rate": 320000000, "capture_rate": 640000000},
        ])
        state = lines[-1]
        self.assertEqual((state["antenna"], state["rate"], state["freq"]), ("6G", 320e6, 6105e6))
        self.assertEqual(state["settings"]["capture_640"], "true")

    def test_what_the_receiver_cannot_do_is_refused_with_the_reason(self):
        lines, _ = self.run_tool(self.args(), "rate=50000000", "freq=3500000000", "antenna=900M", "gain:IF=3", "state")
        self.assertIn("40, 80, 160 or 320 Msps", lines[0]["error"])
        self.assertIn("outside every band", lines[1]["error"])
        self.assertIn("2.4G, 5G or 6G", lines[2]["error"])
        self.assertIn("LNA or LPF", lines[3]["error"])
        self.assertEqual((lines[4]["freq"], lines[4]["rate"]), (5180e6, 40e6), "and it stays as it was")
        self.assertEqual(self.requests(), [], "none of it reached the controller")

    def test_a_radio_not_yet_loaded_is_loaded_by_the_first_setter(self):
        lines, _ = self.run_tool(self.args("--unloaded"), "state", "freq=5300000000")
        self.assertEqual(lines[1], {"ok": True})
        self.assertEqual(self.requests(), [{"op": "load"}, {"op": "tune", "hz": 5300000000}])

    def test_samples_come_in_order_with_their_times(self):
        """A stream that starts after the reader: from its first slice. One slice packed, one a
        firmware event (no samples, no time), one lost (an overflow, and its time passed over)."""
        words = {s: noise_words(s) for s in range(1300)}
        path = os.path.join(self.tmp, "order.cs16")
        want = 1298 * SLICE
        process = self.tool_start(self.args(), "read:CS16:%d:%s" % (want, path))
        self.reading(process)
        for s in range(1300):
            if s == 20:
                self.send(s, b"\x00" * 64)
            elif s == 30:
                continue
            elif s == 10:
                self.send(s, struct.pack("<1792I", *pack(words[s])))
            else:
                self.send(s, struct.pack("<2048I", *words[s]))
        report = self.finish(process)
        self.assertEqual((report["samples"], report["overflows"], report["lost_samples"]), (want, 1, SLICE))
        self.assertEqual((report["time_errors"], report["errors"]), (0, 0))
        self.assertEqual(report["span_ns"], round(1299 * SLICE * 1e9 / 40e6), "every sample's time, the lost one's too")
        with open(path, "rb") as f:
            got = f.read()
        expected = b"".join(samples_of(words[s]) for s in range(1300) if s not in (20, 30))
        self.assertEqual(len(got), len(expected))
        self.assertTrue(got == expected, "the samples, bit for bit")
        self.assertEqual(self.requests(), [{"op": "start"}, {"op": "stop"}], "it started the stream, so it stops it")

    def test_two_channels_are_read_as_two_buffers_of_the_same_instants(self):
        """A stream of channels 0 and 1: the device asks the controller for a two-channel stream
        (which tells the service, as the test does here), and gives each channel's samples in a
        buffer of its own, the same index the same instant. A lost slice is one gap in both."""
        shm = Shm(self.name)
        shm.set("radio_channels", 2)
        shm.close()
        time.sleep(0.3)
        words = {s: noise_words(s) for s in range(1300)}
        path = os.path.join(self.tmp, "two.cs16")
        per = SLICE // 2
        want = 1298 * per
        process = self.tool_start(self.args(), "read2:CS16:%d:%s" % (want, path))
        self.reading(process)
        for s in range(1300):
            if s == 20:
                self.send(s, b"\x00" * 64)
            elif s == 30:
                continue
            else:
                self.send(s, struct.pack("<2048I", *words[s]))
        report = self.finish(process)
        self.assertEqual((report["samples"], report["overflows"], report["lost_samples"]), (want, 1, per))
        self.assertEqual((report["time_errors"], report["errors"]), (0, 0))
        self.assertEqual(report["span_ns"], round(1299 * per * 1e9 / 40e6), "a channel's sample rate times its samples")
        for channel, name in ((0, path), (1, path + ".1")):
            with open(name, "rb") as f:
                got = f.read()
            expected = b"".join(struct.pack("<%dh" % (2 * per), *(v for iq in two_channels(words[s])[channel] for v in iq))
                                for s in range(1300) if s not in (20, 30))
            self.assertTrue(got == expected, "channel %d, bit for bit" % channel)
        self.assertEqual(self.requests(), [{"op": "start", "channels": 2}, {"op": "stop"}])

    def test_one_channel_of_a_two_channel_stream_is_channel_0(self):
        """A stream of two channels that somebody else runs, read as one: channel 0 of it."""
        shm = Shm(self.name)
        shm.set("radio_channels", 2)
        shm.close()
        time.sleep(0.3)
        words = {s: noise_words(s) for s in range(200)}
        path = os.path.join(self.tmp, "first.cs16")
        per = SLICE // 2
        process = self.tool_start(self.args("--running --channels 2"), "read:CS16:%d:%s" % (150 * per, path))
        self.reading(process)
        self.send_slices(range(200), words)
        report = self.finish(process)
        self.assertEqual((report["samples"], report["overflows"]), (150 * per, 0))
        with open(path, "rb") as f:
            got = f.read()
        expected = b"".join(struct.pack("<%dh" % (2 * per), *(v for iq in two_channels(words[s])[0] for v in iq)) for s in range(150))
        self.assertTrue(got == expected)
        self.assertEqual(self.requests(), [], "it is not this device's stream to start or stop")

    def test_two_channels_are_not_taken_from_a_one_channel_stream_already_running(self):
        (line,), err = self.run_tool(self.args("--running"), "read2:CS16:1000:-")
        self.assertIn("the stream that is running has one channel", line.get("error", "") + err)
        self.assertEqual(self.requests(), [], "and it is left running")

    def test_a_stream_already_running_is_joined_from_now_and_left_running(self):
        words = {s: noise_words(s) for s in range(300)}
        self.send_slices(range(100), words)
        self.wait_newest(100)
        path = os.path.join(self.tmp, "joined.cf32")
        process = self.tool_start(self.args("--running"), "read:CF32:%d:%s" % (150 * SLICE + 100, path))
        self.reading(process)
        self.send_slices(range(100, 300), words)
        report = self.finish(process)
        self.assertEqual((report["samples"], report["overflows"], report["time_errors"]), (150 * SLICE + 100, 0, 0))
        with open(path, "rb") as f:
            got = struct.unpack("<%df" % (2 * report["samples"]), f.read())
        expected = struct.unpack("<%dh" % (4096 * 151), b"".join(samples_of(words[s]) for s in range(100, 251)))
        self.assertEqual(list(got), [v / 8192 for v in expected[:len(got)]], "full scale 1.0, nothing before it joined")
        self.assertEqual(self.requests(), [], "another consumer's stream is not this device's to stop")

    def test_a_device_that_only_listens(self):
        words = {s: noise_words(s) for s in range(200)}
        path = os.path.join(self.tmp, "listen.cs16")
        process = self.tool_start(self.args(None), "freq=5300000000", "read:CS16:%d:%s" % (200 * SLICE, path))
        self.assertIn("only listens", json.loads(process.stdout.readline())["error"])
        self.reading(process)
        self.send_slices(range(200), words)
        out, _ = process.communicate(timeout=60)
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((report["samples"], report["overflows"]), (200 * SLICE, 0))
        with open(path, "rb") as f:
            self.assertTrue(f.read() == b"".join(samples_of(words[s]) for s in range(200)))

    def test_without_a_stream_service_opening_says_what_is_missing(self):
        (line,), _ = self.run_tool("driver=w1700k,stream=/w1700k-soapytest-none,service=none", "state")
        self.assertIn("no stream service at /w1700k-soapytest-none", line["error"])

    def test_a_stream_service_named_in_the_environment_is_started_and_ends_with_the_device(self):
        name = self.name + "-own"
        service = "%s --bind 127.0.0.1 --port %d --name %s --ring-mib 16 --workers 2 --rx-threads 2" % (
            STREAM_BINARY, free_udp_port(), name)
        (state,), _ = self.run_tool(Arguments("driver=w1700k,stream=%s,controller=none" % name, W1700K_STREAM_SERVICE=service),
                                    "state")
        self.assertEqual(state["sensors"]["service"], "true")
        self.assertFalse(stream_ready(name), "the service it started went with it, and took its shared memory")

    def test_a_stream_service_that_does_not_come_up_is_reported(self):
        (line,), _ = self.run_tool(Arguments("driver=w1700k,stream=%s-none,controller=none" % self.name,
                                             W1700K_STREAM_SERVICE="false"), "state")
        self.assertIn("did not come up", line["error"])

    def test_a_packing_reaches_the_controller_as_a_word_of_its_own(self):
        (state,), _ = self.run_tool(self.args(more=",packing=half"), "state")
        self.assertNotIn("error", state)
        with open(self.log) as f:
            self.assertEqual(json.loads(f.readline())["packing"], "half")

    def test_no_argument_is_a_command_or_becomes_part_of_one(self):
        """A device's arguments may be anybody's text (SoapyRemote makes a device from its clients'):
        the two commands come from the environment, and the rest is checked before anything runs."""
        mark = os.path.join(self.tmp, "ran-%d" % self.counter)
        touch = "touch %s" % mark
        for what, arguments, says in (
                ("a packing that closes its quote", self.args(more=",packing=raw'; %s; echo '" % touch), "packing= is one of raw, half"),
                ("a packing that is not one", self.args(more=",packing=$(%s)" % touch), "packing= is one of"),
                ("a stream name that closes its quote", Arguments("driver=w1700k,stream=/x'; %s; '" % touch,
                                                                  W1700K_STREAM_SERVICE="none"), "stream= names"),
                ("a stream name with no slash", Arguments("driver=w1700k,stream=w1700k-stream"), "stream= names"),
                ("a controller given as an argument", "driver=w1700k,stream=%s,controller=%s" % (self.name, touch),
                 "controller= is not taken from a device's arguments (only controller=none is): a command is the "
                 "environment's to give, W1700K_CONTROLLER=COMMAND"),
                ("a service given as an argument", "driver=w1700k,stream=%s-none,controller=none,service=%s" % (self.name, touch),
                 "service= is not taken from a device's arguments (only service=none is)"),
                ("both, and the service not needed", "driver=w1700k,stream=%s,controller=none,service=%s" % (self.name, touch),
                 "service= is not taken")):
            with self.subTest(what):
                (line,), _ = self.run_tool(arguments, "state")
                self.assertIn(says, line["error"])
                self.assertFalse(os.path.exists(mark), "nothing of it ran")
                self.assertFalse(os.path.exists(self.log), "and nothing else was started for it")


if __name__ == "__main__":
    unittest.main()
