#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""How the two channels of a two-channel stream stand to each other: gain, phase and delay.

The two channels are two receive chains sampled at the same instants (`w1700k start --channels
2`). Chain by chain the analog paths differ a little: a gain, a phase (the synthesizer's and the
paths'), and a delay of a few nanoseconds. Given a signal that reaches both chains, this measures
channel 1 against channel 0:

    channel 1 (t) = gain x e^(j phase) x channel 0 (t - delay)      (over the signal's band)

from the cross-spectrum of the two: the delay is the slope of its phase over frequency, the phase
is what is left at the LO, the gain the ratio of the common part. What it finds is of the unit AND
of how the signal reached the two chains: from one source split by cable into both connectors it
is the unit's own; from an antenna it includes the path through the air to each.

    two_channel.py measure [--seconds S] [--out FILE]     one measurement of the running stream
    two_channel.py watch [--every S] [--count N]          one line a measurement, until stopped
    two_channel.py state                                  which of the two phase states, by the noise
    two_channel.py align [--learn] [--follow]             bring the channels to the reference state

The phase between the channels lands in one of two states half a turn apart whenever the chains'
LOs start again (a retune that locks the synthesizer again; `w1700k tune` keeps the lock where it
can). `state` and `align` tell the two apart with no signal, by the noise the two chains share
(class Noise), against a reference kept for each LO (~/.config/w1700k/phase-reference.json): the
first `align` at an LO learns the state it finds as that LO's reference, and every later one
brings the channels back to it, starting channel 1's LO again (`w1700k rephase`) until the state
is the reference's. So a phase measured between the channels at an LO holds whenever `align` has
run there since the last retune. `align --follow` stays and does it after every retune it sees.

A two-channel stream must be running and w1700k-stream serving it on this host. Needs numpy.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FULL_SCALE = 8192.0                 # the radio's 14-bit samples (w1700k_stream.h)
STREAM_NAME = '/w1700k-stream'
SLICE = 2048                        # complex samples to a slot; a two-channel slot holds half of each


class NoReference(Exception):
    """Nothing in the samples is common to both channels: there is nothing to measure them by."""


def estimate(a, b, sample_rate, segment=4096, band=None, min_coherence=0.5, max_delay_ns=20.0):
    """Channel `b` against channel `a` (complex samples of the same instants, at `sample_rate`).

    band: (low, high) Hz from the LO, the part of the spectrum to go by; None: all of it.
    max_delay_ns: the delay is looked for within this much either way. Two chains of one radio
    differ by nanoseconds, and the limit matters: a signal of a few lines cannot tell a delay from
    a delay one period of their spacing further (lines 6 MHz apart: 167 ns), nor from half a turn
    of phase with half that delay, so a wide search can report a phase flip as a delay.
    Returns a dict: gain_db, phase_deg (at the LO, the delay taken out), delay_ns and
    delay_samples (positive: `b` is the later), coherence (0..1, over what was used),
    bandwidth_hz (how much of the spectrum was used), gain_spread_db (the distance between the
    gain's two estimates, which noise in either channel pulls apart: it is known to half of that),
    level_dbfs and dc (each channel's), segments. Raises NoReference where the channels have
    nothing in common above `min_coherence`."""
    a = np.asarray(a, dtype=np.complex128)
    b = np.asarray(b, dtype=np.complex128)
    if len(a) != len(b) or len(a) < 256:
        raise ValueError('two channels of the same length, at least 256 samples')
    dc = [complex(a.mean()), complex(b.mean())]
    a = a - dc[0]
    b = b - dc[1]
    n = min(segment, 1 << int(math.log2(len(a))))
    hop = n // 2
    starts = range(0, len(a) - n + 1, hop)
    window = np.hanning(n)
    fa = np.array([np.fft.fft(window * a[i:i + n]) for i in starts])
    fb = np.array([np.fft.fft(window * b[i:i + n]) for i in starts])
    # A segment with somebody else's burst in it (Wi-Fi beside a steady reference) is left out: its
    # floor is far above the quiet segments', and the burst has a phase between the chains of its own.
    floor = np.median(np.abs(fa) ** 2, axis=1) + np.median(np.abs(fb) ** 2, axis=1)
    quiet = floor <= 4.0 * np.percentile(floor, 25)
    fa, fb = fa[quiet], fb[quiet]
    sxx = np.sum(np.abs(fa) ** 2, axis=0)
    syy = np.sum(np.abs(fb) ** 2, axis=0)
    sxy = np.sum(np.conj(fa) * fb, axis=0)                 # phase: -2 pi f delay + phase, for b the later
    f = np.fft.fftfreq(n) * sample_rate
    with np.errstate(divide='ignore', invalid='ignore'):
        coherence = np.where((sxx > 0) & (syy > 0), np.abs(sxy) / np.sqrt(sxx * syy), 0.0)
    use = coherence >= min_coherence
    use &= np.abs(f) > 1.5 * sample_rate / n               # not the LO's own line
    if band is not None:
        use &= (f >= band[0]) & (f <= band[1])
    if len(fa) < 2 or not use.any():
        raise NoReference('the channels have nothing in common here: no signal reaches both, or it is too weak')
    f_used, s_used = f[use], sxy[use]

    def score(delays):                                     # delays in seconds
        return np.abs(np.exp(2j * np.pi * np.outer(delays, f_used)) @ s_used)

    period = 1.0 / sample_rate
    limit = max_delay_ns * 1e-9
    coarse = np.arange(-limit, limit + period / 8, period / 4)
    best = coarse[int(np.argmax(score(coarse)))]
    for step in (0.02, 0.001):                             # of a sample
        fine = np.clip(best + np.arange(-12, 13) * step * period, -limit, limit)
        best = fine[int(np.argmax(score(fine)))]
    rotated = np.sum(s_used * np.exp(2j * np.pi * f_used * best))
    h1 = abs(rotated) / np.sum(sxx[use])                   # low when channel a is noisy
    h2 = np.sum(syy[use]) / abs(rotated)                   # high when channel b is noisy
    level = lambda x: 10 * math.log10(max(float(np.mean(np.abs(x) ** 2)), 1e-12) / FULL_SCALE ** 2)
    return {
        'gain_db': 10 * math.log10(h1 * h2),
        'gain_spread_db': 20 * math.log10(h2 / h1),         # between the two estimates, as amplitudes
        'phase_deg': math.degrees(math.atan2(rotated.imag, rotated.real)),
        'delay_ns': best * 1e9,
        'delay_samples': best * sample_rate,
        'coherence': float(abs(rotated) / math.sqrt(np.sum(sxx[use]) * np.sum(syy[use]))),
        'bandwidth_hz': float(use.sum() * sample_rate / n),
        'level_dbfs': [level(a), level(b)],
        'dc': [[dc[0].real, dc[0].imag], [dc[1].real, dc[1].imag]],
        'segments': int(len(fa)),
    }


def apply(b, sample_rate, gain_db, phase_deg, delay_ns):
    """Channel 1 brought to channel 0: the gain, phase and delay of a measurement taken out of it
    (the delay as a phase slope over the block, so the block's ends are approximate)."""
    b = np.asarray(b, dtype=np.complex128)
    f = np.fft.fftfreq(len(b)) * sample_rate
    spectrum = np.fft.fft(b) * np.exp(2j * np.pi * f * delay_ns * 1e-9)
    return np.fft.ifft(spectrum) * 10 ** (-gain_db / 20) * np.exp(-1j * math.radians(phase_deg))


# --- which of the two states: the channels' own noise ---

NOISE_BAND_HZ = 8e6               # a reading goes by this much either side of the LO, whatever the sample rate
# How much quiet stream a reading takes at least (segments left out as bursts do not count, so in a
# busy channel it takes several times this long). With the bursts out, readings this short held
# to 2 degrees inside a busy Wi-Fi channel (2442 MHz).
NOISE_SECONDS = 0.05
NOISE_SIGMA = 12.0                # and is taken once the correlation stands this many times over chance
# A segment is quiet up to this many times the floor, and the floor is the lowest twentieth of every
# segment the reading has seen (not of one block: a block can lie wholly inside somebody's frame,
# and then nothing in it stands out; judged block by block, 2 aligns in 14 came out wrong at 2442
# MHz).
QUIET = 1.25
FLOOR_PERCENTILE = 5
FLOOR_BLOCKS = 4                  # blocks looked at for the floor before any is added
# How many times align starts channel 1 again before it gives up. A rephase changes the state about
# every other time on one unit's radios and one time in five on another's 5 and 6 GHz radios (3 of
# 16): at one in five, 64 tries fail once in a million.
REPHASE_TRIES = 64
BUSY_QUIET = 0.15                 # under this share of quiet segments a reading is said to be of a busy channel
LEARN_STEADY_DEG = 25.0           # two readings this close are a reference
STATE_TOLERANCE_DEG = 50.0        # how far from the reference, or from its opposite, a reading still names a state
REFERENCES = Path('~/.config/w1700k/phase-reference.json')


class Undetermined(Exception):
    """The noise does not say which state the channels are in (too little of it shared here, or
    a phase that is neither the reference's nor its opposite)."""


class Retuned(Undetermined):
    """The stream was retuned, or locked again, while its state was being read: the reading is of
    no one tuning."""


class Noise:
    """The two channels' own noise against each other, with no signal needed: part of what each
    chain receives with nothing transmitting is common to both (it comes in at the antennas;
    measured 0.02 to 0.7 of the floor, by frequency), and the phase of that common part between
    the channels takes two values half a turn apart, one for each state the chains' LOs can be
    in after a new lock. Blocks of both channels are added until result() is sure enough.
    Bursts are left out (segments above the floor of all the reading has seen: somebody's frame
    has a phase between the antennas of its own), and so are bins above the floor."""

    def __init__(self, sample_rate, segment=4096, band_hz=NOISE_BAND_HZ):
        f = np.fft.fftfreq(segment) * sample_rate
        self.segment, self.window = segment, np.hanning(segment)
        self.bins = np.flatnonzero((np.abs(f) > 0.3e6) & (np.abs(f) <= min(band_hz, 0.4 * sample_rate)))
        self.sxy = np.zeros(len(self.bins), complex)
        self.sxx, self.syy = np.zeros(len(self.bins)), np.zeros(len(self.bins))
        self.segments = self.seen = 0
        self.powers = [[], []]            # every segment's power, each channel: the floor is theirs
        self.floor, self.floor_at = (0.0, 0.0), 0

    def _cut(self, a, b):
        n = len(a) // self.segment
        a = (a[:n * self.segment] - np.mean(a)).reshape(n, self.segment)
        b = (b[:n * self.segment] - np.mean(b)).reshape(n, self.segment)
        pa, pb = np.mean(np.abs(a) ** 2, axis=1), np.mean(np.abs(b) ** 2, axis=1)
        self.powers[0].extend(pa)
        self.powers[1].extend(pb)
        return a, b, pa, pb

    def look(self, a, b):
        """A block taken for the floor only: where the quiet level is, before anything is added."""
        self._cut(a, b)

    def _quiet(self, pa, pb):
        if len(self.powers[0]) - self.floor_at >= 128 or not self.floor_at:
            self.floor = tuple(float(np.percentile(p, FLOOR_PERCENTILE)) for p in self.powers)
            self.floor_at = len(self.powers[0])
        return (pa <= QUIET * self.floor[0]) & (pb <= QUIET * self.floor[1])

    def _raw(self, raw):
        """read_raw's slots as segments (float32: segments, 2 channels, segment, 2) and each
        segment's power less its DC, per channel: cheap, so that only quiet segments cost an FFT."""
        slots = self.segment // raw.shape[2]
        n = raw.shape[0] // slots
        x = raw[:n * slots].astype(np.float32).reshape(n, slots, 2, raw.shape[2], 2).transpose(0, 2, 1, 3, 4)
        x = x.reshape(n, 2, self.segment, 2)
        mean = x.mean(axis=2, keepdims=True)
        power = np.einsum('nckq,nckq->nc', x, x) / self.segment - np.einsum('ncq,ncq->nc', mean[:, :, 0], mean[:, :, 0])
        self.powers[0].extend(power[:, 0])
        self.powers[1].extend(power[:, 1])
        return x, mean, power

    def look_raw(self, raw):
        self._raw(raw)

    def add_raw(self, raw):
        """add() for a read_raw block."""
        x, mean, power = self._raw(raw)
        quiet = self._quiet(power[:, 0], power[:, 1])
        z = x[quiet] - mean[quiet]
        z = (z[..., 0] + 1j * z[..., 1]) * self.window
        self._sum(np.fft.fft(z[:, 0], axis=1)[:, self.bins], np.fft.fft(z[:, 1], axis=1)[:, self.bins], len(x))

    def add(self, a, b):
        a, b, pa, pb = self._cut(a, b)
        quiet = self._quiet(pa, pb)
        self._sum(np.fft.fft(a[quiet] * self.window, axis=1)[:, self.bins],
                  np.fft.fft(b[quiet] * self.window, axis=1)[:, self.bins], len(a))

    def _sum(self, fa, fb, n):
        self.sxy += np.sum(np.conj(fa) * fb, axis=0)
        self.sxx += np.sum(np.abs(fa) ** 2, axis=0)
        self.syy += np.sum(np.abs(fb) ** 2, axis=0)
        self.segments += len(fa)
        self.seen += n

    def result(self):
        """correlation (0..1) and phase_deg of channel 1 against channel 0 over the floor, chance
        (the correlation unrelated noise would show over as much), and how much went in."""
        if not self.segments:
            return {'correlation': 0.0, 'phase_deg': 0.0, 'chance': 1.0, 'segments': 0, 'quiet': 0.0}

        floor = (self.sxx < 3 * np.median(self.sxx)) & (self.syy < 3 * np.median(self.syy))
        c = np.sum(self.sxy[floor]) / math.sqrt(np.sum(self.sxx[floor]) * np.sum(self.syy[floor]))
        # (the window makes neighbouring bins half the same: 1.5 bins to an independent one)
        chance = math.sqrt(1.5 / (self.segments * int(floor.sum())))
        return {'correlation': float(abs(c)), 'phase_deg': math.degrees(math.atan2(c.imag, c.real)), 'chance': chance,
                'segments': self.segments, 'quiet': self.segments / self.seen}


def read_noise(shm, sample_rate, sigma=NOISE_SIGMA, min_seconds=NOISE_SECONDS, max_seconds=8.0, slices=512):
    """A Noise reading of the running stream: blocks added until the correlation stands `sigma`
    times over chance (and at least min_seconds of samples are in), or max_seconds of samples
    have not brought it there. The result says which: `determined`."""
    noise, samples, looked = Noise(sample_rate), 0, 0
    while True:
        raw = read_raw(shm, slices)
        if raw is None:
            raise NoReference('no two-channel stream is arriving at w1700k-stream on this host')
        if looked < FLOOR_BLOCKS:                          # spread over some tens of milliseconds
            noise.look_raw(raw)
            looked += 1
            time.sleep(0.01)
            continue
        noise.add_raw(raw)
        samples += raw.shape[0] * raw.shape[2]
        r = noise.result()
        # (as many quiet samples as min_seconds holds: in a busy channel that takes longer)
        enough = r['segments'] * noise.segment >= min_seconds * sample_rate
        sure = r['correlation'] >= sigma * r['chance']
        if (sure and enough) or samples >= max_seconds * sample_rate:
            return dict(r, determined=bool(sure and enough), seconds=samples / sample_rate)


def state_of(phase_deg, reference_deg, tolerance=STATE_TOLERANCE_DEG):
    """0 when the reading is the reference's state, 1 when it is the other one (half a turn from
    it), None when it is neither to within `tolerance`."""
    off = (phase_deg - reference_deg + 180.0) % 360.0 - 180.0
    if abs(off) <= tolerance:
        return 0
    return 1 if 180.0 - abs(off) <= tolerance else None


def align(read, rephase, reference_deg, tries=REPHASE_TRIES, tolerance=STATE_TOLERANCE_DEG):
    """Bring the channels to the reference's state: read() (a read_noise result), and while it is
    the other state, rephase() (which lands channel 1 in either state at random) and read again.
    The last reading, with `rephased` (how many times) and `off_deg` (from the reference).
    Undetermined when a reading cannot say, twice running, or the state has not come in `tries`."""
    rephased = unsure = rounds = 0
    while True:
        rounds += 1
        if rounds > 4 * tries:
            raise Undetermined('the readings do not agree with each other from one to the next')
        r = read()
        state = state_of(r['phase_deg'], reference_deg, tolerance) if r['determined'] else None
        if state is None:
            unsure += 1
            if unsure < 2:
                continue
            if not r['determined']:
                raise Undetermined(f'too little of the noise is common to the channels here (correlation '
                                   f'{r["correlation"]:.4f}, {r["correlation"] / r["chance"]:.1f} times chance after '
                                   f'{r["seconds"]:.1f} s)')
            raise Undetermined(f'the noise reads {r["phase_deg"]:+.0f} degrees, neither the reference '
                               f'{reference_deg:+.0f} nor its opposite: the reference no longer fits (learn it again)')
        unsure = 0
        if state == 0:
            # said twice running before it is believed: a weak frame of somebody's inside one
            # reading can turn it (1 reading in 12 at 5220 MHz, a Wi-Fi channel)
            again = read()
            if not again['determined'] or state_of(again['phase_deg'], reference_deg, tolerance) != 0:
                continue
            off = (again['phase_deg'] - reference_deg + 180.0) % 360.0 - 180.0
            return dict(again, rephased=rephased, off_deg=off)
        if rephased >= tries:
            raise Undetermined(f'the reference state did not come in {tries} tries')
        rephase()
        rephased += 1


def load_references(path=REFERENCES):
    """{(band, lo_hz): record} from the reference file; empty when there is none."""
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except FileNotFoundError:
        return {}
    return {(r['band'], r['lo_hz']): r for r in data['references']}


def save_references(references, path=REFERENCES):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [references[key] for key in sorted(references)]
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({'version': 1, 'references': rows}, indent=1) + '\n')
    tmp.replace(path)


def read_raw(shm, slices=64, wait=5.0):
    """`slices` consecutive slots of the running two-channel run as they lie in the ring: int16,
    shaped (slices, 2 channels, samples, 2: I and Q). None when the stream gave none in `wait`
    seconds (not running, one channel, or losing)."""
    import stream_shm
    mask, base = shm.get('ring_slices') - 1, shm.get('samples_offset')
    per = SLICE // 2
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        run, newest, channels = shm.layout()
        first = newest - 8 - slices
        if not run or channels != 2 or first < 0:
            time.sleep(0.05)
            continue
        tags = [shm.tag(i) for i in range(first, first + slices)]
        if any(t != stream_shm.tag(run, i) and t != (stream_shm.tag(run, i) | stream_shm.TAG_FLAT)
               for i, t in zip(range(first, first + slices), tags)):
            time.sleep(0.02)                               # a loss or an event in it: the next stretch
            continue
        start, wrap = first & mask, (first & mask) + slices - (mask + 1)
        if wrap <= 0:
            raw = np.frombuffer(shm.map, dtype='<i2', count=2 * SLICE * slices, offset=base + start * 8192).copy()
        else:
            raw = np.concatenate([np.frombuffer(shm.map, dtype='<i2', count=2 * SLICE * (slices - wrap), offset=base + start * 8192),
                                  np.frombuffer(shm.map, dtype='<i2', count=2 * SLICE * wrap, offset=base)])
        if [shm.tag(i) for i in range(first, first + slices)] != tags:
            continue                                       # overwritten while it was copied
        return raw.reshape(slices, 2, per, 2)
    return None


def read_block(shm, slices=64, wait=5.0):
    """The same as (channel 0, channel 1), complex."""
    raw = read_raw(shm, slices, wait)
    if raw is None:
        return None
    z = raw[..., 0].astype(np.float64) + 1j * raw[..., 1]
    return z[:, 0, :].reshape(-1), z[:, 1, :].reshape(-1)


def measure(shm, sample_rate, seconds=1.0, slices=64, **options):
    """One measurement over about `seconds` of the stream: blocks of `slices` slots, read as they
    come and measured as one."""
    blocks, until = [], time.monotonic() + seconds
    while not blocks or time.monotonic() < until:
        block = read_block(shm, slices)
        if block is None:
            if blocks:
                break
            raise NoReference('no two-channel stream is arriving at w1700k-stream on this host')
        blocks.append(block)
        if len(blocks) >= 64:
            break
    result = estimate(np.concatenate([b[0] for b in blocks]), np.concatenate([b[1] for b in blocks]), sample_rate,
                      **options)
    result['samples'] = int(sum(len(b[0]) for b in blocks))
    return result


REPHASE_SETTLE = 0.1              # s after a rephase or a retune before a reading (the samples across it are no good)


def align_here(controller, shm, references, path, learn=False, out=print, seconds=NOISE_SECONDS):
    """What `align` does at the LO the stream is on now: learn the state found as its reference
    (none yet, or `learn`), or bring the channels to the reference's. The reading it ended on."""
    state = controller.state()
    band, lo, rate = state['radio']['band'], state['rf']['lo_hz'], state['radio']['sample_rate']
    where = f'band {band} at {lo / 1e6:.3f} MHz'
    locks = [state['rf']['relocks']]                       # as they stand with our own rephases counted in

    def read():
        """A reading that was of this tuning from its first sample to its last: anyone may retune
        the stream meanwhile (another session, the spectrum page), and a reading across that
        belongs to neither LO."""
        time.sleep(REPHASE_SETTLE)
        r = read_noise(shm, rate, min_seconds=seconds, max_seconds=max(8.0, 4 * seconds))
        controller.poll()
        now = controller.state()
        if (now['radio']['band'], now['rf']['lo_hz'], now['rf']['relocks']) != (band, lo, locks[0]):
            raise Retuned(f'{where}: retuned or locked again while it was being read; nothing decided')
        return r

    def rephase():
        controller.rephase(1)
        locks[0] = controller.state()['rf']['relocks']

    reference = references.get((band, lo))
    if reference is None or learn:
        first, r = read(), read()
        if not (first['determined'] and r['determined']):
            raise Undetermined(f'{where}: too little of the noise is common to the channels to go by (correlation '
                               f'{r["correlation"]:.4f}, {r["correlation"] / r["chance"]:.1f} times chance after '
                               f'{r["seconds"]:.1f} s)')
        if state_of(r['phase_deg'], first['phase_deg'], LEARN_STEADY_DEG) != 0:
            raise Undetermined(f'{where}: the noise does not hold one phase here ({first["phase_deg"]:+.0f}, then '
                               f'{r["phase_deg"]:+.0f} degrees): no reference learned')
        references[(band, lo)] = {'band': band, 'lo_hz': lo, 'phase_deg': round(r['phase_deg'], 1),
                                  'correlation': round(r['correlation'], 4), 'when': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
        save_references(references, path)
        out(f'{where}: learned as the reference (noise phase {r["phase_deg"]:+.0f} degrees, correlation '
            f'{r["correlation"]:.3f}): align returns the channels to this state from now on{busy(r)}')
        return r
    try:
        r = align(read, rephase, reference['phase_deg'])
    except Retuned:
        raise
    except Undetermined as error:
        raise Undetermined(f'{where}: {error}') from None
    did = (f'channel 1 rephased {r["rephased"]} time{"" if r["rephased"] == 1 else "s"}' if r['rephased']
           else 'as it was')
    out(f'{where}: in the reference state, {did} (noise phase {r["off_deg"]:+.0f} degrees from the reference, '
        f'correlation {r["correlation"]:.3f}){busy(r)}')
    return r


def busy(r):
    """A word of caution for a reading taken where little of the stream was quiet: in a channel
    somebody fills (seen: 9 % quiet, a correlation of 0.89, at 5180 MHz beside an access point) what
    the reading goes by may be their signal, whose phase between the antennas is not the unit's."""
    if r.get('quiet', 1.0) >= BUSY_QUIET:
        return ''
    return f'; only {100 * r["quiet"]:.0f} % of the stream was quiet: a busy channel, where this is less sure'


def follow(controller, shm, references, path, every=0.5, learn=False, out=print, rounds=None, seconds=NOISE_SECONDS):
    """`align --follow`: align_here after every change it sees in what the phase depends on (the
    band, the LO, the count of locks), for as long as a two-channel stream runs or comes back."""
    seen = None
    while rounds is None or rounds > 0:
        if rounds is not None:
            rounds -= 1
        controller.poll()
        state = controller.state()
        if state['stream']['running'] and state['stream'].get('channels') == 2:
            now = (state['radio']['band'], state['rf']['lo_hz'], state['rf']['relocks'])
            if now != seen:
                try:
                    align_here(controller, shm, references, path, learn, out, seconds)
                except Retuned:
                    seen = None                            # the next round takes the new tuning
                    continue
                except (Undetermined, NoReference) as error:
                    out(f'not aligned: {error}')
                learn = False
                state = controller.state()                 # our own rephases are counted too
                seen = (state['radio']['band'], state['rf']['lo_hz'], state['rf']['relocks'])
        else:
            seen = None
        time.sleep(every)


def main():
    sys.path[:0] = [str(ROOT / 'host'), str(ROOT / 'tests')]
    import stream_shm
    from w1700k import config as host_config
    from w1700k.controller import Controller

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('measure', 'watch', 'state', 'align'):
        p = sub.add_parser(name)
        p.add_argument('--stream', default=STREAM_NAME)
    for name in ('measure', 'watch'):
        p = sub.choices[name]
        p.add_argument('--seconds', type=float, default=1.0, help='of the stream to a measurement (default: %(default)s)')
        p.add_argument('--band', nargs=2, type=float, metavar=('LOW_MHZ', 'HIGH_MHZ'),
                       help='go by this part of the spectrum only, MHz from the LO')
    sub.choices['measure'].add_argument('--out', help='write the record here as JSON too')
    sub.choices['watch'].add_argument('--every', type=float, default=2.0)
    sub.choices['watch'].add_argument('--count', type=int, default=0, help='stop after this many (default: never)')
    for name in ('state', 'align'):
        sub.choices[name].add_argument('--reference', default=str(REFERENCES), help='the reference file (default: %(default)s)')
        sub.choices[name].add_argument('--seconds', type=float, default=NOISE_SECONDS,
                                       help='of the stream to a reading at least (default: %(default)s)')
    sub.choices['align'].add_argument('--learn', action='store_true',
                                      help='take the state found now as this LO\'s reference, in place of the one kept')
    sub.choices['align'].add_argument('--follow', action='store_true',
                                      help='stay, and align after every retune (learning each new LO), until stopped')
    args = parser.parse_args()

    if not stream_shm.ready(args.stream):
        sys.exit('two_channel: w1700k-stream is not running on this host')
    shm = stream_shm.Shm(args.stream)
    controller = Controller(host_config.load(), emit=lambda event: None)
    controller.attach()
    state = controller.state()
    if args.command != 'align':
        controller.close()
    if not state['stream']['running'] or state['stream'].get('channels') != 2:
        if not (args.command == 'align' and args.follow):
            controller.close() if args.command == 'align' else None
            sys.exit('two_channel: no two-channel stream is running (w1700k start --channels 2)')
    rate = state['radio']['sample_rate']
    band = tuple(1e6 * v for v in args.band) if getattr(args, 'band', None) else None
    tuning = {'band': state['radio']['band'], 'lo_hz': state['rf']['lo_hz'], 'sample_rate': rate,
              'filter_mhz': state['rf']['filter_mhz'], 'agc': state['rf']['agc'], 'lna': state['rf']['lna'],
              'lpf': state['rf']['lpf']}
    try:
        if args.command == 'measure':
            record = dict(tuning, when=time.strftime('%Y-%m-%dT%H:%M:%S%z'), **measure(shm, rate, args.seconds, band=band))
            text = json.dumps(record, indent=1)
            print(text)
            if args.out:
                Path(args.out).write_text(text + '\n')
        elif args.command == 'watch':
            done = 0
            while not args.count or done < args.count:
                r = measure(shm, rate, args.seconds, band=band)
                print(f'gain {r["gain_db"]:+6.2f} dB (+-{r["gain_spread_db"] / 2:.2f})  phase {r["phase_deg"]:+7.1f} deg  '
                      f'delay {r["delay_ns"]:+6.2f} ns  coherence {r["coherence"]:.3f} over '
                      f'{r["bandwidth_hz"] / 1e6:.1f} MHz', flush=True)
                done += 1
                time.sleep(args.every)
        elif args.command == 'state':
            r = read_noise(shm, rate, min_seconds=args.seconds, max_seconds=max(8.0, 4 * args.seconds))
            reference = load_references(args.reference).get((tuning['band'], tuning['lo_hz']))
            says = 'no reference is kept for this LO (align learns one)'
            if not r['determined']:
                says = 'too little of the noise is common to the channels here to say'
            elif reference:
                which = state_of(r['phase_deg'], reference['phase_deg'])
                says = ('neither the reference state nor the other: the reference no longer fits' if which is None
                        else f'the {"reference" if which == 0 else "other"} state (reference {reference["phase_deg"]:+.0f} degrees)')
            print(f'noise phase {r["phase_deg"]:+.1f} degrees, correlation {r["correlation"]:.3f} '
                  f'({r["correlation"] / r["chance"]:.0f} times chance, {r["seconds"]:.2f} s): {says}')
        else:
            references = load_references(args.reference)
            if args.follow:
                follow(controller, shm, references, args.reference, learn=args.learn, out=lambda line: print(line, flush=True),
                       seconds=args.seconds)
            else:
                align_here(controller, shm, references, args.reference, args.learn, seconds=args.seconds)
    except (NoReference, Undetermined) as error:
        sys.exit(f'two_channel: {error}')
    except KeyboardInterrupt:
        pass
    finally:
        shm.close()
        if args.command == 'align':
            controller.close()


if __name__ == '__main__':
    main()
