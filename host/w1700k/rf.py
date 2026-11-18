# SPDX-License-Identifier: MIT
"""RF control: commands to our firmware, which builds and runs each one's register program.

The host posts one RF op (tune, gain, filter, trim, status, locks, band switch) to the control
mailbox (firmware/stream/ctl.h, include/hw/mt7996/probe_page.h "RF commands") with mt7996-ctl
(W1700K_SDR_CTL). Our WM builds the command's program (firmware/radio/rfctl.c) and runs it in
slices between stream chunks, or in its main loop without a stream; nothing waits inside a slice,
so a retune does not stall a 320 Msps stream. The result words land in the RF block after the
mailbox. Samples captured while a command reconfigures the radio are invalid; each program
reports the stream chunk numbers at its start and end.

Raw ops (MMIO and A-die SPI reads and writes, delays, polls) stay available on the same mailbox
for development and register discovery (wm.py read/write/read_rf); regular operation sends only
RF commands. Band 0's die is an MT7976 (LO 2.08-2.74 GHz), bands 1 and 2 are MT7977
(LO 4.65-7.41 GHz).
"""
import json
import time

from .radio import LO_RANGE
from .unit import DEVICE

CTL_BASE = 0x0090F400
CTL_REQ, CTL_COUNT, CTL_CLEANUP = CTL_BASE, CTL_BASE + 0x0C, CTL_BASE + 0x20
CTL_OPS, CTL_RES, CTL_MAX = CTL_BASE + 0x40, CTL_BASE + 0x840, 128
STATE_WORDS = ('req', 'ack', 'status', 'count', 'cycles', 'served', 'calls', 'seen', 'cleanup', 'soft',
               'chunk0', 'chunk1')
IOCTL_WORDS = 64                  # W1700K_SDR_CTL_MAX
END, W32, RMW32, R32, DELAY, POLL, AW, AR, ARMW, APOLL, SAR, RF = range(12)
SOFT = 0x100                      # a POLL/APOLL timeout is counted, not fatal
STATUS = {1: 'bad op', 2: 'timeout', 3: 'too many ops', 4: 'RF-SPI semaphore not granted',
          5: 'SPI master busy', 6: 'SPI transfer did not finish', 7: 'RF command or argument refused',
          10: 'not run: our WM was at something else when it was posted (a radio command, a call, a stream '
              'starting or ending) and passed over it; post it again',
          9: 'refused by the guard: it reaches a register our WM keeps from raw programs while it is locked '
             '(what could transmit, an eFuse, an access that wedges a die). For development, unlock it: '
             '"raw_access": true in the configuration'}
# RF commands (include/hw/mt7996/probe_page.h "RF commands"): op RF, a = command | band << 8.
RF_TUNE, RF_GAIN, RF_FILTER, RF_TRIM, RF_STATUS, RF_LOCKS, RF_BAND = range(1, 8)
RF_GAIN_AGC = 0xFFFFFFFF
RF_UNLOCKED = 8                   # an RF op's result: the tune's VCO did not lock
RF_BLOCK = CTL_BASE + 0xA40       # LO Hz [31:0], LO Hz [63:32], ops, outs, then the result words
RF_OUT_MAX = 16
TOP = 15
CORE_HZ = 720e6

# LO_RANGE: radio.py (each band's measured lock range)
CHAINS = {0: 4, 1: 4, 2: 5}       # RX chains per band
FILTER_CODE = {20: 0, 40: 1, 80: 2, 160: 3, 320: 4}
# Capture route grant words a raw write must not set (include/hw/mt7996/cap_route.h HAZARD: the MCU hangs).
ROUTE_GRANTS = {0x88009000: 0xFFFFFFFF, 0x88009004: 0x00FFFFFF, 0x88009008: 0xFFFFFFFF}
# _status's VCO decode: the SX words count in units of the PFD (MT7977 160 MHz, MT7976 80 MHz).
FPFD, FPFD_7976 = 160e6, 80e6


class CtlError(RuntimeError):
    pass


