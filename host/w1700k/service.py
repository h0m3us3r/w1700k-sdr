# SPDX-License-Identifier: MIT
"""The host's stream service (host/stream, w1700k-stream) as the controller needs it: one control
word in its shared memory, which says how many channels the stream that is about to start has
(host/stream/include/w1700k_stream.h, radio_channels). The service lays each slot out by channel
from the first slice of the run on, so it is told before the radio is.
"""
import mmap
import os
import struct
from pathlib import Path

MAGIC, VERSION = 0x4D525453, 2
HEADER_BYTES = 4096
RADIO_CHANNELS_AT = 536           # offsetof(struct w1700k_stream_header, radio_channels)


def announce_channels(channels, name, directory='/dev/shm'):
    """Tell the service whose shared memory is `name` (as it is named there: "/w1700k-stream") that
    the next stream has `channels` channels. True when it was told; False when this host has no
    such service of this user's (none running, an older one, or the stream goes to another host):
    then whoever reads the stream has to be told another way."""
    if channels not in (1, 2):
        raise ValueError('channels: 1 or 2')
    path = Path(directory) / name.lstrip('/')
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        # Only this user's own object, writable by nobody else, as the service makes it (and as
        # w1700k_stream_open asks of it): a word is not written into a stranger's memory.
        if st.st_uid != os.geteuid() or st.st_mode & 0o022 or st.st_size < HEADER_BYTES:
            return False
        with mmap.mmap(fd, HEADER_BYTES) as header:
            if struct.unpack_from('<2I', header, 0) != (MAGIC, VERSION):
                return False
            struct.pack_into('<Q', header, RADIO_CHANNELS_AT, channels)
        return True
    finally:
        os.close(fd)
