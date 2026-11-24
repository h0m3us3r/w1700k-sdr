# SPDX-License-Identifier: MIT
"""The receiver state machine: load, configure, start, stop and RF control, and `w1700k serve`.

The MT7996 runs only our firmware (firmware/: our ROM patch, WM and WA). A load downloads it,
hands it the unit's calibration EEPROM and has it bring the radio up from cold (BRINGUP), then
sets each band up at its LO and sample rate (FILTER, SETUP, CAPTURE); the captured band is the one
ICAP listens to (SELECT). There are no channels: a band is an LO in Hz (anywhere its synthesizer
locks), a sample rate and an analog filter, each changed on its own. Streams are one STREAM command each: our WM runs the producer it carries
until the pump's stop word. Every operation goes to our firmware as a command (wm.py, rf.py); the
host writes no radio register.

The radio's state is kept on the unit (STATE_FILE, in RAM: a reboot clears it, and the boot id
is checked), so separate CLI invocations and `serve` sessions see the same receiver: a controller
reads it before every operation, and a `serve` session every two seconds while it has nothing to
do, and adopts what another has changed. A long operation (load, configure, start, stop) is
written there before it begins, so that the others show it and wait their turn. Streams run
detached on the unit; a controller attaches to one that is already running.

Rules the hardware imposes:
- RF programs (tune, gain, filter, band switch) run in our WM between stream chunks, or in its
  main loop without a stream.
- The LO is one TUNE, also mid-stream. A new sample rate on the band being streamed stops the stream,
  runs RATE and restarts it; switching to another band is one ICAPCR8 write mid-stream.
- A stream's packing follows its sample rate and the link (packing_for): raw 14-bit IQ where it
  fits the unit port's speed, else every chunk packed to 28 bits (full, up to 160 Msps: all the
  radio's two packers carry), else every other chunk (half: 320 Msps on 10 GbE). Packing is
  lossless (8 samples in 7 words); a band switch keeps a running stream as it is where its packing
  carries the new rate. Where none of those fits, the unit's SoC requantises each slice to fewer
  bits with a scale of its own (stream.REDUCED: 16, 14, 8 or 7 bits per I/Q pair, the deepest
  that fits: 160 Msps is 14 bits on 2.5 Gbit/s, 80 Msps is 8 on 1); that is not lossless, and the
  SoC does it only up to 160 Msps (its memory bus cannot read a 320 Msps capture). At 320 Msps
  the split packing shares the work: the radio's cores reduce every other chunk to 14 bits and
  the SoC the ones between (4.6 Gbit/s: 5 Gbit/s carries it). A rate no packing fits on the
  link is refused. Where that choice is wrong for a link, export.packing (or
  `start --packing`, `serve --packing`) sets the packing by hand: the link is then not consulted,
  only what the packers can do.
- The analog filter is the receiver's only anti-alias filter: the converter samples at 320 MHz, and
  what the filter passes 320 MHz from the LO folds onto the band (18..20 dB down with the 320 MHz
  filter, 32 at 160, 45 at 80: include/hw/adie/mt7976.h). So each band's filter follows its sample
  rate (filter_for) unless one is set by hand, which holds until it is told to follow again.
"""
import json
import queue
import secrets
import sys
import threading
import time

from . import config, firmware, occupancy, rf, wm
from .config import local_link_for
from dataclasses import replace

from .radio import (DEFAULT_BANDS, DEFAULT_FILTER_MHZ, LO_RANGE, BandConfig, Radio, actual_lo, eeprom_image,
                    unit_eeprom)
from .stream import (GO_ADDR, PACKED_SHARE, REDUCED, REDUCED_MAX_RATE, SENDER_MAX_DATAGRAMS, SPLIT, Result,
                     Stream, StreamError, check_host_mtu, datagram_rate, wire_rate)
from .service import announce_channels
from .unit import Unit

STATE_FILE = '/tmp/w1700k-state.json'
# The unit's boot id, its uptime in seconds, then the state file: what a controller follows.
READ_STATE = f'cat /proc/sys/kernel/random/boot_id; cut -d" " -f1 /proc/uptime; cat {STATE_FILE} 2>/dev/null; true'
# Another session's long operation is waited for this long, and believed for as long: a session
# killed in the middle of one leaves its mark behind.
BUSY_WAIT = 120.0
BANDS = (0, 1, 2)
DEFAULT_BAND = 1
PACKINGS = config.PACKINGS
assert PACKINGS == tuple(PACKED_SHARE) + tuple(REDUCED)
# The order tried: lossless first, then the deepest. split is 14 bits where the unit alone cannot
# reduce (above REDUCED_MAX_RATE), the numbered ones where it can.
AUTOMATIC = ('raw', 'full', 'half', SPLIT) + tuple(REDUCED)
FULL_MAX_RATE = 160_000_000       # the WM and the WA pack every chunk up to this rate, half of them above
TUNE_PHASE = ('keep', 'relock')       # a tune's say over the phase between a band's chains (Controller.tune)
TWO_CHANNELS_MAX_RATE = 160_000_000   # two channels at this rate each are what the radio moves at 320 Msps, its most
TWO_CHANNELS_GAIN = (2, 5)            # LNA and LPF index both chains are given when the AGC was on: mid-range
DEFAULT_LINK_MBPS = 10_000        # a port that reports no speed: the unit's 10 GbE
LINK_FILL = 0.975                 # the share of the link a stream may take (320 Msps half is 97.0 % of 10 GbE)
START_TIMEOUT = 10.0
COMMAND_TIMEOUT = 60.0


class ControlError(RuntimeError):
    pass


class Refused(ControlError):
    """A request turned down before anything changed: the receiver is as it was."""


LOCK_RESOLUTION_HZ = 100_000            # the lock-edge search's resolution
LOCK_NEAR_HZ = 20_000_000               # the edges lie this close to TUNE's reach: tried first
LOCK_MARGIN_HZ = 1_000_000              # the span kept inside the edges found (they wander ~0.1 MHz)