class NotStreaming(CtlError):
    pass


class CtlTimeout(CtlError):
    """The ioctl timed out: the device was busy (e.g. a stream still setting up)."""


# --- ops ---

def _op(code, arg=0, flags=0):
    if not 0 <= arg < 1 << 20:
        raise ValueError('op argument 0..2^20-1 (us)')
    return code | flags << 8 | arg << 12


def _mmio_write_allowed(addr, value):
    """Refuse a write that sets a route grant bit (ROUTE_GRANTS): that hangs the MCU."""
    mask = ROUTE_GRANTS.get(addr & ~0x3)
    if mask is not None and value & mask:
        raise ValueError(f'{addr:#010x}={value:#x} drives a route grant, which hangs the MCU')


def w32(addr, value):
    _mmio_write_allowed(addr, value)
    return (W32, addr, value & 0xFFFFFFFF, 0)


def rmw32(addr, mask, value):
    if value & ~mask:
        raise ValueError('value outside mask')
    _mmio_write_allowed(addr, value)
    return (RMW32, addr, mask, value)


def r32(addr):
    return (R32, addr, 0, 0)


def delay(us):
    return (_op(DELAY, us), 0, 0, 0)


def reg(die, k, index):
    if die not in (0, 1, 2) or not (0 <= k <= 4 or k == TOP) or not 0 <= index <= 0xFFF:
        raise ValueError('die 0..2, chain 0..4 or TOP, 12-bit index')
    return die << 16 | k << 12 | index


def aw(r, value):
    return (AW, r, value & 0xFFFFFFFF, 0)


def ar(r):
    return (AR, r, 0, 0)


def armw(r, mask, value):
    if value & ~mask:
        raise ValueError('value outside mask')
    return (ARMW, r, mask, value)


def apoll(r, mask, value, timeout_us=1000, soft=False):
    return (_op(APOLL, timeout_us, SOFT >> 8 if soft else 0), r, mask, value)


def sar(die):
    return (SAR, die, 0, 0)


def check_program(ops):
    """Refuse a program that sets a route grant bit, however it was built."""
    for op in ops:
        if op[0] & 0xFF == W32:
            _mmio_write_allowed(op[1], op[2])
        elif op[0] & 0xFF == RMW32:
            _mmio_write_allowed(op[1], op[3])


# --- the mailbox client ---

