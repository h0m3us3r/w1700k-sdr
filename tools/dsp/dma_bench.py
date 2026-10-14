#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Time bounded repeated MEM_DMA transfers to DSP0, with full content checks.

Run once after a fresh load.py so WM scratch has no stale instruction cache.
The helper polls descriptor completion and never copies payload words.
"""
import argparse
import json
from pathlib import Path
from common import read, write, packed, unpack, healthy, emit, ready
from w1700k import config
from w1700k.unit import Unit
from w1700k.wm import Wm

FIELDS = 'completed sum minimum maximum total tx rx irq error'.split()


def helper(w, source, dest, size, reps):
    before = w.traps()
    result = w.call(0x00910000, source, dest, size, reps, timeout=10)
    healthy(w)
    if w.traps() != before:
        raise RuntimeError('WM DMA helper trapped')
    stats = dict(zip(FIELDS, read(w, 0x00910a00, len(FIELDS))))
    emit(event='dma_cycles', source=hex(source), dest=hex(dest), bytes=size, repeats=reps,
         return_code=result, stats=stats)
    if result or stats['error'] or stats['completed'] != reps or stats['irq']:
        raise RuntimeError(f'DMA did not complete cleanly: {stats}')
    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--helper', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    code = args.helper.read_bytes()
    if not 0 < len(code) <= 2048:
        p.error('helper must fit its scratch reservation')
    code += bytes(-len(code)%4)
    results = []
    with Wm(Unit(config.load(args.config).unit)) as w:
        ready(w)
        write(w, 0x00910000, unpack(code))
        if packed(read(w, 0x00910000, len(code)//4)) != code:
            raise RuntimeError('helper readback mismatch')
        try:
            for size in (64, 256, 1024, 4096, 8192):
                for seed in (0x13579bdf, 0xcafebabe):
                    pattern = [(seed ^ (i*0x9e3779b9)) & 0xffffffff for i in range(size//4)]
                    write(w, 0x00912000, pattern)
                    write(w, 0x8620cffc, [0x5a5a5a5a] + [0xa5a5a5a5]*(size//4) + [0x96969696])
                    stats = helper(w, 0x00912000, 0x8620d000, size, 128)
                    got = read(w, 0x8620d000, size//4)
                    guards = [read(w, 0x8620cffc, 1)[0], read(w, 0x8620d000+size, 1)[0]]
                    if got != pattern or guards != [0x5a5a5a5a, 0x96969696]:
                        raise RuntimeError('DMA payload or guards differ')
                    record = {'bytes': size, 'repeats': 128, 'seed': seed, 'stats': stats,
                              'mean_transfer_us': stats['sum']/128/720,
                              'payload_MBps': size*128*720/stats['sum'],
                              'including_control_MBps': size*128*720/stats['total'],
                              'wm_clock_assumed_hz': 720000000, 'content_and_guards_passed': True}
                    results.append(record); emit(event='dma_benchmark', **record)
        finally:
            args.out.write_text(json.dumps(results, indent=2)+'\n')
        emit(event='benchmark_done', helper_left_loaded=True, queues_stopped=True)


if __name__ == '__main__':
    main()