def packing_fits(packing, rate, link_mbps, piece=None):
    """Whether a stream at `rate` under `packing` fits the link, the packers and the unit's senders."""
    if packing == 'full' and rate > FULL_MAX_RATE or packing in REDUCED and rate > REDUCED_MAX_RATE:
        return False
    if packing == SPLIT:               # whole slices only, and only where the unit cannot do it alone
        return not piece and rate > REDUCED_MAX_RATE and wire_rate(packing, rate) <= link_mbps * 1e6 * LINK_FILL
    if piece and datagram_rate(packing, rate, piece) > SENDER_MAX_DATAGRAMS:
        return False
    return wire_rate(packing, rate, piece) <= link_mbps * 1e6 * LINK_FILL


def packing_for(rate, link_mbps=DEFAULT_LINK_MBPS, piece=None):
    """The packing of a stream at `rate` on a link of `link_mbps`: raw where it fits, else every
    chunk packed (the most room on the link, lossless), else every other chunk, else reduced to the
    most bits that fit."""
    for packing in AUTOMATIC:
        if packing_fits(packing, rate, link_mbps, piece):
            return packing
    if piece and not any(datagram_rate(p, rate, piece) <= SENDER_MAX_DATAGRAMS for p in AUTOMATIC if p != SPLIT):
        raise Refused(f'{rate / 1e6:g} Msps in datagrams of {piece} bytes (export.piece) is more than the '
                      f'unit sends: {datagram_rate(AUTOMATIC[-1], rate, piece) / 1e3:.0f} thousand a second '
                      f'even at {AUTOMATIC[-1]} bits per sample, of {SENDER_MAX_DATAGRAMS / 1e3:g}')
    if rate > REDUCED_MAX_RATE:
        need = wire_rate(SPLIT, rate)
        raise Refused(f'{rate / 1e6:g} Msps needs {need / 1e9:.2f} Gbit/s on the link even reduced to 14 bits '
                      f'(split between the radio and the unit, which is all they manage at that rate), '
                      f'and the link carries {link_mbps / 1e3:g} (at most {LINK_FILL:.1%} of it)')
    need = wire_rate(AUTOMATIC[-1], rate, piece)
    raise Refused(f'{rate / 1e6:g} Msps needs {need / 1e9:.2f} Gbit/s on the link even at '
                  f'{AUTOMATIC[-1]} bits per sample, and the link carries {link_mbps / 1e3:g} '
                  f'(at most {LINK_FILL:.1%} of it)')