class CtlSession:
    """One `mt7996-ctl --serve` on the unit: a request per line, a JSON answer per line. With
    `own_wm` the programs run on our own WM, which serves the mailbox without a stream."""

    def __init__(self, unit, own_wm=False):
        self.process = unit.popen(f'{unit.tool("mt7996-ctl")} {DEVICE} --serve')
        self.own_wm = own_wm
        self.soft = 0
        self.cycles = 0
        self.chunks = (0, 0)

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(10)
            except Exception:
                self.process.kill()

    def ctl(self, writes=(), read=None, timeout_ms=2000):
        """One W1700K_SDR_CTL: writes in order, then `read` = (address, count). (values, via_pump)."""
        words = [f'{a:#x}={v:#x}' for a, v in writes]
        if read:
            words += ['--read', f'{read[0]:#x}', str(read[1])]
        try:
            self.process.stdin.write(' '.join(['--timeout', str(timeout_ms)] + words) + '\n')
            self.process.stdin.flush()
        except OSError as exc:
            raise CtlError(f'mt7996-ctl session ended: {exc}')
        line = self.process.stdout.readline()
        if not line:
            raise CtlError('mt7996-ctl session ended')
        answer = json.loads(line)
        if not answer.get('ok'):
            errno = answer.get('errno') or ''
            kind = CtlTimeout if 'timed out' in errno.lower() else CtlError
            raise kind(f'mt7996-ctl failed: {errno or answer}')
        return [int(x, 16) for x in answer.get('read', [])], bool(answer.get('via_pump'))

    def state(self):
        values, via_pump = self.ctl(read=(CTL_REQ, len(STATE_WORDS)))
        return dict(zip(STATE_WORDS, values)), via_pump

    def streaming(self):
        return self.state()[1]

    def post(self, ops, cleanup=0, guard=0):
        """Write a program and a new request number. A hard failure at an op in [guard, cleanup)
        jumps to op `cleanup` and runs the rest (0: no cleanup)."""
        if not 0 < len(ops) <= CTL_MAX:
            raise ValueError(f'1..{CTL_MAX} ops per program')
        check_program(ops)
        st, via_pump = self.state()
        if not via_pump and not self.own_wm:
            raise NotStreaming('no stream is running; RF programs run inside the stream producer')
        if st['req'] != st['ack']:
            raise CtlError(f'a program is still running (request {st["req"]}, acknowledged {st["ack"]})')
        words = []
        for k, op in enumerate(ops):
            words += [(CTL_OPS + 16 * k + 4 * i, op[i] & 0xFFFFFFFF) for i in range(4)]
        for i in range(0, len(words), IOCTL_WORDS):
            self.ctl(words[i:i + IOCTL_WORDS])
        req = (st['req'] + 1) & 0xFFFFFFFF or 1
        self.ctl([(CTL_COUNT, len(ops)), (CTL_CLEANUP, cleanup | guard << 16), (CTL_REQ, req)])
        return req

    def wait(self, req, count, timeout=2.0):
        """(status, results of the first `count` ops) once `req` is acknowledged."""
        deadline = time.monotonic() + timeout
        while True:
            st, _ = self.state()
            if st['ack'] == req:
                break
            if time.monotonic() > deadline:
                raise CtlError(f'program {req} not acknowledged in {timeout:.1f} s')
            time.sleep(0.002)
        results = []
        for i in range(0, count, IOCTL_WORDS):
            results += self.ctl(read=(CTL_RES + 4 * i, min(IOCTL_WORDS, count - i)))[0]
        self.soft, self.cycles, self.chunks = st['soft'], st['cycles'], (st['chunk0'], st['chunk1'])
        return st['status'], results

    def run(self, ops, timeout=2.0):
        """Run a program (split at CTL_MAX ops); every op's result. Raises on a failed op."""
        results = []
        for i in range(0, len(ops), CTL_MAX):
            part = ops[i:i + CTL_MAX]
            status, res = self.wait(self.post(part), len(part), timeout)
            if status:
                raise CtlError(f'op {i + (status >> 8)}: {STATUS.get(status & 0xFF, status)}')
            results += res
        return results


class SynthUnlocked(CtlError):
    """A band's synthesizer reports no lock. A firmware reload does not reset the A-dies; a
    synthesizer that stays unlocked needs a power cycle of the unit."""

    def __init__(self, band, detail=''):
        self.band = band
        super().__init__(f'band {band} synthesizer unlocked{": " + detail if detail else ""}; '
                         'if it stays unlocked, power-cycle the unit')


def _check_gain(lna, lpf):
    if (lna is None) != (lpf is None) or lna is not None and not (0 <= lna <= 4 and 0 <= lpf <= 9):
        raise ValueError('gain: the AGC, or LNA index 0..4 and LPF index 0..9')


def _status(band, words):
    """status()'s words decoded: SX INT, FRAC, CFG, status, the frequency note, every chain's filter
    code, then every chain's gain word."""
    n = CHAINS[band]
    a4b, a4a = words[0], words[1]
    vco = ((a4b >> 20 & 0x3ff) + (a4a & 0xffffff) / (1 << 24)) * (FPFD_7976 if band == 0 else FPFD)
    return {'band': band, 'vco_hz': int(round(vco)), 'vco': ['locked', 'too low', 'too high', '?'][words[3] & 3],
            'firmware_lo_khz': words[4], 'filter_codes': words[5:5 + n],
            'gain': [_describe_gain(g) for g in words[5 + n:]]}


