#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measure the receiver against a HackRF test tone (tools/rfbench/hrftone).

    bench.py level [--tx-gain DB]          set the tone's level: one tone at 2450 MHz, read back
    bench.py ghosts --tx-gain DB           the tone seen on frequency and 160 / 320 MHz away
    bench.py sweep --tx-gain DB            the tone stepped across the 2.4 GHz band's whole reach

Needs a loaded radio with a stream running, w1700k-stream serving it, and nothing else driving the
radio (stop w1700k-spectrum). The receiver is put on fixed gain and the stream's roll-off
correction is switched off; a tone is on only while a measurement runs, amplifier off.

The HackRF and its antenna are not calibrated: levels are relative. Ratios taken at one tone
frequency (a ghost against the tone itself) do not depend on the path between the two radios.
"""
import argparse
import json
import mmap
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'host'))

from w1700k import config as config_mod          # noqa: E402
from w1700k.controller import Controller         # noqa: E402

SHM = '/dev/shm/w1700k-stream'
SLICE = 2048
FULL_SCALE = 8192.0
FLATTEN_AT = 504                                  # w1700k_stream_header.flatten
N = 1 << 17                                       # samples per measurement: 0.8 ms at 160 Msps


class Tone:
    """hrftone as a child: on while this object lives."""

    def __init__(self, hz, gain):
        self.p = subprocess.Popen([str(HERE / 'hrftone'), str(int(hz)), str(gain)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self._answer()

    def _answer(self):
        line = self.p.stdout.readline().strip()
        if not line.startswith('on'):
            raise RuntimeError(f'hrftone: {line or "no answer"}')

    def tune(self, hz):
        self.p.stdin.write(f'f {int(hz)}\n')
        self.p.stdin.flush()
        self._answer()

    def gain(self, db):
        self.p.stdin.write(f'g {int(db)}\n')
        self.p.stdin.flush()
        self._answer()

    def close(self):
        if self.p.poll() is None:
            try:
                self.p.stdin.write('q\n')
                self.p.stdin.flush()
                self.p.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                self.p.kill()


class Ring:
    def __init__(self):
        f = open(SHM, 'r+b')
        self.m = mmap.mmap(f.fileno(), f.seek(0, 2))
        self.ring, self.tags, self.samples = struct.unpack_from('<3Q', self.m, 16)
        struct.pack_into('<Q', self.m, FLATTEN_AT, 0)     # the samples as received

    def grab(self, count=N):
        """The newest `count` samples, complex64 on the 14-bit scale."""
        slices = count // SLICE
        for _ in range(40):
            _, run, newest = struct.unpack_from('<3Q', self.m, 128)
            first = newest - slices - 8
            out = np.empty(count, dtype=np.complex64)
            ok = first > 0
            for k in range(slices if ok else 0):
                i = first + k
                slot = i & (self.ring - 1)
                tag = struct.unpack_from('<Q', self.m, self.tags + 8 * slot)[0]
                if tag != ((run << 40) | (i + 1)):
                    ok = False
                    break
                a = np.frombuffer(self.m, dtype='<i2', count=2 * SLICE, offset=self.samples + slot * SLICE * 4)
                out[k * SLICE:(k + 1) * SLICE] = a[0::2].astype(np.float32) + 1j * a[1::2].astype(np.float32)
                if struct.unpack_from('<Q', self.m, self.tags + 8 * slot)[0] != tag:
                    ok = False
                    break
            if ok:
                return out
            time.sleep(0.05)
        raise RuntimeError('no clean run of slices in the stream ring')


def spectrum(x, rate):
    """(offset Hz per bin, dBFS per bin: a full-scale tone reads 0)."""
    w = np.hanning(len(x)).astype(np.float32)
    amplitude = np.abs(np.fft.fftshift(np.fft.fft(x * w))) / w.sum() / FULL_SCALE
    f = (np.arange(len(x)) - len(x) // 2) * rate / len(x)
    return f, 20 * np.log10(amplitude + 1e-12)


def peak(f, db, near, within=300e3):
    k = np.abs(f - near) <= within
    i = int(np.argmax(np.where(k, db, -999)))
    return float(f[i]), float(db[i])


def floor(db):
    return float(np.median(db))


class Bench:
    def __init__(self, lna, lpf):
        self.ctl = Controller(config_mod.load(None))
        self.ctl.attach()
        state = self.ctl.state()
        if not state['radio']['loaded'] or not state['stream']['running']:
            raise SystemExit('load the radio and start a stream first')
        self.band = state['radio']['band']
        self.rate = state['stream']['sample_rate']
        self.lo_range = state['rf']['lo_range_hz']
        self.ctl.gain(False, lna, lpf)
        self.ring = Ring()

    def tune(self, hz):
        self.ctl.tune(hz)
        time.sleep(0.25)                          # the retune, and the slices already on their way
        return self.ctl.state()['rf']['lo_hz']

    def close(self):
        self.ctl.gain(True)
        self.ctl.close()


def level(bench, args):
    tone = Tone(2450e6, args.tx_gain)
    try:
        lo = bench.tune(2440e6)
        for gain in range(args.tx_gain, 31, 6):
            tone.gain(gain)
            time.sleep(0.3)
            f, db = spectrum(bench.ring.grab(), bench.rate)
            at, value = peak(f, db, 2450e6 - lo)
            print(json.dumps({'tx_gain': gain, 'tone_mhz': round((lo + at) / 1e6, 3), 'tone_dbfs': round(value, 1),
                              'floor_dbfs': round(floor(db), 1)}), flush=True)
            if value > -22:
                break
    finally:
        tone.close()


def ghosts(bench, args):
    """For each tone: its level on frequency, then what shows with the receiver moved by each offset."""
    tones = [mhz * 1e6 for mhz in args.tones]
    tone = Tone(tones[0], args.tx_gain)
    try:
        for hz in tones:
            tone.tune(hz)
            lo = bench.tune(hz - 10e6)
            f, db = spectrum(bench.ring.grab(args.samples), bench.rate)
            _, reference = peak(f, db, hz - lo)
            row = {'tone_mhz': hz / 1e6, 'rate_msps': bench.rate / 1e6, 'filter_mhz': bench.ctl.state()['rf']['filter_mhz'],
                   'on_frequency_dbfs': round(reference, 1)}
            for shift in (-320e6, -160e6, -80e6, 80e6, 160e6, 320e6):
                want = hz - 10e6 + shift
                if not bench.lo_range[0] + 1e6 <= want <= bench.lo_range[1] - 1e6:
                    continue
                lo = bench.tune(want)
                f, db = spectrum(bench.ring.grab(args.samples), bench.rate)
                expected = hz - lo + shift         # where a response folded by that shift sits: the tone's offset again
                at, value = peak(f, db, expected, 500e3)
                quiet = np.abs(f) > 300e3          # the strongest line anywhere but at DC, whatever it is
                i = int(np.argmax(np.where(quiet, db, -999)))
                row[f'lo{shift / 1e6:+.0f}'] = {
                    'at_expected_dbc': round(value - reference, 1), 'floor_dbc': round(floor(db) - reference, 1),
                    'strongest_dbc': round(float(db[i]) - reference, 1), 'strongest_at_mhz': round(float(f[i]) / 1e6, 3)}
            print(json.dumps(row), flush=True)
    finally:
        tone.close()


def filters(bench, args):
    """Does the analog filter act on the ghost? One tone, the receiver on it and 320 MHz either side,
    at each filter width: a ghost that folds in after the filter falls as the filter narrows; one
    that the mixer itself makes (a second LO component) arrives inside the filter and does not."""
    hz, offset = 2435e6, 5e6
    tone = Tone(hz, args.tx_gain)
    try:
        for width in (320, 160, 80, 40, 20):
            bench.ctl.filter(width)
            row = {'filter_mhz': width}
            for name, shift in (('on_frequency', 0), ('lo-320', -320e6), ('lo+320', 320e6), ('lo-160', -160e6), ('lo+160', 160e6)):
                lo = bench.tune(hz - offset + shift)
                f, db = spectrum(bench.ring.grab(args.samples), bench.rate)
                _, value = peak(f, db, hz - lo + shift, 500e3)
                row[name] = round(value, 1)
            for name in ('lo-320', 'lo+320', 'lo-160', 'lo+160'):
                row[name + '_dbc'] = round(row[name] - row['on_frequency'], 1)
            print(json.dumps(row), flush=True)
    finally:
        tone.close()
        bench.ctl.filter_follow(True)              # as a loaded receiver has them


def sweep(bench, args):
    lo_min, lo_max = bench.lo_range[0] + 2e6, bench.lo_range[1] - 2e6
    tone = Tone(args.start * 1e6, args.tx_gain)
    try:
        for mhz in np.arange(args.start, args.stop + 0.1, args.step):
            hz = mhz * 1e6
            tone.tune(hz)
            lo = bench.tune(min(max(hz - 10e6, lo_min), lo_max))
            f, db = spectrum(bench.ring.grab(), bench.rate)
            at, value = peak(f, db, hz - lo)
            print(json.dumps({'tone_mhz': float(mhz), 'offset_mhz': round((hz - lo) / 1e6, 1), 'tone_dbfs': round(value, 1),
                              'floor_dbfs': round(floor(db), 1)}), flush=True)
    finally:
        tone.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('what', choices=('level', 'ghosts', 'filters', 'sweep'))
    ap.add_argument('--tx-gain', type=int, default=0, help='HackRF TX VGA, dB (0..30 here)')
    ap.add_argument('--lna', type=int, default=2)
    ap.add_argument('--lpf', type=int, default=5)
    ap.add_argument('--start', type=float, default=2060)
    ap.add_argument('--stop', type=float, default=2790)
    ap.add_argument('--step', type=float, default=10)
    ap.add_argument('--tones', type=float, nargs='+', default=[2420, 2435, 2450, 2465, 2480], help='MHz, for ghosts')
    ap.add_argument('--samples', type=int, default=1 << 20, help='per ghost measurement (a power of two)')
    args = ap.parse_args()
    if not 0 <= args.tx_gain <= 30:
        raise SystemExit('--tx-gain 0..30')
    bench = Bench(args.lna, args.lpf)
    try:
        {'level': level, 'ghosts': ghosts, 'filters': filters, 'sweep': sweep}[args.what](bench, args)
    finally:
        bench.close()


if __name__ == '__main__':
    main()