def filter_for(cfg):
    """The analog filter that follows a band's sample rate: as wide as the rate (the widest the
    roll-off correction applies to, and no wider: the filter is what keeps aliases out), 320 MHz at
    640/2."""
    return min(DEFAULT_FILTER_MHZ, cfg.sample_rate // 1_000_000)


def default_rf(cfg, filter_mhz=DEFAULT_FILTER_MHZ):
    """A band's RF state after its SETUP. The analog filter is its own setting: it follows the
    sample rate, or is the one set by hand, kept across LO and sample-rate changes."""
    return {'lo_hz': cfg.lo_hz, 'agc': True, 'lna': None, 'lpf': None, 'filter_mhz': filter_mhz}


class Controller:
    def __init__(self, config, emit=None, unit=None):
        self.config = config
        self.emit = emit or (lambda event: None)
        self.unit = unit or Unit(config.unit)
        self.radio = Radio(self.unit, warn=lambda text: self._log('warn', text))
        self.stream = Stream(self.unit)
        self._ctl = None
        self.boot_id = None
        self.session = secrets.token_hex(4)   # this controller, among those that share the unit
        self._seen = None                 # the state file as this controller last wrote or read it
        self._unit_clock = None           # the unit's uptime minus this host's monotonic clock
        self.busy_other = None            # another session's long operation, and when it began
        self._busy_other_since = None     # (the unit's uptime)
        self.busy = None
        self.error = None
        self._reset()
        self.snapshot = self.state()

    def _reset(self):
        """The receiver of a unit that holds no state: nothing loaded, every band at its default."""
        self.loaded = False
        self.bands = dict(DEFAULT_BANDS)
        self.bands_ready = []
        self.band = DEFAULT_BAND
        self.packing = None               # the running stream's packing; None: no stream
        self.channels = 1                 # a stream's channels: 1, or 2 (two chains of the band, sampled together)
        self.gain_was_agc = False         # a two-channel stream fixed the gain itself; a stop gives the AGC back
        self.link_mbps = None             # the export link's capacity, read at a stream start; None: not yet
        self.packing_manual = None        # the running stream's packing as set by hand; None: chosen automatically
        self.filter_follows_rate = True   # each band's analog filter is filter_for() its sample rate
        self.rf = {b: default_rf(cfg, filter_for(cfg)) for b, cfg in self.bands.items()}
        self.synth_locked = [None] * len(BANDS)
        self.lo_lock = {}                 # band -> (lowest, highest) LO its synthesizer locks at, found at load
        # band -> its synthesizer's locks counted (None: its phase is not known to have been kept).
        # Each full lock leaves the band's chains at random in one of two phases half a turn apart
        # from each other: a comparison of two channels made under one count holds under that count.
        self.relocks = {b: 0 for b in BANDS}

    # --- state ---

    def state(self):
        cfg, rfs = self.bands[self.band], self.rf[self.band]
        return {
            'busy': self.busy or self.busy_other,
            'radio': {'loaded': self.loaded, 'band': self.band, 'rf_band': cfg.rf_band, 'lo_hz': cfg.lo_hz,
                      'sample_rate': cfg.sample_rate, 'capture_rate': cfg.capture_rate,
                      'bands_ready': sorted(self.bands_ready),
                      'synth_locked': list(self.synth_locked)},
            'stream': {'running': self.packing is not None, 'packing': self.packing,
                       'packing_manual': self.packing_manual is not None, 'link_mbps': self.link_mbps,
                       'sample_rate': cfg.sample_rate, 'center_hz': rfs['lo_hz'], 'channels': self.channels},
            'rf': dict(rfs, lo_range_hz=list(self.lo_lock.get(self.band, LO_RANGE[self.band])),
                       filter_follows_rate=self.filter_follows_rate, relocks=self.relocks[self.band]),
            'error': self.error,
        }

    def _changed(self):
        self.snapshot = self.state()
        self.emit({'event': 'state', 'state': self.snapshot})

    def _log(self, level, text):
        self.emit({'event': 'log', 'level': level, 'text': text})

    def _progress(self, text, fraction):
        self.emit({'event': 'progress', 'text': text, 'fraction': round(fraction, 3)})

    def _save(self):
        record = {'boot_id': self.boot_id, 'loaded': self.loaded, 'band': self.band,
                  'lo_lock': {str(b): list(v) for b, v in self.lo_lock.items()},
                  'bands': {str(b): c.as_dict() for b, c in self.bands.items()},
                  'bands_ready': self.bands_ready, 'packing': self.packing, 'link_mbps': self.link_mbps,
                  'channels': self.channels, 'gain_was_agc': self.gain_was_agc,
                  'packing_manual': self.packing_manual, 'filter_follows_rate': self.filter_follows_rate,
                  'rf': {str(b): v for b, v in self.rf.items()}, 'synth_locked': self.synth_locked,
                  'relocks': [self.relocks[b] for b in BANDS],
                  # last, after everything the unit's own reader of this file looks for: whose it
                  # is, and the long operation that session is in the middle of
                  'session': self.session, 'busy': self.busy,
                  'busy_since': self._unit_now() if self.busy else None}
        text = json.dumps(record)
        self.unit.push(text.encode(), STATE_FILE)
        self._seen = text

    def _unit_now(self):
        """The unit's uptime now, as far as the last reading of it tells; None before the first."""
        return None if self._unit_clock is None else round(time.monotonic() + self._unit_clock, 2)

    def attach(self):
        """Adopt the receiver state the unit holds for this boot, and a stream already running."""
        self._follow()
        self._check_stream()
        self._changed()

    def _follow(self):
        """Adopt what the unit holds where that is not what this controller last saw there:
        another session's change, or a reboot. True when the receiver is not as it was believed."""
        text = self.unit.check(READ_STATE)
        boot_id, _, rest = text.partition('\n')
        uptime, _, saved = rest.partition('\n')
        boot_id, saved = boot_id.strip(), saved.strip()
        try:
            self._unit_clock = float(uptime) - time.monotonic()
        except ValueError:
            pass
        if boot_id == self.boot_id and saved == self._seen:
            if self.busy_other and not self._busy_fresh():      # that session went away mid-operation
                self.busy_other = None
                self._changed()
                return True
            return False
        if self.boot_id is not None and boot_id != self.boot_id:
            self._log('warn', 'the unit has rebooted: its radio is not loaded')
            self._close_ctl()
            self._reset()
        self.boot_id, self._seen = boot_id, saved
        self.busy_other = None
        try:
            record = json.loads(saved) if saved else None
            if record and record.get('boot_id') == self.boot_id:
                self._restore(record)
        except (ValueError, KeyError, TypeError):
            self._log('warn', 'ignoring an unreadable receiver state on the unit')
        self._changed()
        return True

    def _busy_fresh(self):
        now, since = self._unit_now(), self._busy_other_since
        # (two sessions' readings of the unit's clock differ by their round trips: a mark that
        # seems to lie a moment ahead is a fresh one)
        return not isinstance(since, (int, float)) or now is None or now - since < BUSY_WAIT

    def _restore(self, record):
        bands = {int(b): BandConfig(int(b), **c).validate() for b, c in record['bands'].items()}
        rfs = {int(b): dict(v) for b, v in record['rf'].items()}
        if (sorted(bands) != list(BANDS) or sorted(rfs) != list(BANDS) or record['band'] not in BANDS
                or record['packing'] not in (None, *PACKINGS)
                or any(set(v) != set(default_rf(bands[b])) for b, v in rfs.items())):
            raise ValueError('inconsistent state')
        self.bands, self.band = bands, record['band']
        self.loaded, self.bands_ready = bool(record['loaded']), list(record['bands_ready'])
        self.packing = record['packing']
        self.channels = 2 if record.get('channels') == 2 and self.packing else 1
        self.gain_was_agc = bool(record.get('gain_was_agc')) and self.channels == 2
        link = record.get('link_mbps')
        self.link_mbps = link if isinstance(link, int) and not isinstance(link, bool) and link > 0 else None
        manual = record.get('packing_manual')
        self.packing_manual = manual if manual in PACKINGS and self.packing else None
        self.filter_follows_rate = bool(record.get('filter_follows_rate', True))
        self.rf = rfs
        self.lo_lock = {int(b): tuple(v) for b, v in record.get('lo_lock', {}).items()}
        counts = record.get('relocks')
        if isinstance(counts, list) and len(counts) == len(BANDS) and all(type(v) is int and v >= 0 for v in counts):
            self.relocks = dict(zip(BANDS, counts))
        locks = record.get('synth_locked', [None] * len(BANDS))
        if len(locks) == len(BANDS) and all(v in (None, True, False) for v in locks):
            self.synth_locked = list(locks)
        busy = record.get('busy') if record.get('session') != self.session else None
        self._busy_other_since = record.get('busy_since')
        self.busy_other = busy if isinstance(busy, str) and self._busy_fresh() else None

    # --- operations ---

    def run(self, busy, operation, *args):
        """Run one operation with the busy flag set and errors recorded in the state. A request
        that is refused (Refused: the receiver stays as it was) is logged, not recorded: the
        state's error is what is wrong with the receiver, and it keeps the one it had."""
        before = self.error
        try:
            self._wait_turn()
        except Exception as exc:
            self.error = str(exc)
            self._log('error', self.error)
            self._changed()
            raise
        self.busy, self.error = busy, None
        self._changed()
        try:
            if busy:
                self._save()        # the other sessions show it, and wait
            return operation(*args)
        except Refused as exc:
            self.error = before
            self._log('error', str(exc))
            raise
        except Exception as exc:
            self.error = str(exc)
            self._log('error', self.error)
            raise
        finally:
            self.busy = None
            if busy:
                try:
                    self._save()
                except Exception as exc:        # the unit is gone: the others will see that themselves
                    self._log('warn', f'the receiver state was not saved: {exc}')
            self._changed()

    def _wait_turn(self):
        """The receiver as the unit holds it now, once no other session is in the middle of a long
        operation on it."""
        self._follow()
        deadline = time.monotonic() + BUSY_WAIT
        while self.busy_other and time.monotonic() < deadline:
            time.sleep(0.3)
            self._follow()
        if self.busy_other:
            self._log('warn', f'another session has been {self.busy_other} for {BUSY_WAIT:.0f} s: going ahead')
            self.busy_other = None

    def load(self, band=None):
        """Download our firmware and bring the radio up from cold, every band at its LO and sample rate."""
        return self.run('loading', self._load, band)

    def configure(self, band, sample_rate=None, lo_hz=None, capture_rate=None):
        """Capture `band`, optionally at another sample rate (RATE) and LO (TUNE); what is not given
        stays. A sample rate alone is captured at that rate; band 2's 640/2 is 320 Msps captured
        at 640 (radio.RATE_CODE)."""
        if band not in self.bands:
            raise ValueError('band 0, 1 or 2')
        cur = self.bands[band]
        if sample_rate is None:
            if capture_rate is not None:
                raise ValueError('a capture rate needs its sample rate')
            sample_rate, capture_rate = cur.sample_rate, cur.capture_rate
        cfg = BandConfig(band, cur.lo_hz if lo_hz is None else int(lo_hz), sample_rate, capture_rate).validate()
        self._check_lock_range(band, cfg.lo_hz)
        return self.run('configuring', self._configure, cfg)

    def start(self, packing=None, channels=1):
        """Start streaming; `packing` sets it by hand, as export.packing does for every stream.
        `channels` 2 streams two receive chains of the band (chains 0 and 1, the radio's two
        capture paths), sampled at the same instants, each at the band's sample rate: together
        they cost what one channel costs at twice that rate. The host's stream service is told
        first, and lays every slot out by channel (w1700k_stream.h)."""
        if packing is not None and packing not in PACKINGS:
            raise ValueError(f'packing: one of {", ".join(PACKINGS)}')
        if channels not in (1, 2):
            raise ValueError('channels: 1 or 2')
        return self.run('starting', self._start, packing, channels)

    def stop(self):
        return self.run('stopping', self._stop_asked)

    def _stop_asked(self):
        """A stop that was asked for: the next stream is one channel again unless it says
        otherwise, and the AGC is back if two channels took it away."""
        result = self._stop()
        if self.channels != 1:
            self.channels = 1
            if self.gain_was_agc:
                self.gain_was_agc = False
                self._gain(True, None, None)
            self._save()
        return result

    def tune(self, hz, phase=None):
        """The active band's LO. `phase` is what becomes of the phase between the band's chains:
        None keeps it while a two-channel stream runs, wherever the synthesizer's lock reaches
        (rf.hold), and locks again otherwise; 'keep' keeps it or refuses; 'relock' locks again.
        Every new lock is counted in the state (rf.relocks)."""
        if phase not in (None, *TUNE_PHASE):
            raise ValueError(f'phase: {" or ".join(TUNE_PHASE)}')
        return self.run(None, self._tune, int(hz), phase)

    def rephase(self, chain=1):
        """Start one chain's LO path of the active band again: the phase between it and the band's
        other chains falls anew, at random, in one of its two states half a turn apart (what a
        new lock does to every chain, done to one with the synthesizer left alone). Counted with
        the locks (rf.relocks). For whoever can tell the states apart and wants a particular one
        (tools/two_channel.py align)."""
        return self.run(None, self._rephase, chain)

    def gain(self, agc=False, lna=None, lpf=None):
        return self.run(None, self._gain, agc, lna, lpf)

    def filter(self, mhz):
        """The active band's analog filter, set by hand: filters no longer follow the sample rate."""
        return self.run(None, self._filter, mhz)

    def filter_follow(self, on=True):
        """Whether every band's analog filter follows its sample rate; turned on, they are set to it."""
        return self.run(None, self._filter_follow, bool(on))

    def trim(self, code):
        self._require_loaded()
        return rf.set_trim(self._session(), self.band, code)

    def occupancy(self, seconds=occupancy.SECONDS, per_second=occupancy.PER_SECOND,
                  threshold_db=occupancy.THRESHOLD_DB, keep=False):
        """One occupancy report of the stream that runs: what the radio's DSP core saw in `seconds`
        of it, at about `per_second` snapshots a second (occupancy.py). keep: leave it running."""
        return self.run(None, self._occupancy, seconds, per_second, threshold_db, keep)

    def rf_status(self):
        self._require_loaded()
        out = rf.status(self._session(), self.band)
        self._check_locks()
        self._changed()
        return out

    def poll(self):
        """Adopt what another session has changed, and notice a stream that ended on its own
        (overrun, lost source, killed pump)."""
        self._follow()
        self._check_stream()

    def _check_stream(self):
        if self.busy_other:
            return      # that session is stopping or restarting it: its end is not the stream's own
        if self.packing and self.stream.finished():
            result = self._finish_stream()
            self.error = f'the stream ended: {result.text}'
            self._log('error', self.error)
            self._changed()

    def close(self):
        self._close_ctl()
        self.unit.close()

    # --- implementation ---

    def _require_loaded(self):
        if not self.loaded:
            raise Refused('the radio is not loaded')

    def _link(self):
        return self.link_mbps or DEFAULT_LINK_MBPS

    def _packing_at(self, rate, manual=None):
        """The packing of a stream at `rate`: the one set by hand (this stream's, else the
        configuration's) where the packers can do it, else the one the rate and the link give."""
        manual = manual or self.config.export.packing
        if not manual:
            return packing_for(rate, self._link(), self.config.export.piece)
        if manual == 'full' and rate > FULL_MAX_RATE:
            raise Refused(f'packing full is set by hand, and the radio packs every chunk only up to '
                          f'{FULL_MAX_RATE / 1e6:g} Msps, not {rate / 1e6:g}')
        if manual in REDUCED and rate > REDUCED_MAX_RATE:
            raise Refused(f'packing {manual} is set by hand, and the unit reduces the bits per sample only '
                          f'up to {REDUCED_MAX_RATE / 1e6:g} Msps, not {rate / 1e6:g}')
        if manual == SPLIT and (rate <= REDUCED_MAX_RATE or self.config.export.piece):
            raise Refused(f'packing split is set by hand; it is for whole slices above '
                          f'{REDUCED_MAX_RATE / 1e6:g} Msps, where the unit cannot reduce a stream alone')
        return manual

    def _tune(self, hz, phase=None):
        self._require_loaded()
        b = self.band
        self._check_lock_range(b, hz)
        self._retune(b, hz, phase)

    def _rephase(self, chain):
        self._require_loaded()
        if type(chain) is not int or not 0 <= chain < rf.CHAINS[self.band]:
            raise ValueError(f'chain: 0..{rf.CHAINS[self.band] - 1}')
        self.relocks[self.band] += 1
        self._save()
        rf.rephase(self._session(), self.band, chain)

    def _retune(self, b, hz, phase=None):
        """Band b's LO, by tune()'s rule for the phase between its chains."""
        must = phase == 'keep'
        keep = must or phase is None and bool(self.packing) and self.channels == 2 and b == self.band
        if keep and self._hold_band(b, hz, must):
            return
        if keep:
            self._log('warn', f'band {b}: {hz / 1e6:.3f} MHz is beyond what its lock reaches; locked again, and '
                              'the phase between its channels may have turned half a turn')
        self._tune_band(b, hz)

    def _hold_band(self, b, hz, must):
        """Band b's LO moved with its lock kept (HOLD): whether it was. Not when the unit will not;
        `must` makes that a refusal. A lock that did not follow is locked again here either way,
        and with `must` that is an error once it is."""
        if self.synth_locked[b] is False:
            if must:
                raise Refused(f'band {b} has no lock to keep')
            return False
        try:
            actual = rf.hold(self._session(), b, hz)
        except rf.HoldRefused as exc:
            if must:
                raise Refused(f'{exc}; a tune that locks again reaches it, and may turn the phase between '
                              'the channels half a turn')
            return False
        except rf.HoldLost as exc:
            self._log('warn', f'{exc}; locking again')
            self._tune_band(b, hz)
            if must:
                raise ControlError(f'{exc}: the band was locked again there, and the phase between its '
                                   'channels may have turned half a turn')
            return True
        self.rf[b]['lo_hz'] = actual
        self.bands[b] = replace(self.bands[b], lo_hz=actual)
        self._save()
        return True

    def _check_lock_range(self, band, hz):
        span = self.lo_lock.get(band)
        if span and not span[0] <= hz <= span[1]:
            raise ValueError(f'band {band} locks {span[0] / 1e6:.1f}-{span[1] / 1e6:.1f} MHz on this unit')

    def _locks_at(self, band, hz):
        self.relocks[band] += 1
        try:
            rf.tune(self._session(), band, hz)
            return True
        except rf.SynthUnlocked:
            return False

    def _probe_lock(self, band):
        """Band's lockable LO span on this unit: from its LO (locked) towards each end of TUNE's reach,
        bisected on TUNE's lock result to LOCK_RESOLUTION_HZ, LOCK_MARGIN_HZ inside the edges found
        (they differ from die to die by tens of MHz); the band is retuned to its LO afterwards. A
        band whose LO does not lock keeps TUNE's reach."""
        home = self.bands[band].lo_hz
        if not self._locks_at(band, home):
            return tuple(LO_RANGE[band])
        edges = []
        for bound in LO_RANGE[band]:
            inward = 1 if bound < home else -1
            if self._locks_at(band, bound):
                edges.append(bound)
                continue
            near = bound + inward * LOCK_NEAR_HZ
            good = near if self._locks_at(band, near) else home
            bad = bound
            while abs(bad - good) > LOCK_RESOLUTION_HZ:
                mid = (good + bad) // 2
                if self._locks_at(band, mid):
                    good = mid
                else:
                    bad = mid
            edges.append(good + inward * LOCK_MARGIN_HZ)
        self._locks_at(band, home)
        return tuple(edges)

    def _tune_band(self, b, hz):
        """Band b's LO (TUNE), mid-stream too; the LO realised is the band's from now on. Its
        synthesizer locks again, which is counted."""
        self.relocks[b] += 1
        try:
            actual = rf.tune(self._session(), b, hz)
        except rf.SynthUnlocked:
            self.synth_locked[b] = False
            self._save()
            raise
        self.rf[b]['lo_hz'] = actual
        self.bands[b] = replace(self.bands[b], lo_hz=actual)
        self.synth_locked[b] = True
        self._save()

    def _check_locks(self):
        """Every band's synthesizer lock; an unlocked band is an error that only a power cycle
        clears, not a reason to reload."""
        self.synth_locked = rf.synth_locks(self._session())
        self._save()
        unlocked = [b for b, ok in enumerate(self.synth_locked) if not ok]
        if unlocked:
            self.error = str(rf.SynthUnlocked(unlocked[0]))
            self._log('error', '; '.join(str(rf.SynthUnlocked(b)) for b in unlocked))

    def _gain(self, agc, lna, lpf):
        self._require_loaded()
        if agc:
            lna = lpf = None
        elif lna is None or lpf is None:
            raise ValueError('gain: agc, or both lna and lpf')
        rf.set_gain(self._session(), self.band, lna, lpf)
        self.rf[self.band].update(agc=bool(agc), lna=lna, lpf=lpf)
        self._save()

    def _filter(self, mhz):
        self._require_loaded()
        if mhz not in rf.FILTER_CODE:
            raise ValueError(f'filter: one of {", ".join(map(str, rf.FILTER_CODE))} MHz')
        rf.set_filter(self._session(), self.band, mhz)
        self.rf[self.band]['filter_mhz'] = mhz
        self.filter_follows_rate = False
        self._save()

    def _filter_follow(self, on):
        self.filter_follows_rate = on
        if on and self.loaded:
            for b in BANDS:
                self._follow_filter(b)
        self._save()

    def _follow_filter(self, b):
        """Band b's filter at its sample rate, if filters follow."""
        want = filter_for(self.bands[b])
        if self.filter_follows_rate and self.rf[b]['filter_mhz'] != want:
            rf.set_filter(self._session(), b, want)
            self.rf[b]['filter_mhz'] = want

    def _occupancy(self, seconds, per_second, threshold_db, keep):
        self._require_loaded()
        if not self.packing:
            raise Refused('occupancy is measured beside a stream: start one')
        if self.channels != 1:
            # The DSP reads a chunk as one chain's samples in time order; a two-channel stream's
            # chunks hold two chains, two samples of each in turn (iq_format.h).
            raise Refused('occupancy reads a stream of one channel: this one has two')
        if not 0 < seconds <= 600:
            raise ValueError('occupancy: up to 600 seconds')
        cfg, session = self.bands[self.band], self._session()
        every = max(1, round(seconds * per_second))
        if every > 0xFFFF:
            raise ValueError('occupancy: at most 65535 snapshots a report')
        last = occupancy.head(session)['seq']
        occupancy.on(session, occupancy.period_for(cfg.sample_rate, per_second), every, threshold_db)
        try:
            # A report takes as long as its snapshots take, and beside a 320 Msps stream the
            # firmware gets to fewer of them a second than were asked for.
            _, words = occupancy.wait(session, last, timeout=4 * seconds + 5)
        except occupancy.OccupancyError as exc:
            raise Refused(str(exc))     # nothing is wrong with the receiver: the stream goes on
        finally:
            if not keep:
                occupancy.off(session)
        result = occupancy.decode(words, cfg.sample_rate, self.rf[self.band]['lo_hz'])
        return dict(result, wifi=occupancy.wifi_channels(result))

    def _command(self, op, *args, timeout=COMMAND_TIMEOUT):
        """One radio command on our WM; raises ControlError with the firmware's reason."""
        res = self._wm().radio(op, *args, timeout=timeout)
        if res['res']:
            reason = wm.RADIO_RESULTS.get(res['res'], f'result {res["res"]}')
            if res['fails']:
                reason += f' ({wm.RADIO_FAILS.get(res["fail_code"], "failure")} {res["fail_arg"]:#x})'
            raise ControlError(f'{op}: {reason}')
        return res

    def _filter_of(self, band):
        if self.filter_follows_rate:
            return filter_for(self.bands[band])
        return self.rf.get(band, {}).get('filter_mhz', DEFAULT_FILTER_MHZ)

    def _set_band(self, cfg):
        """A band at its LO and sample rate (SETUP: everything that follows from them, the synthesizer,
        the DC-offset calibration at the band's filter) and its capture path. The analog filter is
        not part of it (FILTER, set at load). The band keeps the LO the synthesizer realises."""
        self.relocks[cfg.band] += 1
        self._command('setup', cfg.band, cfg.lo_hz & 0xFFFFFFFF, cfg.lo_hz >> 32, cfg.rate_code)
        self._command('capture', cfg.band, cfg.rate_code)
        self.bands[cfg.band] = replace(cfg, lo_hz=actual_lo(cfg.band, cfg.lo_hz))

    def _set_rate(self, cfg):
        """A band's sample rate alone (RATE: no synthesizer, no DCOC; LO and filter stay), then its
        capture path."""
        self._command('rate', cfg.band, cfg.rate_code)
        self._command('capture', cfg.band, cfg.rate_code)

    def _load(self, band=None):
        if band is not None:
            if band not in BANDS:
                raise ValueError('band 0, 1 or 2')
            self.band = band
        if self.packing:
            self._stop()
        self._close_ctl()
        fw = firmware.load(self.config.firmware_dir)
        eeprom = self._eeprom()
        self.loaded, self.bands_ready, self.lo_lock = False, [], {}
        self._save()
        self._progress('loading our firmware', 0.0)
        wm.load(self.radio, fw, self.config.unit.module, self.config.unknown_rom)
        w = self._wm()
        w.wait_running()
        if self.config.raw_access:
            w.unlock()
            self._log('warn', 'our WM is unlocked, as the configuration says (raw_access): raw programs and calls '
                              'reach anything, what could transmit and the eFuse included')
        self._progress('bringing the radio up', 0.3)
        w.put_eeprom(eeprom)
        self._command('bringup')
        for i, b in enumerate([b for b in BANDS if b != self.band] + [self.band]):
            self._progress(f'band {b}', 0.5 + 0.15 * i)
            rf.set_filter(self._session(), b, self._filter_of(b))   # before SETUP's DCOC
            self._set_band(self.bands[b])
            self.bands_ready.append(b)
        self._progress('lock ranges', 0.95)
        self.lo_lock = {b: self._probe_lock(b) for b in BANDS}
        self._command('select', self.band)
        self.loaded = True
        self.rf = {b: default_rf(cfg, self._filter_of(b)) for b, cfg in self.bands.items()}
        self.synth_locked = rf.synth_locks(self._session())
        self._save()
        self._progress('ready', 1.0)

    def _wm(self):
        return wm.Wm(self.unit, session=self._session())

    def _eeprom(self):
        """The calibration to load: a file the user named; else what the unit itself carries, so
        that a host with several units never gives one unit another's; else the one saved copy."""
        try:
            path = config.eeprom_path(self.config)
            if path is None:
                own = unit_eeprom(self.unit)
                if own is not None:
                    return eeprom_image(own)
                path = config.saved_eeprom()
            return eeprom_image(path.read_bytes())
        except config.ConfigError as exc:
            raise ControlError(str(exc))
        except OSError as exc:
            raise ControlError(f'cannot read the calibration EEPROM {path}: {exc.strerror}')

    def _configure(self, cfg):
        b = cfg.band
        running = self.packing
        if not self.loaded:
            self.bands[b], self.band = cfg, b
            self._load()
            return
        old = self.bands[b]
        manual = self.packing_manual
        if running:
            # refused before anything changes: what the stream's channels cost at that rate (two
            # channels have a highest rate of their own), and a packing for it
            needed = self._packing_at(self._stream_rate(cfg, self.channels), manual)
        if cfg.rate_code == old.rate_code and b in self.bands_ready:
            # the band's rate unchanged: switch the capture (mid-stream when the running packing
            # carries that rate, else restarted with the packing it needs), then the LO alone if it
            # moved
            if b != self.band:
                # a stream chosen automatically keeps a packing that still fits; one set by hand is that one.
                # Two channels are started again on the other band (the switch on a running ring is
                # tried with one capture path only), and stay two.
                if running and self.channels == 1 and (
                        running == needed or not manual and running not in REDUCED and running != SPLIT
                        and packing_fits(running, cfg.sample_rate, self._link(), self.config.export.piece)):
                    self._switch_mid_stream(b)
                else:
                    if running:
                        self._stop()
                    self._command('select', b)
                    self.band = b
                    self._save()
                    if running:
                        self._start(manual)
            if cfg.lo_hz != old.lo_hz:
                self._retune(b, cfg.lo_hz)
            return
        # another sample rate: RATE (no synthesizer: the LO, gain and filter stay), then the LO
        if running:
            self._stop()
        self.band = b
        if b in self.bands_ready:                 # not ready until its capture is set again
            self.bands_ready.remove(b)
            self._save()
        self._set_rate(replace(cfg, lo_hz=old.lo_hz))
        self.bands[b] = replace(old, sample_rate=cfg.sample_rate, capture_rate=cfg.capture_rate)
        self._follow_filter(b)
        self.bands_ready.append(b)
        self._command('select', b)
        self._save()
        if cfg.lo_hz != old.lo_hz:
            self._retune(b, cfg.lo_hz)
        if running:
            self._start(manual)

    def _switch_mid_stream(self, band):
        before, after = rf.select_band(self._session(), band)
        self.band = band
        self._save()
        self._log('info', f'capturing band {band} from ICAP0 write pointer {before:#x}..{after:#x}')
        self._check_locks()

    def _stream_rate(self, cfg, channels):
        """The rate a stream of `channels` channels costs: the radio, the unit and the link carry
        two channels at a band's rate exactly as they carry one at twice it. Two are refused where
        the capture is another datapath (the 320 MHz class, and so 640/2) and where twice the rate
        is more than the radio can move."""
        if channels == 1:
            return cfg.sample_rate
        if cfg.capture_rate != cfg.sample_rate or cfg.sample_rate > TWO_CHANNELS_MAX_RATE:
            raise Refused(f'two channels are streamed up to {TWO_CHANNELS_MAX_RATE / 1e6:g} Msps each, captured at '
                          f'that rate; band {cfg.band} is at {cfg.sample_rate / 1e6:g} (captured at '
                          f'{cfg.capture_rate / 1e6:g})')
        return 2 * cfg.sample_rate

    def _announce(self, channels):
        """The host's stream service told how many channels the stream about to start has, so
        that its first slot is already laid out for them."""
        name = self.config.export.service
        if name and announce_channels(channels, name):
            return
        if channels == 2:
            self._log('warn', 'no w1700k-stream of this user on this host was told that the stream has two channels '
                              '(none running, or one from before two channels): whoever reads it must know')

    def _equal_gain(self):
        """Two channels are compared with each other, so both chains get one fixed gain: under the
        AGC each chain's gain steps by itself, and their ratio and phase step with it. A gain set
        by hand is left as it is."""
        if self.rf[self.band]['agc']:
            self._gain(False, *TWO_CHANNELS_GAIN)
            self.gain_was_agc = True
            self._log('info', f'two channels: the gain is fixed at LNA {TWO_CHANNELS_GAIN[0]}, LPF '
                              f'{TWO_CHANNELS_GAIN[1]} on both chains instead of the AGC (set another with gain)')

    def _start(self, packing=None, channels=None):
        if self.packing:
            return
        if channels is None:
            channels = self.channels      # a stream this controller restarts keeps its channels
        if not self.loaded:
            self._load()
        export = self.config.export
        # Without a destination MAC in the configuration, the stream goes to this host.
        here = None if export.dst_mac else local_link_for(export.dst_ip)
        dst_mac = export.dst_mac or here[1]
        speed, mtu = self.stream.check_interface(export.interface)
        self.link_mbps = min((v for v in (speed, export.link_mbps) if v), default=None)
        manual = packing or export.packing
        packing = self._packing_at(self._stream_rate(self.bands[self.band], channels), manual)
        self.stream.check_mtu(export.interface, mtu, packing, export.piece)
        if here:
            check_host_mtu(here[0], here[2], export.dst_ip, packing, export.piece, mtu)
        if channels == 2:
            self._equal_gain()
        self._announce(channels)
        self.stream.install_own(self._wm(), packing, channels)
        self.stream.launch(export, dst_mac, packing, mtu)
        self.packing, self.packing_manual, self.channels = packing, manual, channels
        self._save()
        try:
            self._wait_producer()
        except Exception:
            self._abort_start()
            raise
        self._check_locks()

    def _wait_producer(self):
        """Running once the pump serves the mailbox and the producer has taken the go word."""
        session = self._session()
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            try:
                values, via_pump = session.ctl(read=(GO_ADDR, 1))
            except rf.CtlTimeout:
                values, via_pump = [None], False          # the stream is still taking the device
            if via_pump and values[0] == 0:
                return
            if self.stream.finished():
                raise StreamError('the stream ended at start')
            time.sleep(0.2)
        raise StreamError('the WM producer did not take the go word')

    def _abort_start(self):
        self.stream.signal_stop()
        try:
            self.stream.wait_finished()
            result = self._finish_stream()
            self._log('error', f'stream start failed: {result.text}')
        except StreamError as exc:
            self._log('error', str(exc))
            self.packing = self.packing_manual = None
            self._save()

    def _stop(self):
        if not self.packing:
            return None
        self.stream.signal_stop()
        self.stream.wait_finished()
        result = self._finish_stream()
        level = 'info' if result.ok else 'warn'
        self._log(level, f'stream {result.text}: {result.chunks} chunks in {result.seconds} s')
        sent = (result.reader or {}).get('send') or {}
        if sent.get('senders'):
            # the unit's own senders (a reduced packing, or small datagrams): what they could not carry
            short = sent.get('lapped', 0) or sent.get('errors', 0)
            self._log('warn' if short else 'info',
                      f'the unit sent {sent.get("slices", 0)} slices in {sent.get("datagrams", 0)} datagrams; '
                      f'{sent.get("lapped", 0)} skipped (the senders fell behind), {sent.get("errors", 0)} send '
                      f'errors, {sent.get("busy", 0)} waits for the link')
        exported = (result.reader or {}).get('export') or {}
        if exported.get('result'):
            self._log('warn', f'the driver\'s export ended with {exported.get("result")}: '
                              f'{exported.get("unsent", 0)} completions were not sent')
        elif exported.get('unsent'):
            # beside the unit's senders it goes on after falling behind (W1700K_SDR_EXPORT_SHORT)
            self._log('warn', f'the driver\'s export fell behind and passed over {exported.get("unsent")} '
                              f'completions (at most {exported.get("max_lag", 0)} behind)')
        split = (result.reader or {}).get('split') or {}
        if split.get('on'):
            # the radio's own chunks: how wide the shift it was given ranged (7 holds full scale)
            self._log('warn' if split.get('shift_errors') else 'info',
                      f'the radio reduced its chunks under shifts {split.get("shift_lowest")} to '
                      f'{split.get("shift_highest")} ({split.get("shift_writes", 0)} changes, '
                      f'{split.get("shift_errors", 0)} failed); the driver exported {exported.get("slices", 0)} of them')
        return result

    def _finish_stream(self):
        packing = self.packing
        try:
            result = self.stream.collect_own(self._wm(), packing)
        except (StreamError, wm.WmError) as exc:
            self._log('warn', f'stream clean-up: {exc}')
            result = Result(0, 0, 0.0, False, None)
        self.packing = self.packing_manual = None
        self._save()
        return result

    def _session(self):
        if self._ctl is None or self._ctl.process.poll() is not None:
            self._close_ctl()
            self._ctl = rf.CtlSession(self.unit, own_wm=True)
        return self._ctl

    def _close_ctl(self):
        if self._ctl is not None:
            self._ctl.close()
            self._ctl = None


# --- w1700k serve ---

def _require(args, key, kind, optional=False):
    value = args.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise ValueError(f'{key}: expected {getattr(kind, "__name__", kind)}')
    return value


def handle(controller, request):
    """Run one request; the reply fields beyond id/ok."""
    op = request.get('op')
    if op == 'status':
        return {}
    if op == 'load':
        controller.load(_require(request, 'band', int, optional=True))
    elif op == 'configure':
        controller.configure(_require(request, 'band', int), _require(request, 'sample_rate', int, True),
                             _require(request, 'lo_hz', (int, float), True),
                             _require(request, 'capture_rate', int, True))
    elif op == 'start':
        channels = _require(request, 'channels', int, optional=True)
        controller.start(_require(request, 'packing', str, optional=True), 1 if channels is None else channels)
    elif op == 'stop':
        controller.stop()
    elif op == 'tune':
        controller.tune(_require(request, 'hz', (int, float)), _require(request, 'phase', str, optional=True))
    elif op == 'rephase':
        chain = _require(request, 'chain', int, optional=True)
        controller.rephase(1 if chain is None else chain)
    elif op == 'gain':
        controller.gain(bool(request.get('agc')), _require(request, 'lna', int, True), _require(request, 'lpf', int, True))
    elif op == 'filter':
        controller.filter(_require(request, 'mhz', int))
    elif op == 'filter_follow':
        controller.filter_follow(_require(request, 'on', bool))
    elif op == 'occupancy':
        seconds, per_second = _require(request, 'seconds', (int, float), True), _require(request, 'per_second', int, True)
        threshold = _require(request, 'threshold_db', (int, float), True)
        return {'occupancy': controller.occupancy(occupancy.SECONDS if seconds is None else seconds,
                                                  occupancy.PER_SECOND if per_second is None else per_second,
                                                  occupancy.THRESHOLD_DB if threshold is None else threshold,
                                                  bool(request.get('keep')))}
    else:
        raise ValueError(f'unknown op {op!r}')
    return {}


def serve(controller, stdin=None, stdout=None, poll_seconds=2.0):
    """Newline-delimited JSON requests on stdin, one reply each, events in between (see README)."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    out_lock = threading.Lock()

    def send(obj):
        with out_lock:
            stdout.write(json.dumps(obj) + '\n')
            stdout.flush()

    controller.emit = send
    requests = queue.Queue()

    def worker():
        try:
            controller.attach()
        except Exception as exc:
            send({'event': 'log', 'level': 'error', 'text': f'cannot reach the unit: {exc}'})
        while True:
            try:
                item = requests.get(timeout=poll_seconds)
            except queue.Empty:
                try:
                    controller.poll()
                except Exception as exc:
                    send({'event': 'log', 'level': 'warn', 'text': f'stream check failed: {exc}'})
                continue
            if item is None:
                return
            try:
                extra = handle(controller, item)
                send({'id': item['id'], 'ok': True, **extra, 'state': controller.snapshot})
            except Exception as exc:
                send({'id': item['id'], 'ok': False, 'error': str(exc)})

    thread = threading.Thread(target=worker, name='w1700k-controller', daemon=True)
    thread.start()
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get('id'), int):
                raise ValueError('a request is an object with an integer id')
        except ValueError as exc:
            send({'event': 'log', 'level': 'error', 'text': f'bad request: {exc}'})
            continue
        if request.get('op') == 'status':
            send({'id': request['id'], 'ok': True, 'state': controller.snapshot})
        else:
            requests.put(request)
    requests.put(None)
    thread.join()
    controller.close()
