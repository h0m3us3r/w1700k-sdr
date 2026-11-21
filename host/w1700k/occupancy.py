# SPDX-License-Identifier: MIT
"""Spectrum occupancy, measured on the radio itself beside a stream.

While a stream runs, our WM copies a chunk of it into one of the MT7996's DSP cores now and then
(firmware/stream/occ.h) and the core takes its spectrum and keeps account, per bin of a 64th of
the sample rate: the mean power, the largest, and in what share of the snapshots the bin was busy
(firmware/dsp/occ.c). The host only turns it on and reads the report, both in the mailbox page
(include/hw/mt7996/probe_page.h "Occupancy") through the control session; the samples it is made
from never leave the radio for it, and the stream goes on as it was.

    w1700k occupancy [--seconds S] [--per-second N] [--threshold DB] [--keep]

Busy means more than the threshold above the bin's noise in that snapshot, which the core finds
by itself (occ.c says how, and what it cannot see). Powers are dBFS: 0 is a tone that fills the
converter. They are as sampled: the receiver's AGC moves its gain with what is in the air, so a
level is not a field strength, and `gain_span_db` says how far the noise moved over the report.
The samples come at twice the channel filter's width: only the middle half of the bins is inside
the filter (`in_band`), and the Wi-Fi channels listed are those that lie inside it whole.

The noise's shape over the bins is what the quiet moments share: of 64 snapshots after it is
turned on, `learned_from` were alike but for the gain, and the shape is theirs. Under 8 the band
was hardly ever quiet then: the report says so in `note`, its shares are not to be trusted, and
the radio goes on looking (64 snapshots at a time) for as long as it is kept running.

Busy is energy, and near a strong transmitter its skirts make every bin busy while it sends. So
each bin also has `strong`: the share of the snapshots in which it was busy and within 20 dB of
the strongest busy bin. That is where transmissions sit; `busy` is where anything was.
"""
import math
import struct
import time

BASE = 0x0090B000
PERIOD, EVERY, THRESHOLD, STATE, SNAPSHOTS, SEQ = (BASE + at for at in (0x00, 0x04, 0x08, 0x10, 0x14, 0x18))
HEAD_WORDS = 7                    # PERIOD .. SEQ
REPORT, REPORT_WORDS = BASE + 0x40, 328
BINS, BLOCKS = 64, 32
CHUNK_BYTES, WORD_BYTES = 8192, 4
OFF, RUNNING = 0, 1
FAILURES = {2: 'DSP core 0 does not run the occupancy program: the firmware set on the unit has no DSP image '
               '(rebuild with make -C firmware, then w1700k load)',
            3: 'the radio\'s second DMA engine was not idle',
            4: 'a copy into the DSP core did not finish',
            5: 'the DSP core did not answer'}
FULL_SCALE = (8191 * 32) ** 2     # a bin's power for a tone that fills the converter (the Hann window sums to 32)
DSP_HZ = 640e6
PER_SECOND, SECONDS, THRESHOLD_DB = 100, 1.0, 6.0
LEARN, QUIET_LEAST = 64, 8          # firmware/dsp/occ_layout.h OCC_LEARN, OCC_QUIET_LEAST
READ_WORDS = 64                   # rf.IOCTL_WORDS
# The 20 MHz Wi-Fi channels: (first number, last, step, the frequency of channel 0 in MHz).
WIFI = ((1, 13, 1, 2407), (36, 177, 4, 5000), (1, 233, 4, 5950))
WIFI_HALF_HZ = 8_125_000          # half of what a 20 MHz channel's carriers occupy


class OccupancyError(RuntimeError):
    pass


def period_for(sample_rate, per_second):
    """The chunks from one snapshot to the next for about `per_second` snapshots a second."""
    if not 0 < per_second <= 1000:
        raise ValueError('snapshots a second: above 0, up to 1000')
    return max(1, round(sample_rate * WORD_BYTES / CHUNK_BYTES / per_second))


def head(session):
    values, _ = session.ctl(read=(BASE, HEAD_WORDS))
    return {'period': values[0], 'every': values[1], 'state': values[4], 'snapshots': values[5], 'seq': values[6]}


def off(session):
    session.ctl([(PERIOD, 0)])


def on(session, period, every, threshold_db=THRESHOLD_DB, sleep=time.sleep, clock=time.monotonic):
    """Turn it on, or change what it runs at. One that had failed is turned off first and seen off:
    the firmware forgets a failure only when it sees the period at 0."""
    if not (0 < period < 1 << 24 and 0 < every < 1 << 16 and 0 < threshold_db <= 60):
        raise ValueError('occupancy: a period of 1..2^24-1 chunks, 1..65535 snapshots a report, a threshold of 0..60 dB')
    if head(session)['state'] in FAILURES:
        off(session)
        deadline = clock() + 2.0
        while head(session)['state'] != OFF:
            if clock() > deadline:
                raise OccupancyError('the firmware did not turn occupancy off')
            sleep(0.02)
    factor, = struct.unpack('<I', struct.pack('<f', 10 ** (threshold_db / 10)))
    session.ctl([(EVERY, every), (THRESHOLD, factor), (PERIOD, period)])


