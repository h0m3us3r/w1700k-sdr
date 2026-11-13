# SPDX-License-Identifier: MIT
"""Test helper: w1700k-stream's shared memory (host/stream/include/w1700k_stream.h) from Python."""
import mmap
import os
import struct

SLICE = 2048
TAG_EVENT, TAG_LOST, TAG_FLAT = 1 << 63, 1 << 62, 1 << 61
TAG_KIND = TAG_EVENT | TAG_LOST | TAG_FLAT

# Field offsets in struct w1700k_stream_header (all u64 but the first four u32).
FIELDS = {
    "ring_slices": 16, "tags_offset": 24, "samples_offset": 32, "total_bytes": 40, "service_pid": 48,
    "heartbeat_ns": 56, "position_seq": 128, "run": 136, "newest": 144, "received": 168,
    "datagrams": 256, "bytes": 264, "slices": 272, "packed": 280, "events": 288, "lost": 296, "pieces": 304,
    "bad": 312, "late": 320, "before_start": 328, "duplicates": 336, "socket_drops": 344, "spare_words": 352,
    "stale_slices": 360, "stale_lines": 368, "flattened": 376, "saturated": 384, "overruns": 392, "reduced": 400,
    "restart_requests": 496, "flatten": 504, "radio_sample_rate": 512, "radio_capture_rate": 520,
    "radio_filter_hz": 528, "flatten_active": 624, "flatten_rate": 632,
}


def ready(name):
    """The service has published its object and receives."""
    try:
        with open("/dev/shm/" + name.lstrip("/"), "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return len(head) == 16 and struct.unpack("<4I", head)[:2] == (0x4D525453, 1)


def tag(run, index):
    return (run << 40) | (index + 1)


class Shm:
    def __init__(self, name):
        path = "/dev/shm/" + name.lstrip("/")
        with open(path, "r+b") as f:
            self.map = mmap.mmap(f.fileno(), 0)
        magic, version, _, slice_samples = struct.unpack_from("<4I", self.map, 0)
        assert (magic, version, slice_samples) == (0x4D525453, 1, SLICE), "not a w1700k-stream object"

    def close(self):
        self.map.close()

    def get(self, field):
        return struct.unpack_from("<Q", self.map, FIELDS[field])[0]

    def set(self, field, value):
        struct.pack_into("<Q", self.map, FIELDS[field], value)

    def position(self):
        while True:
            s0 = self.get("position_seq")
            run, newest = self.get("run"), self.get("newest")
            if not s0 & 1 and self.get("position_seq") == s0:
                return run, newest

    def tag(self, index):
        slot = index & (self.get("ring_slices") - 1)
        return struct.unpack_from("<Q", self.map, self.get("tags_offset") + 8 * slot)[0]

    def samples(self, index):
        """The slot's samples as [(i, q), ...]."""
        slot = index & (self.get("ring_slices") - 1)
        flat = struct.unpack_from("<%dh" % (2 * SLICE), self.map, self.get("samples_offset") + slot * SLICE * 4)
        return list(zip(flat[0::2], flat[1::2]))