def _describe_gain(word):
    if not word:
        return 'agc'
    if word & 2:
        return f'lna {word >> 16 & 7} lpf {word >> 20 & 15}'
    if word & 1:
        return f'lna {word >> 8 & 7} lpf {word >> 12 & 15}'
    return f'{word:#010x}'


# --- RF commands (firmware/radio/rfctl.h): no register named here ---

def rf_op(command, band=0, b=0, c=0):
    """One RF op: our WM builds the command's register program and runs it inside the op."""
    return (RF, command | band << 8, b & 0xFFFFFFFF, c & 0xFFFFFFFF)


def rf_command(session, command, band=0, b=0, c=0, timeout=2.0):
    """Run one RF command: (result, the RF block's result words, LO Hz). The result is 0 or
    RF_UNLOCKED; a refused or failed command raises CtlError (naming the op of its program)."""
    if not session.own_wm:
        raise CtlError('RF commands need our own WM (it builds their programs)')
    status, (res,) = session.wait(session.post([rf_op(command, band, b, c)]), 1, timeout)
    if status:
        where = f' at op {res >> 8} of its program' if res >> 8 else ''
        raise CtlError(f'RF command {command} band {band}: {STATUS.get(status & 0xFF, status)}{where}')
    words, _ = session.ctl(read=(RF_BLOCK, 4 + RF_OUT_MAX))
    return res, words[4:4 + min(words[3], RF_OUT_MAX)], words[0] | words[1] << 32


def tune(session, band, f_hz):
    """Retune band's LO to f_hz (the synthesizer presets of its frequency group, INT/FRAC, the VCO
    band search, the locks): the LO the words give, Hz. Raises SynthUnlocked when the VCO does not
    lock."""
    f_hz = int(f_hz)
    lo, hi = LO_RANGE[band]
    if not lo <= f_hz <= hi:
        raise ValueError(f'band {band} tunes {lo / 1e9:.2f}-{hi / 1e9:.2f} GHz')
    res, out, actual = rf_command(session, RF_TUNE, band, f_hz, f_hz >> 32)
    if res == RF_UNLOCKED:
        vco = out[0] & 3
        raise SynthUnlocked(band, f'the VCO did not lock at {f_hz / 1e6:.3f} MHz '
                                  f'({"too low" if vco == 1 else "too high"})')
    return actual


def set_gain(session, band, lna=None, lpf=None):
    """(lna, lpf) forced on every chain, or the AGC (both None). Returns the read-back words."""
    _check_gain(lna, lpf)
    agc = lna is None and lpf is None
    return rf_command(session, RF_GAIN, band, RF_GAIN_AGC if agc else lna, 0 if agc else lpf)[1]


def set_filter(session, band, mhz):
    """Every chain's analog RX filter; returns every chain's filter status word (the code: band 0's
    320 MHz is the MT7976's code 3 widened, so it reads 3). Band 0's 320 needs the bring-up."""
    if mhz not in FILTER_CODE:
        raise ValueError(f'filter: one of {", ".join(map(str, FILTER_CODE))} MHz')
    return rf_command(session, RF_FILTER, band, mhz)[1]


def set_trim(session, band, code):
    """Filter RC trim 0..31 on every chain (larger is a lower corner): every chain's trim word."""
    if not 0 <= code <= 31:
        raise ValueError('trim code 0..31')
    return rf_command(session, RF_TRIM, band, code)[1]


def status(session, band):
    """Synthesizer, filter codes and gain words of a band, as read from the hardware."""
    return _status(band, rf_command(session, RF_STATUS, band)[1])


def synth_locks(session):
    """Whether each band's synthesizer reports lock."""
    return [v & 3 == 0 for v in rf_command(session, RF_LOCKS)[1]]


def select_band(session, band):
    """Switch the captured band on the running ring (ICAPCR8 of every ICAP instance): ICAP0's write
    pointer (before, after) the switch, which places it in the ring."""
    if band not in CHAINS:
        raise ValueError('band 0, 1 or 2')
    out = rf_command(session, RF_BAND, band)[1]
    return out[0], out[-1]
