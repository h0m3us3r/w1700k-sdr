# SPDX-License-Identifier: MIT
"""w1700k-spectrum end to end, without hardware.

A fake controller (spectrum_fake_controller.py) stands in for `w1700k serve`, a fake exporter
sends "SDR1" datagrams over loopback to w1700k-stream, which serves them to the spectrum server,
and a minimal WebSocket client plays the browser. The
exporter follows the fake radio's tuning the way the real stream does: its samples carry a tone
at one fixed RF frequency, so a frame labelled with the wrong tuning shows it in the wrong bin.
The tests check that the page's messages reach the controller as the right ops, that the
controller's state relabels the status and the frames, that no frame across a retune is
mislabelled, and that a controller that dies is reported and restarted.
"""
import base64
import collections
import fcntl
import glob
import hashlib
import json
import math
import os
import shlex
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

import stream_shm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECTRUM = os.path.join(ROOT, "host", "spectrum")
BINARY = os.path.join(SPECTRUM, "build", "w1700k-spectrum")
STREAM = os.path.join(ROOT, "host", "stream")
STREAM_BINARY = os.path.join(STREAM, "build", "w1700k-stream")
FAKE_CONTROLLER = os.path.join(ROOT, "tests", "spectrum_fake_controller.py")

MAGIC = 0x31524453
SLICE_SAMPLES = 2048
TONE_HZ = 5185e6     # the RF frequency of the exporter's tone
TONE_1_HZ = 5174e6   # ... and of the tone it puts in channel 1 of a two-channel stream
FFT_SIZE = 4096
DUMP_SECONDS = 0.01  # the length of a SIGUSR1 snapshot here
DUMP_BEHIND = 64     # slices a snapshot ends short of the newest (serve.cpp)


def build_problem():
    """Why the server cannot be built here, or None."""
    if not shutil.which("make") or not shutil.which(os.environ.get("CXX", "c++")):
        return "no make or C++ compiler"
    probe = subprocess.run([os.environ.get("CXX", "c++"), "-x", "c++", "-fsyntax-only", "-"],
                           input="#include <fftw3.h>\n#include <zlib.h>\n", text=True, capture_output=True)
    if probe.returncode:
        return "FFTW3 or zlib headers missing"
    return None


def sample_word(i, q):
    return ((i & 0x3FFF) << 16) | (q & 0x3FFF)


def tone_words(cycles, amplitude=4000):
    """A slice of complex tone at `cycles` per slice: a whole number, so slices join seamlessly."""
    words = []
    for n in range(SLICE_SAMPLES):
        phase = 2 * math.pi * cycles * n / SLICE_SAMPLES
        words.append(sample_word(round(amplitude * math.cos(phase)), round(amplitude * math.sin(phase))))
    return words


def two_channel_tone_words(cycles0, cycles1, amplitude=4000):
    """A two-channel slice: beats of four words, channel 0 at two sample times then channel 1 at
    the same two; each channel a tone of its own, in cycles per its 1024 sample times."""
    per = SLICE_SAMPLES // 2

    def tone(cycles, t):
        phase = 2 * math.pi * cycles * t / per
        return sample_word(round(amplitude * math.cos(phase)), round(amplitude * math.sin(phase)))
    words = []
    for t in range(0, per, 2):
        words += [tone(cycles0, t), tone(cycles0, t + 1), tone(cycles1, t), tone(cycles1, t + 1)]
    return words


def pack(words):
    """The unit's 28-bit packing: of each 8 samples, the 8th rides in the others' spare bits."""
    out = []
    for g in range(0, len(words), 8):
        w = words[g:g + 8]
        out.extend(w[k] | ((w[7] << (14 - 2 * k)) & 0xC000C000) for k in range(7))
    return out


