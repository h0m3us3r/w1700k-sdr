# SPDX-License-Identifier: MIT
"""Host access shared by the DMA setup and validation tools."""
import json
import struct
from w1700k import rf
from w1700k.wm import MAGIC, STATE_RUNNING


def emit(**data):
    print(json.dumps(data), flush=True)


def healthy(w):
    status = w.status()
    if status['magic'] != MAGIC or status['state'] != STATE_RUNNING:
        raise RuntimeError('WM is not running; reload normal firmware')
    return status


def read(w, address, count):
    out = []
    for offset in range(0, count, 128):
        healthy(w)
        out += w.run([rf.r32(address+4*i) for i in range(offset, min(count, offset+128))])
    return out


def write(w, address, words):
    for offset in range(0, len(words), 128):
        healthy(w)
        w.write([(address+4*i, words[i]) for i in range(offset, min(len(words), offset+128))])


def unpack(data):
    return list(struct.unpack('<'+'I'*(len(data)//4), data))


def packed(words):
    return struct.pack('<'+'I'*len(words), *words)


def ready(w):
    healthy(w)
    if w.session.streaming():
        raise RuntimeError('stop viewer and stream first')
    # Check reset/enable before any DTCM access: reset-held reads can hang the bus.
    if read(w, 0x81020010, 2) != [7, 0]:
        raise RuntimeError('load the DMA test container first')
    box = read(w, 0x86200000, 11)
    if box[0] != 0x44535031 or box[10] != 1:
        raise RuntimeError('DMA test DSP is not ready')