def report(session):
    """The report in the page as (its number, its words), or None while one is being written."""
    seq = head(session)['seq']
    if not seq or seq & 1:
        return None
    words = []
    for at in range(0, REPORT_WORDS, READ_WORDS):
        words += session.ctl(read=(REPORT + WORD_BYTES * at, min(READ_WORDS, REPORT_WORDS - at)))[0]
    if session.ctl(read=(SEQ, 1))[0][0] != seq:
        return None
    return seq, words


def wait(session, after=0, timeout=5.0, sleep=time.sleep, clock=time.monotonic):
    """The next whole report whose number is not `after`. Raises with the firmware's reason when
    it has stopped, or when none comes."""
    deadline = clock() + timeout
    while True:
        state = head(session)
        if state['state'] in FAILURES:
            raise OccupancyError(FAILURES[state['state']])
        got = report(session) if state['seq'] not in (0, after) else None
        if got and got[0] != after:
            return got
        if clock() > deadline:
            if state['state'] != RUNNING:
                raise OccupancyError('occupancy is not running: it is measured beside a stream')
            raise OccupancyError(f'no report in {timeout:.0f} s ({state["snapshots"]} snapshots so far)')
        sleep(0.05)


def _dbfs(power):
    return round(10 * math.log10(power / FULL_SCALE), 1) if power > 0 else None


def decode(words, sample_rate, center_hz):
    """A report's words as a dict: its bins from the lowest frequency to the highest."""
    if len(words) != REPORT_WORDS:
        raise ValueError(f'a report is {REPORT_WORDS} words')
    raw = struct.pack(f'<{REPORT_WORDS}I', *words)
    number, snapshots, peak, cycles, total, level_min, level_max, quiet = struct.unpack_from('<IIIIIffI', raw)
    mean = struct.unpack_from(f'<{BINS}f', raw, 32)
    most = struct.unpack_from(f'<{BINS}f', raw, 32 + 4 * BINS)
    busy = struct.unpack_from(f'<{BINS}I', raw, 32 + 8 * BINS)
    noise = struct.unpack_from(f'<{BINS}f', raw, 32 + 12 * BINS)
    strong = struct.unpack_from(f'<{BINS}I', raw, 32 + 16 * BINS)
    share = lambda n: round(n / snapshots, 3) if snapshots else 0.0
    step = sample_rate / BINS
    order = list(range(BINS // 2, BINS)) + list(range(BINS // 2))       # bin k >= 32 is below the LO
    offset = lambda k: (k - BINS if k >= BINS // 2 else k) * step
    bins = [{'hz': round(center_hz + offset(k)), 'in_band': abs(offset(k)) + step / 2 <= sample_rate / 4,
             'mean_dbfs': _dbfs(mean[k]), 'max_dbfs': _dbfs(most[k]), 'noise_dbfs': _dbfs(noise[k]),
             'busy': share(busy[k]), 'strong': share(strong[k])} for k in order]
    span = round(10 * math.log10(level_max / level_min), 1) if level_min > 0 and level_max > 0 else None
    out = {'report': number, 'snapshots': snapshots, 'snapshots_since_on': total, 'bin_hz': step,
           'peak_dbfs': round(20 * math.log10(peak / 8191), 1) if peak else None, 'gain_span_db': span,
           'learned_from': quiet, 'dsp_ms': round(cycles / DSP_HZ * 1e3, 3), 'bins': bins}
    if snapshots and quiet < QUIET_LEAST:
        out['note'] = (f'only {quiet} of {LEARN} snapshots were quiet while the noise was learned: the band is too busy '
                       'over this span for the shares to be trusted. A narrower span (a lower sample rate) sees more '
                       'quiet; kept running (--keep), the radio goes on looking')
    return out


def wifi_channels(decoded):
    """The 20 MHz Wi-Fi channels that lie whole inside the channel filter: for each, the share of
    the snapshots in which its busiest bin was busy, the same for strong (a transmission in the
    channel, not another's skirts), and the largest power seen in it."""
    bins = [b for b in decoded['bins'] if b['in_band']]
    half = decoded['bin_hz'] / 2
    low, high = bins[0]['hz'] - half, bins[-1]['hz'] + half
    out = []
    for first, last, step, base in WIFI:
        for number in range(first, last + 1, step):
            center = (base + 5 * number) * 1_000_000
            if center - WIFI_HALF_HZ < low or center + WIFI_HALF_HZ > high:
                continue
            inside = [b for b in bins if abs(b['hz'] - center) <= WIFI_HALF_HZ]
            levels = [b['max_dbfs'] for b in inside if b['max_dbfs'] is not None]
            out.append({'channel': number, 'mhz': center // 1_000_000, 'busy': max(b['busy'] for b in inside),
                        'strong': max(b['strong'] for b in inside), 'max_dbfs': max(levels) if levels else None})
    return out