class Exporter(threading.Thread):
    """Sends the unit's export datagrams: one slice each, and a firmware event now and then.

    It follows the fake radio through `tuning_file` (or a fixed `tuning`): it sends while the
    radio streams, starts each stream at seq 0, packs when the radio packs, and puts the tone at
    TONE_HZ (silence when that is outside the band)."""

    def __init__(self, port, tuning_file=None, tuning=None, tone_hz=TONE_HZ, slices_per_second=2000,
                 event_every=256):
        super().__init__(daemon=True)
        self.address = ("127.0.0.1", port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tuning_file, self.fixed, self.tone_hz = tuning_file, tuning, tone_hz
        self.rate = slices_per_second
        self.event_every = event_every
        self.payloads = {}
        self.stopping = threading.Event()
        self.seq = 0
        self.slices = 0

    def tuning(self):
        if self.fixed:
            return self.fixed
        try:
            with open(self.tuning_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def payload(self, tuning):
        if tuning.get("channels") == 2:
            # Two channels, each at the sample rate: this tone in channel 0, another in channel 1.
            key = ("two", tuning["center_hz"], tuning["sample_rate"])
            if key not in self.payloads:
                per = SLICE_SAMPLES // 2
                cycles = [round((hz - tuning["center_hz"]) / tuning["sample_rate"] * per) for hz in (self.tone_hz, TONE_1_HZ)]
                self.payloads[key] = struct.pack("<%dI" % SLICE_SAMPLES, *two_channel_tone_words(*cycles))
            return self.payloads[key]
        cycles = round((self.tone_hz - tuning["center_hz"]) / tuning["sample_rate"] * SLICE_SAMPLES)
        if abs(cycles) > 0.4 * SLICE_SAMPLES:
            cycles = None
        key = (cycles, tuning.get("packing") == "half")
        if key not in self.payloads:
            words = tone_words(cycles) if cycles is not None else [0] * SLICE_SAMPLES
            if key[1]:
                words = pack(words)
            self.payloads[key] = struct.pack("<%dI" % len(words), *words)
        return self.payloads[key]

    def send(self, payload):
        self.sock.sendto(struct.pack("<IHHQ", MAGIC, 0, len(payload), self.seq) + payload, self.address)
        self.seq += 1

    def run(self):
        burst, running = max(1, self.rate // 100), False
        while not self.stopping.is_set():
            tuning = self.tuning()
            if not tuning or not tuning.get("running"):
                running = False
                time.sleep(0.01)
                continue
            if not running:
                running, self.seq = True, 0  # a new stream numbers its completions from 0
            for _ in range(burst):
                self.send(self.payload(tuning))
                self.slices += 1
                if self.seq % self.event_every == 0:
                    self.send(bytes(64))
            time.sleep(0.01)

    def stop(self):
        self.stopping.set()
        self.join(2)
        self.sock.close()


class WebSocketClient:
    """Just enough RFC 6455 to play the page: text in and out, binary frames in (acknowledged)."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        request = ("GET /ws HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nOrigin: http://%s:%d\r\n\r\n"
                   % (host, port, key, host, port))
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during the handshake")
            response += chunk
        head, self.buffer = response.split(b"\r\n\r\n", 1)
        if not head.startswith(b"HTTP/1.1 101"):
            raise ConnectionError(head.decode(errors="replace"))
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
        if ("sec-websocket-accept: " + accept).lower() not in head.decode().lower():
            raise ConnectionError("wrong Sec-WebSocket-Accept")
        self.sock.settimeout(None)
        self.send_lock = threading.Lock()
        self.cond = threading.Condition()
        self.status = {}
        self.statuses = 0
        self.hello = None
        self.frames = collections.deque(maxlen=400)
        self.notices = []
        self.closed = None
        threading.Thread(target=self.read_loop, daemon=True).start()

    def send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(head + mask + body)

    def send(self, message):
        self.send_frame(1, json.dumps(message).encode())

    def read_exact(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def read_message(self):
        message, first_opcode = b"", None
        while True:
            b0, b1 = self.read_exact(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self.read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self.read_exact(8))[0]
            payload = self.read_exact(n)
            if opcode >= 8:  # control frames may come between fragments
                return opcode, payload
            if first_opcode is None:
                first_opcode = opcode
            message += payload
            if b0 & 0x80:
                return first_opcode, message

    def read_loop(self):
        try:
            while True:
                opcode, payload = self.read_message()
                if opcode == 1:
                    self.on_text(json.loads(payload))
                elif opcode == 2:
                    self.on_frame(payload)
                elif opcode == 9:
                    self.send_frame(10, payload)
                elif opcode == 8:
                    code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                    with self.cond:
                        self.closed = code
                        self.cond.notify_all()
                    return
        except (ConnectionError, OSError):
            with self.cond:
                self.closed = self.closed or -1
                self.cond.notify_all()

    def on_text(self, m):
        with self.cond:
            if m["t"] == "hello":
                self.hello = m
            elif m["t"] == "status":
                self.status.update({k: v for k, v in m.items() if k != "t"})
                self.statuses += 1
            elif m["t"] == "notice":
                self.notices.append(m)
            self.cond.notify_all()

    def on_frame(self, data):
        kind, flags, bins, sequence, start, stop, base, step = struct.unpack_from("<BBHIddff", data)
        self.send({"t": "ack", "seq": sequence})
        if kind != 1:
            return
        codes = data[48:48 + bins]
        peak = max(range(bins), key=codes.__getitem__)
        frame = {"start": start, "stop": stop, "bins": bins, "peak_hz": start + (peak + 0.5) * (stop - start) / bins,
                 "peak_db": base + codes[peak] * step, "at": time.monotonic(), "channel": (flags >> 1) & 1}
        with self.cond:
            self.frames.append(frame)
            self.cond.notify_all()

    def wait(self, predicate, timeout=10, what="condition"):
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                result = predicate()
                if result:
                    return result
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError("timed out waiting for %s; status: %s; notices: %s"
                                         % (what, json.dumps(self.status)[:1500], self.notices[-5:]))
                self.cond.wait(left)

    def wait_notice(self, fragment, timeout=10):
        return self.wait(lambda: next((n for n in self.notices if fragment in n["text"]), None), timeout,
                         "a notice containing %r" % fragment)

    def wait_frame(self, center, rate, timeout=15):
        """The first frame received from now on that is labelled centre `center`, rate `rate`."""
        since = time.monotonic()

        def labelled():
            for f in reversed(self.frames):
                if f["at"] < since:
                    break
                width = rate / FFT_SIZE
                if abs(f["start"] - (center - rate / 2 - width / 2)) < 1 and abs(f["stop"] - f["start"] - rate) < 1:
                    return f
            return None
        return self.wait(labelled, timeout, "a frame at %.3f MHz, %.0f Msps" % (center / 1e6, rate / 1e6))

    def close(self):
        try:
            self.send_frame(8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()


class ServerFixture:
    count = 0

    def __init__(self, test, controller=True, extra=(), passive_tuning=None, progress_delay=0.3, env=None):
        self.dir = tempfile.mkdtemp(prefix="spectrum-test-")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "controller.jsonl")
        self.tuning_file = os.path.join(self.dir, "tuning.json")
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        self.udp_port = udp.getsockname()[1]
        udp.close()
        ServerFixture.count += 1
        self.stream_name = "/w1700k-sptest-%d-%d" % (os.getpid(), ServerFixture.count)
        self.service = subprocess.Popen([STREAM_BINARY, "--bind", "127.0.0.1", "--port", str(self.udp_port),
                                         "--name", self.stream_name, "--ring-mib", "64", "--workers", "2",
                                         "--rx-threads", "2"], stderr=subprocess.DEVNULL)
        test.addCleanup(self.stop_service)
        deadline = time.monotonic() + 10
        while not stream_shm.ready(self.stream_name):
            if time.monotonic() > deadline or self.service.poll() is not None:
                raise AssertionError("w1700k-stream did not start")
            time.sleep(0.05)
        command = [BINARY, "--listen", "127.0.0.1:0", "--stream", self.stream_name, "--fft-threads", "2",
                   "--dump-dir", self.dir, "--dump-seconds", str(DUMP_SECONDS), *extra]
        if controller:
            command += ["--controller", "exec %s %s --log %s --tuning-file %s --progress-delay %g"
                        % (shlex.quote(sys.executable), shlex.quote(FAKE_CONTROLLER), shlex.quote(self.log),
                           shlex.quote(self.tuning_file), progress_delay)]
        else:
            command.append("--no-controller")
        self.process = subprocess.Popen(command, stderr=subprocess.PIPE, text=True, env=env)
        test.addCleanup(self.stop)
        self.stderr = []
        self.address = None
        ready = threading.Event()

        def drain():
            for line in self.process.stderr:
                self.stderr.append(line.rstrip())
                if line.startswith("serving on http://"):
                    self.address = line.split("http://", 1)[1].split("/", 1)[0]
                    ready.set()
            ready.set()
        threading.Thread(target=drain, daemon=True).start()
        if not ready.wait(20) or not self.address:
            raise AssertionError("the server did not start: %s" % "\n".join(self.stderr))
        host, port = self.address.rsplit(":", 1)
        self.exporter = Exporter(self.udp_port, tuning_file=self.tuning_file if controller else None,
                                 tuning=None if controller else passive_tuning)
        self.exporter.start()
        test.addCleanup(self.exporter.stop)
        self.ws = WebSocketClient(host, int(port))
        test.addCleanup(self.ws.close)
        self.ws.wait(lambda: self.ws.hello and self.ws.statuses, what="hello and status")
        # One FFT bin per output bin over the whole span.
        self.ws.send({"t": "fft", "size": FFT_SIZE, "averaging": 8})
        self.ws.send({"t": "view", "start": 0, "stop": 0, "bins": FFT_SIZE, "auto": False, "fps": 0, "kbps": 0})

    def requests(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]

    def ops(self):
        return [r for r in self.requests() if "op" in r]

    def wait_op(self, predicate, timeout=10, what="a controller request"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [r for r in self.ops() if predicate(r)]
            if found:
                return found[-1]
            time.sleep(0.05)
        raise AssertionError("timed out waiting for %s; got %s" % (what, self.ops()))

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stderr.close()

    def stop_service(self):
        if self.service.poll() is None:
            self.service.send_signal(signal.SIGTERM)
            try:
                self.service.wait(10)
            except subprocess.TimeoutExpired:
                self.service.kill()
                self.service.wait()


@unittest.skipIf(build_problem(), build_problem() or "")
class SpectrumServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for directory in (STREAM, SPECTRUM):
            result = subprocess.run(["make", "-C", directory, "-j4"], capture_output=True, text=True)
            if result.returncode:
                raise AssertionError("build failed:\n" + result.stdout[-3000:] + result.stderr[-3000:])

    def controller(self, ws):
        return ws.status.get("controller") or {}

    def assert_tone(self, frame, tone_hz=TONE_HZ):
        width = (frame["stop"] - frame["start"]) / frame["bins"]
        self.assertLess(abs(frame["peak_hz"] - tone_hz), 2 * width,
                        "tone at %.4f MHz in a frame of %.3f-%.3f MHz, expected at %.4f"
                        % (frame["peak_hz"] / 1e6, frame["start"] / 1e6, frame["stop"] / 1e6, tone_hz / 1e6))

    def assert_no_mislabelled_frame(self, ws):
        """Every frame received so far shows the tone where it is: none carries another tuning."""
        with ws.cond:
            frames = list(ws.frames)
        self.assertTrue(frames)
        for frame in frames:
            self.assert_tone(frame)

    def test_controls_drive_the_controller_and_relabel_the_stream(self):
        s = ServerFixture(self)
        ws = s.ws
        ws.wait(lambda: self.controller(ws).get("state"), what="the controller's state")
        self.assertTrue(ws.hello["controller"])
        self.assertEqual([b["name"] for b in ws.hello["bands"]], ["2.4 GHz", "5 GHz", "6 GHz"])
        self.assertEqual([(r["label"], r["sample_rate"], r["capture_rate"], r["bands"]) for r in ws.hello["rates"]],
                         [("40", 40e6, 40e6, [0, 1, 2]), ("80", 80e6, 80e6, [0, 1, 2]), ("160", 160e6, 160e6, [0, 1, 2]),
                          ("320", 320e6, 320e6, [0, 1, 2]), ("640/2", 320e6, 640e6, [2])])
        ws.wait(lambda: ws.status["receiver"]["lo"] == 5180e6 and ws.status["receiver"]["sample_rate"] == 40e6,
                what="the controller's tuning in the status")

        # Run: the controller picks the packing.
        ws.send({"t": "run", "on": True})
        self.assertNotIn("packing", s.wait_op(lambda r: r["op"] == "start"))
        self.assert_tone(ws.wait_frame(5180e6, 40e6))

        # Centre frequency: a tune mid-stream, then frames labelled with the new centre.
        ws.send({"t": "set", "lo": 5190e6})
        self.assertEqual(s.wait_op(lambda r: r["op"] == "tune")["hz"], 5190000000)
        ws.wait(lambda: ws.status["receiver"]["lo"] == 5190e6, what="the new centre in the status")
        self.assert_tone(ws.wait_frame(5190e6, 40e6))
        ws.send({"t": "set", "lo": 5175e6})
        self.assert_tone(ws.wait_frame(5175e6, 40e6))
        self.assert_no_mislabelled_frame(ws)
        ws.wait(lambda: ws.status["spectrum"]["retuned"] > 0, what="frames dropped at the retunes")

        # Outside the band's LO range: refused here, never sent.
        ws.send({"t": "set", "lo": 1000e6})
        ws.wait_notice("outside the LO range")
        self.assertFalse([r for r in s.ops() if r["op"] == "tune" and r["hz"] == 1000000000])

        # Gain and filter.
        ws.send({"t": "set", "agc": False, "lna": 3, "lpf": 7})
        op = s.wait_op(lambda r: r["op"] == "gain" and "lna" in r)
        self.assertEqual((op["lna"], op["lpf"]), (3, 7))
        ws.wait(lambda: self.controller(ws)["state"]["rf"]["lna"] == 3, what="the gain in the state")
        ws.send({"t": "set", "agc": True})
        s.wait_op(lambda r: r["op"] == "gain" and r.get("agc") is True)
        ws.send({"t": "set", "filter": 80})
        s.wait_op(lambda r: r["op"] == "filter" and r["mhz"] == 80)
        ws.send({"t": "set", "filter": 320})                # any filter at any width
        s.wait_op(lambda r: r["op"] == "filter" and r["mhz"] == 320)
        ws.wait(lambda: self.controller(ws)["state"]["rf"].get("filter_follows_rate") is False,
                what="a filter set by hand no longer follows the rate")
        ws.send({"t": "set", "filter_follow": True})        # every band's filter at its sample rate again
        s.wait_op(lambda r: r["op"] == "filter_follow" and r["on"] is True)
        ws.wait(lambda: self.controller(ws)["state"]["rf"].get("filter_follows_rate") is True,
                what="the filters following the rate")
        ws.send({"t": "set", "filter_follow": "yes"})
        ws.wait_notice("filter_follow must be true or false")

        # A rate the radio does not have is refused before the controller sees it.
        ws.send({"t": "set", "band": 2, "sample_rate": 640e6})
        ws.wait_notice("sample_rate must be 40, 80, 160 or 320 Msps")
        ws.send({"t": "set", "band": 1, "sample_rate": 320e6, "capture_rate": 640e6})
        ws.wait_notice("the 5 GHz band has no 640/2")

        # Band and sample rate: a configure with progress, then a new rate at the same LO. The fake
        # radio stops and restarts the stream inside it, numbering from 0 again.
        ws.send({"t": "set", "band": 1, "sample_rate": 80e6})
        ws.wait(lambda: self.controller(ws).get("busy") == "configuring"
                and (self.controller(ws).get("progress") or {}).get("text"), what="configure progress")
        ws.send({"t": "set", "lo": 5200e6})
        ws.wait_notice("busy (configuring)")
        op = s.wait_op(lambda r: r["op"] == "configure")
        self.assertEqual((op["band"], op["sample_rate"], op["capture_rate"]), (1, 80000000, 80000000))
        self.assertNotIn("channel", op)
        ws.wait(lambda: not self.controller(ws).get("busy") and ws.status["receiver"]["sample_rate"] == 80e6,
                timeout=15, what="the configure to finish")
        self.assertEqual(ws.status["receiver"]["lo"], 5175e6, "the LO stays across a sample-rate change")
        self.assert_tone(ws.wait_frame(5175e6, 80e6))

        # 320 Msps: one configure; the controller restarts the stream packed half.
        ws.send({"t": "set", "band": 1, "sample_rate": 320e6})
        ws.wait(lambda: not self.controller(ws).get("busy") and ws.status["receiver"]["sample_rate"] == 320e6
                and self.controller(ws)["state"]["stream"]["running"], timeout=15, what="the 320 Msps configure")
        self.assertEqual([r["op"] for r in s.ops()][-1], "configure")
        self.assertEqual(self.controller(ws)["state"]["stream"]["packing"], "half")
        self.assert_tone(ws.wait_frame(5175e6, 320e6, timeout=20))
        self.assert_no_mislabelled_frame(ws)

        # Firmware events take slots without counting as lost slices.
        ws.wait(lambda: ws.status["stream"]["events"] > 0 and ws.status["stream"]["packed"] > 0,
                what="events and packed slices")
        self.assertEqual(ws.status["stream"]["lost"], 0)
        self.assertEqual(ws.status["stream"]["bad"], 0)

        # SIGUSR1: a snapshot labelled with the current tuning. It is the newest DUMP_SECONDS of the
        # stream, and its tuning is steady only if every slice of it came after the change took
        # effect. The frame labelled with the new tuning, above, did: once the stream has moved on
        # by a snapshot's length from there (twice that here), the snapshot has too. At the fake
        # exporter's 2000 slices a second that is a second and a half, which a busy machine does
        # not always let pass by itself.
        span = 2 * (int(DUMP_SECONDS * 320e6 / SLICE_SAMPLES) + DUMP_BEHIND)
        seen = ws.status["stream"]["slices"]
        ws.wait(lambda: ws.status["stream"]["slices"] >= seen + span, timeout=30,
                what="the stream to move a snapshot's length past the retune")
        s.process.send_signal(signal.SIGUSR1)
        deadline = time.monotonic() + 10
        while not glob.glob(os.path.join(s.dir, "w1700k-dump-*.json")) and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.2)
        [meta_path] = glob.glob(os.path.join(s.dir, "w1700k-dump-*.json"))
        with open(meta_path) as f:
            meta = json.load(f)
        self.assertEqual((meta["center_hz"], meta["sample_rate"]), (5175e6, 320e6))
        self.assertTrue(meta["tuning_steady"])
        self.assertGreater(meta["event_slices"], 0)
        self.assertEqual(meta["samples"], SLICE_SAMPLES * (meta["slices"] - meta["event_slices"]))
        self.assertEqual(os.path.getsize(meta_path[:-5] + ".bin"), 4 * meta["samples"])

        # Stop.
        stops = sum(r["op"] == "stop" for r in s.ops())
        ws.send({"t": "run", "on": False})
        s.wait_op(lambda r: sum(o["op"] == "stop" for o in s.ops()) > stops, what="a stop")
        ws.wait(lambda: not self.controller(ws)["state"]["stream"]["running"], what="the stream to stop")

    def test_a_two_channel_stream_is_started_from_the_page_and_either_channel_is_shown(self):
        """The run message asks for two channels; the controller's state then says the stream has
        them, the stream service is told (here by the server, as it would be again after a
        restart), and the frames are of one channel: channel 0 until the page asks for 1."""
        s = ServerFixture(self)
        ws = s.ws
        ws.wait(lambda: self.controller(ws).get("state"), what="the controller's state")
        self.assertEqual(ws.status["receiver"]["channels"], 1)
        ws.send({"t": "run", "on": True, "channels": 2})
        self.assertEqual(s.wait_op(lambda r: r["op"] == "start").get("channels"), 2)
        ws.wait(lambda: ws.status["receiver"]["channels"] == 2, what="two channels in the status")
        shm = stream_shm.Shm(s.stream_name)
        self.addCleanup(shm.close)
        deadline = time.monotonic() + 10
        while shm.layout()[2] != 2:
            self.assertLess(time.monotonic(), deadline, "the stream service was not told of two channels")
            time.sleep(0.05)

        def shows(tone_hz):
            def newest():
                with ws.cond:
                    frame = ws.frames[-1] if ws.frames else None
                if not frame:
                    return None
                width = (frame["stop"] - frame["start"]) / frame["bins"]
                wide = abs(frame["stop"] - frame["start"] - 40e6) < 1       # a channel's rate, not twice it
                return frame if wide and abs(frame["peak_hz"] - tone_hz) < 2 * width else None
            return newest
        self.assertEqual(ws.status["fft"]["channel"], 0)
        self.assertEqual(ws.wait(shows(TONE_HZ), 15, "frames of channel 0, with its tone")["channel"], 0)
        ws.send({"t": "fft", "channel": 1})
        ws.wait(lambda: ws.status["fft"]["channel"] == 1, what="channel 1 in the status")
        self.assertEqual(ws.wait(shows(TONE_1_HZ), 15, "frames of channel 1, with its tone")["channel"], 1,
                         "every frame says which channel it is of")
        # Both at once: a frame of each channel over the same samples, channel 0's then channel 1's.
        ws.send({"t": "fft", "channel": 2})
        ws.wait(lambda: ws.status["fft"]["channel"] == 2, what="both channels in the status")
        since = time.monotonic() + 0.5

        def pairs():
            with ws.cond:
                frames = [f for f in ws.frames if f["at"] > since]
            return frames if len(frames) >= 40 else None
        frames = ws.wait(pairs, 20, "frames of both channels")
        first = next(i for i, f in enumerate(frames) if f["channel"] == 0)
        got = frames[first:first + 30]
        self.assertEqual([f["channel"] for f in got], [0, 1] * 15, "in pairs, channel 0's frame first")
        for f in got:
            width = (f["stop"] - f["start"]) / f["bins"]
            self.assertLess(abs(f["peak_hz"] - (TONE_HZ, TONE_1_HZ)[f["channel"]]), 2 * width, "each with its own tone")
        ws.send({"t": "fft", "channel": 0})
        ws.wait(lambda: ws.status["fft"]["channel"] == 0, what="channel 0 alone again")
        since = time.monotonic() + 0.5

        def only_zero():
            with ws.cond:
                frames = [f for f in ws.frames if f["at"] > since]
            return frames if len(frames) >= 15 else None
        self.assertEqual({f["channel"] for f in ws.wait(only_zero, 20, "frames of channel 0 alone")}, {0})
        ws.send({"t": "fft", "channel": 3})
        ws.wait_notice("channel must be 0, 1 or 2")
        ws.send({"t": "run", "on": False})
        s.wait_op(lambda r: r["op"] == "stop")
        ws.wait(lambda: ws.status["receiver"]["channels"] == 1, what="one channel again in the status")
        ws.send({"t": "run", "on": True, "channels": 3})
        ws.wait_notice("channels must be 1 or 2")

    def test_controller_death_is_reported_and_recovered(self):
        s = ServerFixture(self)
        ws = s.ws
        ws.wait(lambda: self.controller(ws).get("state"), what="the controller's state")
        first = self.controller(ws)["pid"]
        # The controller publishes its state by itself, which can come before the server's first
        # request has reached it: it is not killed until that request is on record.
        s.wait_op(lambda r: r["op"] == "status", what="the first controller's status request")
        os.kill(s.requests()[0]["pid"], signal.SIGKILL)
        ws.wait(lambda: not self.controller(ws)["alive"], what="the controller's death in the status")
        ws.send({"t": "set", "lo": 5200e6})
        c = self.controller(ws)
        self.assertEqual(c["restarts"], 1)
        self.assertIn("was killed by signal 9", c["error"])
        ws.wait_notice("restarting in")
        ws.wait_notice("the controller is not running")
        ws.wait(lambda: self.controller(ws)["alive"] and self.controller(ws).get("state")
                and self.controller(ws)["pid"] != first, timeout=15, what="the controller to come back")
        self.assertEqual(len([r for r in s.requests() if "pid" in r]), 2)
        # Each controller is asked for its state first.
        s.wait_op(lambda r: len(s.ops()) == 2, what="the second controller's status request")
        self.assertEqual([r["op"] for r in s.ops()], ["status", "status"])

    def test_passive_viewer(self):
        s = ServerFixture(self, controller=False, extra=["--center", "2440", "--rate", "40"],
                          passive_tuning={"center_hz": 2440e6, "sample_rate": 40e6, "running": True})
        ws = s.ws
        self.assertFalse(ws.hello["controller"])
        self.assertEqual(ws.status["controller"], {"mode": "off"})
        s.exporter.tone_hz = 2445e6
        self.assert_tone(ws.wait_frame(2440e6, 40e6), 2445e6)
        ws.send({"t": "set", "lo": 2450e6})
        ws.send({"t": "run", "on": False})
        ws.wait(lambda: sum("--no-controller" in n["text"] for n in ws.notices) == 2, what="two refusals")


@unittest.skipIf(build_problem(), build_problem())
class WhoIsAnswered(unittest.TestCase):
    """The page drives a receiver: a request is answered when it was meant for this server (its
    Host is a name the server knows itself by), a socket when its page is this server's, and
    beyond this machine both only with the server's token. And a reader that does not read gets
    no more than a little held for it."""
    TOKEN = "a-token-of-the-tests-own"

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["make", "-C", SPECTRUM], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("build failed:\n" + result.stdout + result.stderr)

    def serve(self, *options, listen="127.0.0.1:0", token=None):
        """A passive server (no stream, no controller); its port, and what it said as it started."""
        env = dict(os.environ)
        env.pop("W1700K_SPECTRUM_TOKEN", None)
        if token:
            env["W1700K_SPECTRUM_TOKEN"] = token
        self.process = subprocess.Popen([BINARY, "--listen", listen, "--stream", "/w1700k-sptest-none-%d" % os.getpid(),
                                         "--no-controller", *options], stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self.process.stderr.close)
        self.addCleanup(self.process.wait, 10)
        self.addCleanup(self.process.terminate)
        # what it says as it starts: where it serves, the names it answers to, and beyond this
        # machine (or with a token of the user's) the link to open, with a line on the token
        beyond = listen != "127.0.0.1:0"
        said = []
        for _ in range(2 + (1 if token or beyond else 0) + (1 if beyond and "--no-token" in options else 0)):
            line = self.process.stderr.readline()
            if not line:
                raise AssertionError("the server did not start: %s" % said)
            said.append(line.rstrip())
        self.port = int(said[0].split("http://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
        return said

    def ask(self, target="/", host=None, headers=(), method="GET", upgrade=False):
        """One request on a connection of its own: (status code, {header: value}, body)."""
        lines = ["%s %s HTTP/1.1" % (method, target), "Host: %s" % (host or "127.0.0.1:%d" % self.port), *headers]
        if upgrade:
            lines += ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Version: 13",
                      "Sec-WebSocket-Key: " + base64.b64encode(os.urandom(16)).decode()]
        else:
            lines += ["Connection: close"]
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as link:
            link.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                piece = link.recv(65536)
                if not piece:
                    break
                data += piece
            head, _, body = data.partition(b"\r\n\r\n")
            fields = dict(line.split(": ", 1) for line in head.decode().split("\r\n")[1:] if ": " in line)
            want = int(fields.get("Content-Length", 0)) if not upgrade else 0
            while len(body) < want:
                piece = link.recv(65536)
                if not piece:
                    break
                body += piece
        return int(head.split()[1]), fields, body

    def test_a_request_is_answered_under_the_servers_own_names_and_no_other(self):
        said = self.serve()
        here = "127.0.0.1:%d" % self.port
        self.assertIn("127.0.0.1", said[1])
        self.assertIn("localhost", said[1], "it says which names it answers to")
        for host in (here, "localhost:%d" % self.port, "LOCALHOST:%d" % self.port, "[::1]:%d" % self.port,
                     "%s:%d" % (socket.gethostname(), self.port)):
            with self.subTest(host):
                self.assertEqual(self.ask(host=host)[0], 200)
                self.assertEqual(self.ask("/ws", host=host, headers=["Origin: http://" + host], upgrade=True)[0], 101)
        # a page on another site, whose name was made to resolve here: its Host and Origin agree
        for host in ("attacker.example:%d" % self.port, "attacker.example", "127.0.0.1:%d" % (self.port + 1), "127.0.0.1",
                     "", "127.0.0.1:%d@attacker.example" % self.port):
            with self.subTest(host):
                status, _, body = self.ask(host=host or " ")
                self.assertEqual(status, 421)
                self.assertIn(b"--host NAME", body)
                self.assertNotIn(socket.gethostname().encode(), body, "which names it has is not told to whoever asks")
                self.assertEqual(self.ask("/ws", host=host or " ", headers=["Origin: http://" + host], upgrade=True)[0], 421)

    def test_a_socket_is_opened_for_the_servers_own_page_and_for_no_other_sites(self):
        self.serve()
        here = "127.0.0.1:%d" % self.port
        for origin, status in (("http://" + here, 101), ("http://localhost:%d" % self.port, 101), (None, 101),
                               ("http://attacker.example:%d" % self.port, 403), ("https://" + here + ".attacker.example", 403),
                               ("http://127.0.0.1", 403), ("null", 403)):
            with self.subTest(origin):
                headers = ["Origin: " + origin] if origin else []
                self.assertEqual(self.ask("/ws", headers=headers, upgrade=True)[0], status)
        # what a proxy says it was asked for is not taken on its word, unless one is there
        self.assertEqual(self.ask("/ws", headers=["Origin: http://front.example", "X-Forwarded-Host: front.example"],
                                  upgrade=True)[0], 403)

    def test_names_and_origins_it_is_told_of(self):
        self.serve("--host", "Viewer.Example", "--allow-origin", "https://front.example/")
        self.assertEqual(self.ask(host="viewer.example:%d" % self.port)[0], 200)
        self.assertEqual(self.ask(host="other.example:%d" % self.port)[0], 421)
        self.assertEqual(self.ask("/ws", headers=["Origin: https://front.example"], upgrade=True)[0], 101)
        self.assertEqual(self.ask("/ws", headers=["Origin: https://front.example:8443"], upgrade=True)[0], 403)

    def test_behind_a_proxy_the_name_is_the_one_the_proxy_was_asked_for(self):
        self.serve("--behind-proxy", "--host", "sdr.example")
        forwarded = ["X-Forwarded-Host: sdr.example"]
        self.assertEqual(self.ask(host="127.0.0.1:%d" % self.port, headers=forwarded)[0], 200)
        self.assertEqual(self.ask("/ws", headers=forwarded + ["Origin: https://sdr.example"], upgrade=True)[0], 101)
        self.assertEqual(self.ask(headers=["X-Forwarded-Host: attacker.example"])[0], 421)
        self.assertEqual(self.ask("/ws", headers=forwarded + ["Origin: https://attacker.example"], upgrade=True)[0], 403)

    def test_with_a_token_nothing_is_answered_without_it(self):
        said = self.serve(token=self.TOKEN)
        self.assertEqual(said[-1].split()[:2], ["open", "http://127.0.0.1:%d/?token=%s" % (self.port, self.TOKEN)])
        cookie = "Cookie: other=1; w1700k-%d=%s" % (self.port, self.TOKEN)
        for target in ("/", "/app.js", "/ws"):
            with self.subTest(target):
                status, _, body = self.ask(target, upgrade=target == "/ws")
                self.assertEqual(status, 401)
                self.assertIn(b"?token=", body)
                self.assertNotIn(self.TOKEN.encode(), body)
                self.assertEqual(self.ask(target, headers=["Cookie: w1700k-%d=%s" % (self.port, "x" * len(self.TOKEN))],
                                          upgrade=target == "/ws")[0], 401)
                self.assertEqual(self.ask(target + "?token=" + self.TOKEN[:-1], upgrade=target == "/ws")[0], 401)
                self.assertEqual(self.ask(target, headers=[cookie], upgrade=target == "/ws")[0], 101 if target == "/ws" else 200)
        # the link: the token leaves the address for a cookie that only this server's pages send
        status, fields, _ = self.ask("/?token=" + self.TOKEN)
        self.assertEqual((status, fields["Location"]), (303, "/"))
        self.assertEqual(fields["Set-Cookie"], "w1700k-%d=%s; Path=/; HttpOnly; SameSite=Strict" % (self.port, self.TOKEN))
        self.assertEqual(self.ask("//attacker.example/?token=" + self.TOKEN)[1]["Location"], "/", "and leads nowhere else")
        # a client that is not a browser
        self.assertEqual(self.ask("/ws?token=" + self.TOKEN, upgrade=True)[0], 101)
        self.assertEqual(self.ask("/app.js", headers=["Authorization: Bearer " + self.TOKEN])[0], 200)
        self.assertEqual(self.ask("/app.js", host="attacker.example:%d" % self.port, headers=[cookie])[0], 421,
                         "the token does not make up for the name")

    def test_on_this_machine_alone_no_token_is_wanted_and_beyond_it_one_is_made(self):
        said = self.serve()
        self.assertFalse(any(line.startswith("open ") for line in said))
        self.assertEqual(self.ask()[0], 200)
        self.process.terminate()
        self.process.wait(10)
        said = self.serve(listen="0.0.0.0:0")
        link = said[-1].split()[1]
        token = link.split("?token=")[1]
        self.assertRegex(token, "^[0-9a-f]{32}$")
        self.assertNotIn("127.0.0.1", link.split("?")[0], "a link that can be opened from another machine")
        self.assertEqual(self.ask()[0], 401)
        self.assertEqual(self.ask("/app.js?token=" + token)[0], 303)
        self.assertEqual(self.ask("/app.js", headers=["Cookie: w1700k-%d=%s" % (self.port, token)])[0], 200)
        self.process.terminate()
        self.process.wait(10)
        said = self.serve("--no-token", listen="0.0.0.0:0")
        self.assertIn("NO TOKEN IS WANTED", said[-1])
        self.assertEqual(self.ask()[0], 200)

    def test_a_token_that_is_too_little_is_refused(self):
        for token in ("short", "sixteen or more, with spaces"):
            result = subprocess.run([BINARY, "--no-controller"], env=dict(os.environ, W1700K_SPECTRUM_TOKEN=token),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("W1700K_SPECTRUM_TOKEN has to be 16 or more", result.stderr)

    def rss_mib(self):
        with open("/proc/%d/status" % self.process.pid) as status:
            return next(int(line.split()[1]) for line in status if line.startswith("VmRSS:")) / 1024

    def test_a_reader_that_does_not_read_is_not_answered_without_end(self):
        self.serve()
        before = self.rss_mib()
        request = ("GET /app.js HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.port).encode()
        link = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(link.close)
        link.settimeout(2)
        try:
            for _ in range(80):                      # some 4 MB of requests: 65 KiB asked for by each
                link.sendall(request * 1000)
        except OSError:
            pass                                     # the server stopped listening to it: as it should
        time.sleep(0.5)
        self.assertLess(self.rss_mib() - before, 64, "what is held for it is bounded (this was a gigabyte)")
        self.assertEqual(self.ask()[0], 200, "and everybody else is served")
        deadline = time.monotonic() + 15
        link.settimeout(1)
        while time.monotonic() < deadline:           # and it is let go of, read or not
            try:
                if not link.recv(1 << 20):
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        else:
            self.fail("the connection was kept")

    def test_more_connections_than_a_few_pages_make_are_not_kept(self):
        self.serve()
        held = []
        self.addCleanup(lambda: [link.close() for link in held])
        for _ in range(90):
            held.append(socket.create_connection(("127.0.0.1", self.port), timeout=5))
        time.sleep(0.5)
        dropped = 0
        for link in held:
            link.setblocking(False)
            try:
                dropped += link.recv(1) == b""
            except BlockingIOError:
                pass
            except OSError:
                dropped += 1
        self.assertGreaterEqual(dropped, 90 - 64)
        self.assertLess(dropped, 90 - 32, "those within the number are kept")
        for link in held:
            link.close()
        held.clear()
        time.sleep(0.3)
        self.assertEqual(self.ask()[0], 200)

    def test_files_served_from_a_directory_are_that_directorys(self):
        base = tempfile.mkdtemp(prefix="spectrum-web-")
        self.addCleanup(shutil.rmtree, base, True)
        os.mkdir(os.path.join(base, "web"))
        with open(os.path.join(base, "web", "index.html"), "w") as page:
            page.write("the page\n")
        with open(os.path.join(base, "web-notes.txt"), "w") as beside:
            beside.write("beside the directory, its name beginning with the directory's\n")
        self.serve("--web", os.path.join(base, "web"))
        self.assertEqual(self.ask("/index.html")[2], b"the page\n")
        self.assertEqual(self.ask("-notes.txt")[0], 404, "a target with no leading slash named the file beside it")


@unittest.skipIf(build_problem(), build_problem())
class PlansMeasuredApart(unittest.TestCase):
    """FFTW's measured plan of an FFT size is found by a process of its own and kept, so the
    server waits for none: where measuring takes minutes (arm64, where FFTW times by the clock)
    a server asked to stop did not stop until its measurement ended, 106 s on one host."""

    MEASURING = 300     # seconds a host is given to measure the smallest size; 20 to 30 where this was found

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["make", "-C", SPECTRUM], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("build failed:\n" + result.stdout + result.stderr)

    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        self.kept = os.path.join(self.cache, "w1700k", "fftwf-wisdom")
        self.env = dict(os.environ, XDG_CACHE_HOME=self.cache)

    def someone_measures(self, size):
        """As while another process measures the plan of `size` (512 is 2^9): it holds the
        byte of the lock file that says so, and that size's own."""
        os.makedirs(os.path.dirname(self.kept), exist_ok=True)
        held = open(self.kept + ".lock", "w")
        self.addCleanup(held.close)
        for byte in (0, size.bit_length() - 1):
            fcntl.lockf(held, fcntl.LOCK_EX, 1, byte)
        return held

    def measure(self, size):
        process = subprocess.Popen([BINARY, "--measure-plan", str(size)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, env=self.env)

        def end():
            if process.poll() is None:
                process.kill()
            process.wait(10)
            process.stderr.close()
        self.addCleanup(end)
        return process

    def test_a_server_stops_at_once_while_its_plan_is_being_measured(self):
        self.someone_measures(262144)       # the size a server starts at: the process it starts has nothing to do
        server = subprocess.Popen([BINARY, "--listen", "127.0.0.1:0", "--stream", "/w1700k-sptest-none-%d" % os.getpid(),
                                   "--no-controller"], stderr=subprocess.PIPE, text=True, env=self.env)

        def end():
            if server.poll() is None:
                server.kill()
            server.wait(10)
            server.stderr.close()
        self.addCleanup(end)
        self.assertIn("http://", server.stderr.readline())
        time.sleep(1)
        self.assertFalse(os.path.exists(self.kept), "nothing was measured: the server runs on the estimate")
        asked = time.monotonic()
        server.terminate()
        self.assertEqual(server.wait(10), 0)
        self.assertLess(time.monotonic() - asked, 5)

    def test_a_running_server_moves_to_the_measured_plan_when_it_is_kept(self):
        """The whole of it, with a stream running: the server shows frames on the estimate
        while the process it started for its FFT size waits behind another's measurement, that
        process measures and keeps the plan when its turn comes, and the server, which was not
        restarted, says it runs on the measured plan and goes on showing the tone where it is."""
        # The size a server starts at is in another process's hands, which is measuring: the one
        # the server starts for it has nothing to do, and the one for the tests' size waits.
        other = self.someone_measures(262144)
        fixture = ServerFixture(self, controller=False, extra=["--center", "2440", "--rate", "40"],
                                passive_tuning={"center_hz": 2440e6, "sample_rate": 40e6, "running": True}, env=self.env)
        ws = fixture.ws
        fixture.exporter.tone_hz = 2445e6

        def tone(frame):
            width = (frame["stop"] - frame["start"]) / frame["bins"]
            self.assertLess(abs(frame["peak_hz"] - 2445e6), 2 * width)

        tone(ws.wait_frame(2440e6, 40e6))
        self.assertEqual(ws.status["spectrum"]["plan"], "estimated")
        self.assertFalse(os.path.exists(self.kept))
        other.close()   # the other measurement ends: the turn of the server's own
        ws.wait(lambda: ws.status["spectrum"]["plan"] == "measured", timeout=self.MEASURING,
                what="the server on its measured plan")
        self.assertGreater(os.path.getsize(self.kept), 0)
        tone(ws.wait_frame(2440e6, 40e6))
        self.assertEqual(fixture.process.poll(), None)

    def test_a_plan_is_measured_once_and_kept(self):
        first = self.measure(512)
        self.assertEqual(first.wait(self.MEASURING), 0, first.stderr.read())
        self.assertGreater(os.path.getsize(self.kept), 0)
        written = os.stat(self.kept).st_mtime_ns
        started = time.monotonic()
        self.assertEqual(self.measure(512).wait(10), 0)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(os.stat(self.kept).st_mtime_ns, written, "nothing left to measure, nothing written")

    def test_a_second_process_for_a_size_has_nothing_to_do_and_one_for_another_waits(self):
        self.someone_measures(512)
        started = time.monotonic()
        self.assertEqual(self.measure(512).wait(10), 0)
        self.assertLess(time.monotonic() - started, 5)
        other = self.measure(1024)
        with self.assertRaises(subprocess.TimeoutExpired):
            other.wait(1.5)
        self.assertFalse(os.path.exists(self.kept))

    def test_a_size_that_is_not_an_fft_size_is_refused(self):
        refused = self.measure(500)
        self.assertNotEqual(refused.wait(10), 0)
        self.assertIn("power of two", refused.stderr.read())
        self.assertFalse(os.path.exists(self.kept))


if __name__ == "__main__":
    unittest.main()
