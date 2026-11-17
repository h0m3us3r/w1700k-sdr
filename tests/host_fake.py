# SPDX-License-Identifier: MIT
"""A simulated unit for the host package tests, running our firmware. FakeUnit records every
command and answers as the unit side does (the driver reload, mt7996-probe --acquire,
mt7996-download with RESET_MT and the containers, mt7996-ctl --serve, the detached mt7996-stream
job); behind the mailbox page, FakeDevice is our WM as the host sees it: its status block, the
radio commands (firmware/radio/radio_cmd.h), the control interpreter with raw ops and RF ops
(firmware/stream/ctl.h, include/hw/mt7996/probe_page.h), the stream producer and our WA's
status block. Not a test module itself; header_defines() serves the tests that check the host's
mirrors of the C headers."""
import hashlib
import json
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))

from w1700k import radio, rf, stream, wm  # noqa: E402
from w1700k.config import Config, ExportConfig, UnitConfig  # noqa: E402
from w1700k.unit import OUT, StepResult, UnitError  # noqa: E402

BOOT_ID = '01234567-89ab-cdef-0123-456789abcdef'

# firmware/radio/radio_cmd.h
RADIO_OK, RADIO_ERR_OP, RADIO_ERR_ARG, RADIO_ERR_ORDER, RADIO_ERR_HW = range(5)
IDENTIFIED, PATCH_INIT, COMMON, POOL, POST, UP = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 15, 1 << 16


def BAND_UP(b):
    return 1 << (4 + b)


def RF_UP(d):
    return 1 << (8 + d)


def CHAN(b):
    return 1 << (20 + b)


def CAPTURE(b):
    return 1 << (24 + b)


BROUGHT_UP = IDENTIFIED | PATCH_INIT | COMMON | POOL | POST | UP | sum(BAND_UP(b) | RF_UP(b) for b in range(3))
RADIO_OP_NAMES = {code: name for name, code in wm.RADIO_OPS.items()}
ARITY = {'selftest': 0, 'identify': 0, 'bringup': 0, 'setup': 4, 'step': 2, 'capture': 2, 'select': 1,
         'stream': 1, 'rate': 2, 'dcoc': 1}
EE_BUFFER = 0xE0100000            # where our WM's radio_ee lands in CRAM (any address will do)
RF_BUILD = 0xE0050000             # its RF command builder
# probe_page.h CTL status codes
CTL_OK, CTL_BAD_OP, CTL_TIMEOUT, CTL_TOO_MANY, CTL_BAD_ARG = 0, 1, 2, 3, 7
CTL_ACK, CTL_STATUS, CTL_SOFT, CTL_SERVED = rf.CTL_BASE + 4, rf.CTL_BASE + 8, rf.CTL_BASE + 0x24, rf.CTL_BASE + 0x14
ICAP0_CR9 = 0x800230B4            # ICAP0's write pointer (hw/mt7996/icap.h)
RING = 0x100000


class FakeDevice:
    """The MT7996 behind the driver, running our firmware once downloaded."""

    def __init__(self):
        self.mem = {}                     # MCU words: the mailbox page, the EEPROM buffer, MMIO
        self.adie = {}                    # A-die registers by die << 16 | k << 12 | index
        self.wm_up = False                # our WM runs its main loop
        self.wa_up = False
        self.downloads = []               # per download step: ('acquire',), ('--reset',), ('rom',) for the
                                          # ROM's read, or (file, *flags)
        self.download_failure = None      # a container file name whose download fails
        self.rom = ROM                    # the boot ROM, as a host reads it
        self.rom_read_failure = None      # an address at which its read fails
        self.radio_commands = []          # (name, args) per radio command our WM took
        self.radio_failures = {}          # name -> (res, fail code, fail arg): its next command fails so
        self.channels = {}                # band -> (LO Hz, bw class) of its SETUP, the class after a RATE
        # where each band's VCO locks (unit A's dies, 2026-10-11): the controller's probe finds it
        self.lock_span = {0: (2_080_781_250, 2_748_046_875), 1: (4_646_562_500, 7_509_048_461),
                          2: (4_707_151_793, 7_576_215_820)}
        self.icap_band = None             # ICAPCR8 of the three ICAPs
        self.route_armed = False
        self.stream_packing = None        # the STREAM command's packing while it runs
        self.pump = None                  # mt7996-stream: None, 'waiting' (no producer took go), 'streaming'
        self.pump_argv = None
        self.wa_stuck = False             # the WA does not leave the packer at the stream's end
        self.programs = []                # every control program run (lists of ops)
        self.rf_ops = []                  # (command, band, b, c) per RF op
        self.sx_status = [0, 0, 0]        # each band's SX status [1:0] (0 locked); a reload keeps it
        self.unlocking = {}               # band -> the SX status its tunes leave (1 too low, 2 too high)
        self.lo, self.gain, self.filter, self.trim = {}, {}, {}, {}
        self.cr9 = 0x2000
        self.ctl_timeouts = 0             # the next N ctl ioctls time out
        self.setup_timeouts = 0           # ... and N after the pump starts (it takes the device)
        self.faulting = set()             # addresses whose R32 traps (our WM skips it and goes on)
        self.dropped = set()              # addresses whose writes (host or W32 op) do not land
        self.calls = []                   # (fn, args) our WM was asked to call
        self.mtu = 9216
        self.link_mbps = 10000            # eth1's negotiated speed; None: not reported

    @property
    def streaming(self):
        return self.pump == 'streaming'

    # --- our WM's status block ---

    def st(self, name):
        return self.mem.get(wm.STATUS + 4 * wm.STATUS_WORDS.index(name), 0)

    def set_st(self, name, value):
        self.mem[wm.STATUS + 4 * wm.STATUS_WORDS.index(name)] = value & 0xFFFFFFFF

    def eeprom(self):
        """The image our WM's EEPROM buffer holds."""
        return b''.join(struct.pack('<I', self.mem.get(EE_BUFFER + 4 * i, 0)) for i in range(wm.EEPROM_BYTES // 4))

    def reset(self):
        """RESET_MT: the cores stop; our WM, its mailbox and buffers are gone. The A-dies keep their
        state (sx_status)."""
        self.mem, self.wm_up, self.wa_up = {}, False, False
        self.stream_packing, self.route_armed, self.icap_band = None, False, None

    def boot_wm(self):
        """Our WM after the set's download (wm_main): status block, mailbox served."""
        for name in wm.STATUS_WORDS:
            self.set_st(name, 0)
        self.set_st('version', 6)
        self.set_st('radio_ee', EE_BUFFER)
        self.set_st('rf_build', RF_BUILD)
        self.set_st('state', wm.STATE_RUNNING)
        self.set_st('magic', wm.MAGIC)
        self.mem[CTL_ACK] = self.mem.get(rf.CTL_REQ, 0)
        self.wm_up = True

    def write(self, addr, value):
        """A host write through W1700K_SDR_CTL, and what our WM's main loop makes of it."""
        if addr in self.dropped:
            return
        self.mem[addr] = value
        if not self.wm_up:
            return
        if addr == rf.CTL_REQ and value and value != self.mem.get(CTL_ACK):
            self._run_program()
        elif addr == wm.CALL and value:
            args = [self.mem.get(wm.ARGS + 4 * i, 0) for i in range(4)]
            self.calls.append((value, args))
            self.mem[wm.CALL] = 0
            self.set_st('ret', sum(args))
            self.set_st('calls', self.st('calls') + 1)
        elif addr == wm.WA_START and value == wm.WA_START_REQ:
            self._start_wa()
        elif addr == wm.RADIO_REQ and value != self.st('radio_ack') and not self.stream_packing:
            self._radio()

    def read(self, addr):
        if self.wm_up and addr == wm.STATUS:
            self.set_st('beats', self.st('beats') + 1)
        if self.wa_up and addr == wm.WA_STATUS:
            at = wm.WA_STATUS + 4 * wm.WA_STATUS_WORDS.index('beats')
            self.mem[at] = self.mem.get(at, 0) + 1
        return self.mem.get(addr, 0)

    def _start_wa(self):
        self.mem[wm.WA_START] = wm.WA_STARTED
        self.mem[wm.WA_STATUS] = wm.WA_MAGIC
        self.wa_up = True

    # --- radio commands ---

    def _radio(self):
        name = RADIO_OP_NAMES.get(self.st('radio_op'), self.st('radio_op'))
        args = [self.st(f'radio_arg{i}') for i in range(4)]
        self.radio_commands.append((name, tuple(args[:ARITY.get(name, 4)])))
        if name in self.radio_failures:
            res, code, arg = self.radio_failures.pop(name)
            self._radio_ack(res, code, arg)
        elif name == 'stream':
            res = self._stream(args[0])
            if res is not None:                           # else acknowledged when the stream ends
                self._radio_ack(res)
        else:
            self._radio_ack(self._command(name, args))

    def _radio_ack(self, res, fail_code=0, fail_arg=0):
        self.set_st('radio_res', res)
        self.set_st('radio_fail_code', fail_code)
        self.set_st('radio_fail_arg', fail_arg)
        self.set_st('radio_fails', 1 if fail_code else 0)
        self.set_st('radio_us', 1000)
        self.set_st('radio_ack', self.st('radio_req'))

    def _command(self, name, args):
        """radio.c's radio_command: the result, the state word updated on success."""
        state, a = self.st('radio_state'), args
        if name == 'selftest':
            return wm.RADIO_SELFTEST_MAGIC
        if name == 'identify':
            state |= IDENTIFIED
        elif name == 'bringup':
            state = BROUGHT_UP
        elif name == 'step':
            steps = {v: k for k, v in wm.RADIO_STEPS.items()}
            step, arg = steps.get(a[0]), a[1]
            need, bits = {'patch_init': (0, IDENTIFIED | PATCH_INIT), 'pool': (0, POOL),
                          'common': (PATCH_INIT, COMMON), 'band': (COMMON, BAND_UP(arg)),
                          'rf_init': (PATCH_INIT, RF_UP(arg)), 'post': (COMMON, POST),
                          'dcoc': (CHAN(arg), 0)}.get(step, (None, 0))
            if need is None or step in ('band', 'rf_init', 'dcoc') and arg > 2:
                return RADIO_ERR_ARG
            if state & need != need:
                return RADIO_ERR_ORDER
            state |= bits
        elif name == 'setup':
            band, lo, hi, bwf = a
            hz = hi << 32 | lo
            if band > 2 or bwf & 0xFF > (3 if band == 0 else 4) or not radio.LO_RANGE[band][0] <= hz <= radio.LO_RANGE[band][1]:
                return RADIO_ERR_ARG
            if not state & UP:
                return RADIO_ERR_ORDER
            state = state & ~(CHAN(band) | CAPTURE(band)) | CHAN(band)
            self.channels[band] = (hz, bwf & 0xFF)
            self.lo[band] = radio.actual_lo(band, hz)
            self.sx_status[band] = self.unlocking.get(band, self.sx_status[band])
        elif name == 'rate':                          # the sample rate alone: LO and filter stay
            band, bw = a[:2]
            if band > 2 or bw > (3 if band == 0 else 4):
                return RADIO_ERR_ARG
            if not state & CHAN(band):
                return RADIO_ERR_ORDER
            state &= ~CAPTURE(band)
            self.channels[band] = (self.channels[band][0], bw)
        elif name == 'dcoc':
            if a[0] > 2:
                return RADIO_ERR_ARG
            if not state & CHAN(a[0]):
                return RADIO_ERR_ORDER
        elif name == 'capture':
            if a[0] > 2 or a[1] > 4:
                return RADIO_ERR_ARG
            if not state & CHAN(a[0]):
                return RADIO_ERR_ORDER
            state |= CAPTURE(a[0])
        elif name == 'select':
            if a[0] > 2:
                return RADIO_ERR_ARG
            self.icap_band = a[0]
        else:
            return RADIO_ERR_OP
        self.set_st('radio_state', state)
        return RADIO_OK

    # --- streams ---

    def _stream(self, packing):
        """STREAM: the WA started if the schedule has it pack, the route and ICAP0 armed, the
        producer waiting for the go word; None while it runs."""
        names = {code: name for name, code in wm.RADIO_PACKING.items()}
        if packing not in names:
            return RADIO_ERR_ARG
        self.stream_packing = names[packing]
        if self.stream_packing != 'raw':
            self._start_wa()
        self.route_armed = True
        for i in range(4):
            self.mem[stream.DONE_AT + 4 * i] = 0
        self.mem[stream.GO_ADDR] = 0
        return None

    def pump_start(self, argv):
        """mt7996-stream writes the go word; a waiting producer takes it (writes 0)."""
        self.pump_argv = argv
        self.ctl_timeouts = self.setup_timeouts
        self.mem[stream.GO_ADDR] = stream.GO_MAGIC
        self.pump = 'waiting'
        if self.stream_packing:
            self.mem[stream.GO_ADDR] = 0
            self.pump = 'streaming'

    def pump_end(self, status):
        """The pump ends and writes the stop word; the producer winds down with `status` and writes
        its result page; our WM releases the route and acknowledges the STREAM command. False if
        no pump ran."""
        if self.pump is None:
            return False
        was, self.pump = self.pump, None
        self.mem[stream.GO_ADDR] = stream.STOP_MAGIC
        if was == 'streaming':
            wa = self.stream_packing != 'raw'                      # the WA packs in half and full
            exited = wa and not self.wa_stuck
            res32 = dict.fromkeys(stream.RES32, 0)
            res32.update(status=status, wa_status=(1 | exited << 8) if wa else 0)
            res64 = dict.fromkeys(stream.RES64, 0)
            res64.update(chunks=1000, cycles=720_000_000)
            words = [stream.DONE_MAGIC, status, 0, 0] + [res32[k] for k in stream.RES32]
            for k in stream.RES64:
                words += [res64[k] & 0xFFFFFFFF, res64[k] >> 32]
            for i, w in enumerate(words):
                self.mem[stream.DONE_AT + 4 * i] = w
            if not wa or exited:
                self.route_armed = False
            self.stream_packing = None
            self._radio_ack(status)
            if self.st('radio_req') != self.st('radio_ack'):      # posted meanwhile: served now
                self._radio()
        return True

    # --- the control interpreter ---

    def _run_program(self):
        count = self.mem.get(rf.CTL_COUNT, 0)
        if count > rf.CTL_MAX:
            return self._ctl_finish(CTL_TOO_MANY, 0)
        ops = [tuple(self.mem.get(rf.CTL_OPS + 16 * k + 4 * i, 0) for i in range(4)) for k in range(count)]
        self.programs.append(ops)
        status = soft = 0
        for k, (op, a, b, c) in enumerate(ops):
            e, res = self._op(op, a, b, c)
            self.mem[rf.CTL_RES + 4 * k] = res & 0xFFFFFFFF
            if e == CTL_TIMEOUT and op & rf.SOFT:
                soft += 1
            elif e:
                status = e | k << 8
                break
        self._ctl_finish(status, soft)

    def _ctl_finish(self, status, soft):
        self.mem[CTL_STATUS] = status
        self.mem[CTL_SOFT] = soft
        self.mem[CTL_SERVED] = self.mem.get(CTL_SERVED, 0) + 1
        self.mem[CTL_ACK] = self.mem[rf.CTL_REQ]

    def _op(self, op, a, b, c):
        """One op: (status, result)."""
        code, mem, adie = op & 0xFF, self.mem, self.adie
        if code in (rf.W32, rf.RMW32):
            value = b if code == rf.W32 else (mem.get(a, 0) & ~b) | c
            mask = rf.ROUTE_GRANTS.get(a & ~3, 0)
            assert not value & mask, f'{a:#x}={value:#x} drives a route grant: the MCU hangs'
            if a not in self.dropped:
                mem[a] = value & 0xFFFFFFFF
            return CTL_OK, 0
        if code == rf.R32:
            if a in self.faulting:
                self.set_st('trap_cause', 5)
                self.set_st('trap_tval', a)
                self.set_st('traps', self.st('traps') + 1)
                return CTL_OK, 0xBAD
            return CTL_OK, self._cr9() if a == ICAP0_CR9 else mem.get(a, 0)
        if code == rf.DELAY:
            return CTL_OK, 0
        if code == rf.POLL:
            v = mem.get(a, 0)
            return (CTL_OK if v & b == c else CTL_TIMEOUT), v
        if code == rf.AW:
            adie[a] = b
            return CTL_OK, 0
        if code == rf.AR:
            return CTL_OK, adie.get(a, 0)
        if code == rf.ARMW:
            adie[a] = (adie.get(a, 0) & ~b) | c
            return CTL_OK, adie[a]
        if code == rf.APOLL:
            v = adie.get(a, 0)
            return (CTL_OK if v & b == c else CTL_TIMEOUT), v
        if code == rf.SAR:
            return CTL_OK, 0x90
        if code == rf.RF:
            return self._rf(a & 0xFF, a >> 8 & 0xFF, b, c)
        return CTL_BAD_OP, 0

    def _cr9(self):
        if self.streaming:
            self.cr9 = (self.cr9 + 0x2000) % RING
        return self.cr9

    def _rf(self, command, band, b, c):
        """An RF op as our WM's builder and interpreter run it: (status, result), the RF block
        written (probe_page.h "RF commands")."""
        self.rf_ops.append((command, band, b, c))
        n = rf.CHAINS.get(band)
        if n is None and command != rf.RF_LOCKS:
            return CTL_BAD_ARG, CTL_BAD_ARG
        lo, res = 0, CTL_OK
        if command == rf.RF_TUNE:
            hz = c << 32 | b
            if not rf.LO_RANGE[band][0] <= hz <= rf.LO_RANGE[band][1]:
                return CTL_BAD_ARG, CTL_BAD_ARG
            lo_ok, hi_ok = self.lock_span[band]
            self.sx_status[band] = self.unlocking.get(band, 0) or (0 if lo_ok <= hz <= hi_ok else 2)
            self.lo[band] = lo = radio.actual_lo(band, hz)
            ops, outs = 33 if band == 0 else 100, [self.sx_status[band], 0x40, 0, 0x10000, 0]
            res = rf.RF_UNLOCKED if self.sx_status[band] & 3 else CTL_OK
        elif command == rf.RF_GAIN:
            if b == rf.RF_GAIN_AGC:
                word = 0
            elif b <= 4 and c <= 9:
                word = 1 | b << 8 | c << 12 | 2 | b << 16 | c << 20
            else:
                return CTL_BAD_ARG, CTL_BAD_ARG
            self.gain[band] = word
            ops, outs = 2 * n, [word] * n
        elif command == rf.RF_FILTER:
            if b not in rf.FILTER_CODE:
                return CTL_BAD_ARG, CTL_BAD_ARG
            self.filter[band] = rf.FILTER_CODE[b]
            ops, outs = 2 * n, [self.filter[band]] * n
        elif command == rf.RF_TRIM:
            if b > 31:
                return CTL_BAD_ARG, CTL_BAD_ARG
            self.trim[band] = b
            ops, outs = n, [b << 8 | 0x10000] * n
        elif command == rf.RF_STATUS:
            outs = self._sx_words(band) + [self.sx_status[band], round(self.lo.get(band, 0) / 1000)]
            outs += [self.filter.get(band, 0)] * n + [self.gain.get(band, 0)] * n
            ops = len(outs)
        elif command == rf.RF_LOCKS:
            ops, outs = 3, list(self.sx_status)
        elif command == rf.RF_BAND:
            before = self._cr9()
            self.icap_band = band
            ops, outs = 5, [before, 0, 0, 0, self._cr9()]
        else:
            return CTL_BAD_ARG, CTL_BAD_ARG
        block = [lo & 0xFFFFFFFF, lo >> 32, ops, len(outs)] + outs + [0] * (rf.RF_OUT_MAX - len(outs))
        for i, w in enumerate(block):
            self.mem[rf.RF_BLOCK + 4 * i] = w & 0xFFFFFFFF
        return CTL_OK, res

    def _sx_words(self, band):
        """SX INT and FRAC for the band's LO (VCO = LO x 16/5 at 80 MHz on the MT7976, x 8/5 or 4/3
        at 160 MHz on the MT7977), and CFG."""
        lo = self.lo.get(band, 0)
        n = lo * 16 / 5 / 80e6 if band == 0 else lo * (8 / 5 if lo < 5926e6 else 4 / 3) / 160e6
        return [int(n) << 20, 0x80000000 | int(round((n - int(n)) * (1 << 24))), 0x15]


class FakeCtlProcess:
    """mt7996-ctl --serve: one line per W1700K_SDR_CTL, answered in JSON."""

    def __init__(self, device):
        self.device = device
        self.lines = []
        self.alive = True
        parent = self

        class In:
            def write(self, text):
                if not parent.alive:
                    raise BrokenPipeError('mt7996-ctl ended')
                parent.lines.append(parent._answer(text))

            def flush(self):
                pass

            def close(self):
                parent.alive = False

        class Out:
            def readline(self):
                return parent.lines.pop(0) if parent.lines and parent.alive else ''

        self.stdin, self.stdout = In(), Out()

    def _answer(self, line):
        d = self.device
        if d.ctl_timeouts:
            d.ctl_timeouts -= 1
            return '{"at":"ctl","ok":false,"errno":"Connection timed out"}\n'
        words, reads, i = line.split(), [], 0
        while i < len(words):
            if words[i] == '--timeout':
                i += 2
            elif words[i] == '--read':
                addr, n = int(words[i + 1], 0), int(words[i + 2])
                reads = [d.read(addr + 4 * k) for k in range(n)]
                i += 3
            else:
                addr, value = (int(x, 0) for x in words[i].split('='))
                d.write(addr, value)
                i += 1
        return json.dumps({'at': 'ctl', 'ok': True, 'via_pump': int(d.streaming),
                           'read': [f'{v:#010x}' for v in reads]}) + '\n'

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


class FakeUnit:
    def __init__(self, device=None, bin_dir='/fake'):
        self.device = device or FakeDevice()
        self.bin_dir = bin_dir
        self.files = {}
        self.log = []                                     # every shell command and client argv
        self.module_replacements = 0
        self.sessions = []                                # every mt7996-ctl --serve started
        self.target = 'root@unit'
        self.boot_id = BOOT_ID                            # changes when the unit reboots
        self.calibration = None                           # the EEPROM file in its own partition, if any

    def tool(self, name):
        return f'{self.bin_dir}/{name}'

    def _done(self, stdout=b'', rc=0):
        return subprocess.CompletedProcess([], rc, stdout, b'')

    def end_stream(self, status=7):
        """The stream job ends (SIGINT: stopped; or on its own): the pump's summary and done files."""
        if self.device.pump_end(status):
            summary = {'at': 'stream', 'errno': 0, 'result': 0, 'buffers': 1000}
            self.files[stream.OUT_FILE] = (json.dumps(summary, separators=(',', ':')) + '\n').encode()
            self.files[stream.DONE_FILE] = b'done\n'

    def run(self, command, *, input=None, timeout=60):
        self.log.append(command)
        d = self.device
        if command.startswith('cat /proc/sys/kernel/random/boot_id'):
            uptime = f'{time.monotonic():.2f}\n' if '/proc/uptime' in command else ''
            return self._done((self.boot_id + '\n' + uptime).encode() + self.files.get('/tmp/w1700k-state.json', b''))
        if '"calibration"' in command:                    # radio.py unit_eeprom
            return self._done(self.calibration or b'', rc=0 if self.calibration else 1)
        if command.startswith('sha256sum'):
            lines = [f'{hashlib.sha256(v).hexdigest()}  {k}' for k, v in self.files.items() if k.endswith('.bin')]
            return self._done('\n'.join(lines).encode())
        if command == 'sh -s':
            script = input.decode()
            self.log.append(script)
            if 'insmod' in script:                        # the driver reload (radio.py replace_module)
                self.module_replacements += 1
                self.end_stream()
                for session in self.sessions:
                    session.kill()
                d.reset()
            return self._done()
        if 'setsid sh -c' in command:
            job = shlex.split(command.split('setsid sh -c ', 1)[1])[0]
            d.pump_start(shlex.split(job.split(' > ')[0]))
            for path in (stream.OUT_FILE, stream.DONE_FILE):
                self.files.pop(path, None)
            return self._done()
        if command.startswith('test -e'):
            return self._done(rc=0 if command.split()[-1] in self.files else 1)
        if 'kill -INT' in command:
            self.end_stream()
            return self._done()
        if command == 'pidof mt7996-stream > /dev/null':
            return self._done(rc=0 if d.pump else 1)
        match = re.match(r'cat /sys/class/net/(\S+)/mtu ', command)
        if match:
            speed = '' if d.link_mbps is None else f'{d.link_mbps}\n'
            return self._done(f'{d.mtu}\nup\n{speed}'.encode() if match.group(1) == 'eth1' else b'')
        if command.startswith('rm -f'):
            for path in command.split()[2:]:
                self.files.pop(path, None)
            return self._done()
        if command.startswith('cat '):
            return self._done(self.files.get(shlex.split(command)[1], b''))
        raise AssertionError(f'unexpected unit command: {command}')

    def check(self, command, *, input=None, timeout=60):
        result = self.run(command, input=input, timeout=timeout)
        if result.returncode:
            raise UnitError(f'unit command failed ({result.returncode}): {command}')
        return result.stdout.decode()

    def push(self, data, remote, mode=None):
        self.log.append(f'push {remote}')
        self.files[remote] = bytes(data)

    def read(self, remote, missing_ok=False):
        return self.files.get(remote, b'')

    def popen(self, command):
        self.log.append(command)
        assert command == f'{self.bin_dir}/mt7996-ctl /dev/w1700k-sdr --serve', command
        session = FakeCtlProcess(self.device)
        self.sessions.append(session)
        return session

    def close(self):
        pass

    def batch(self, steps, timeout):
        results = []
        for step in steps:
            result = self._client(list(step.argv))
            results.append(result)
            if result.exit:
                results += [None] * (len(steps) - len(results))
                break
        return results

    def _client(self, argv):
        self.log.append(argv)
        name, d = Path(argv[0]).name, self.device
        if name == 'mt7996-probe' and argv[1:] == ['/dev/w1700k-sdr', '0x00901000', '4', '--acquire']:
            d.downloads.append(('acquire',))
            return StepResult(0, b'{"at":"read","pass":1,"ok":true,"word0":"0x00000000"}\n', b'', b'')
        if name == 'mt7996-probe' and argv[1] == '/dev/w1700k-sdr' and argv[4:] == ['--raw', OUT]:
            at, n = int(argv[2], 16), int(argv[3])
            if d.downloads[-1:] != [('acquire',)] and d.downloads[-1:] != [('rom',)]:
                raise AssertionError('the ROM is read with host ownership, before anything is downloaded')
            if d.downloads[-1] != ('rom',):
                d.downloads.append(('rom',))
            if at == d.rom_read_failure:
                return StepResult(1, b'{"at":"read","pass":1,"ok":false,"errno":"Input/output error"}\n', b'', b'')
            return StepResult(0, b'{"at":"read","pass":1,"ok":true}\n', b'', d.rom[at - ROM_BASE:at - ROM_BASE + n])
        if name == 'mt7996-download' and argv[1:] == ['/dev/w1700k-sdr', '--reset']:
            d.downloads.append(('--reset',))
            d.reset()
            return StepResult(0, b'{"at":"reset","ok":true}\n', b'', b'')
        if name == 'mt7996-download' and argv[3] == '--i-understand':
            file, flags = Path(argv[2]).name, tuple(argv[4:])
            d.downloads.append((file, *flags))
            if argv[2] not in self.files or file == d.download_failure:
                line = {'at': 'download', 'ok': False, 'phase_name': 'WAIT_DOWNLOAD', 'errno': 'Timer expired'}
                return StepResult(1, (json.dumps(line) + '\n').encode(), b'', b'')
            if '--wa' in flags:                           # the last FW_START: the ROM starts our WM
                d.boot_wm()
            return StepResult(0, b'{"at":"download","ok":true}\n', b'', b'')
        raise AssertionError(f'unexpected client {argv}')


FILES = {'patch': 'w1700k_rom_patch.bin', 'wm': 'w1700k_wm.bin', 'wa': 'w1700k_wa.bin'}


ROM_BASE, ROM = 0x00800000, bytes(range(256)) * 256           # the fake radio's boot ROM: 64 KiB


def make_firmware(directory):
    """A schema 3 firmware.json set (firmware/build.py) with small stand-in images, written for the
    fake radio's ROM."""
    directory = Path(directory)
    containers = []
    for role, name in FILES.items():
        data = role.encode() * 64
        (directory / name).write_bytes(data)
        containers.append({'role': role, 'file': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                           'flags': ['--wa'] if role == 'wa' else []})
    manifest = {'schema': 3, 'rom': {'base': ROM_BASE, 'size': len(ROM), 'sha256': [hashlib.sha256(ROM).hexdigest()]},
                'containers': containers}
    (directory / 'firmware.json').write_text(json.dumps(manifest))
    return manifest


def make_config(tmp):
    """A configuration over make_firmware's set and a unit EEPROM file (16 KiB, blank tail)."""
    tmp = Path(tmp)
    make_firmware(tmp)
    eeprom = tmp / 'eeprom.bin'
    eeprom.write_bytes(bytes(range(256)) * 30 + b'\xff' * (16384 - 7680))
    return Config(unit=UnitConfig(bin_dir='/fake'), eeprom=eeprom, firmware_dir=tmp,
                  export=ExportConfig(dst_mac='02:00:00:00:00:01'))


def temp_dir():
    return tempfile.TemporaryDirectory()


def header_defines(*paths):
    """{name: value} of the numeric #defines of C headers (U32(), u suffixes and integer casts
    allowed), names of earlier defines resolved; a function-like macro is kept as a function."""
    out = {}
    pattern = re.compile(r'#define\s+(\w+)(\(([\w\s,]+)\))?\s+(.+?)\s*(?:/\*.*)?$')
    for path in paths:
        for line in Path(path).read_text().splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            name, _, params, body = match.groups()
            body = re.sub(r'U32\((\w+)\)', r'\1', body)
            body = re.sub(r'\b(0x[0-9a-fA-F]+|\d+)[uU]\b', r'\1', body)
            body = re.sub(r'\((?:unsigned int|unsigned|uint32_t|int)\)', '', body)
            if params:
                out[name] = _macro([p.strip() for p in params.split(',')], body, out)
                continue
            value = _evaluate(body, out)
            if value is not None:
                out[name] = value
    return out


def _macro(params, body, known):
    def expand(*args):
        text = body
        for param, arg in zip(params, args):
            text = re.sub(rf'\b{param}\b', str(arg), text)
        return _evaluate(text, known)
    return expand


def _evaluate(expr, known):
    def call(m):
        args = [_evaluate(a, known) for a in m.group(2).split(',')]
        if not callable(known.get(m.group(1))) or None in args:
            return m.group()
        value = known[m.group(1)](*args)
        return m.group() if value is None else str(value)

    for _ in range(8):                                      # function-like macros used in the body
        expanded = re.sub(r'\b(\w+)\(([^()]*)\)', call, expr)
        if expanded == expr:
            break
        expr = expanded
    expr = re.sub(r'\b[A-Za-z_]\w*\b', lambda m: str(known[m.group()]) if isinstance(known.get(m.group()), int)
                  else m.group(), expr)
    expr = re.sub(r'~\s*0\b', '0xFFFFFFFF', expr)              # 32-bit unsigned, as in C
    if not re.fullmatch(r'[0-9a-fA-Fx\s+\-*()<>|&~]+', expr):
        return None
    try:
        return eval(expr, {'__builtins__': {}}) & 0xFFFFFFFF  # digits and operators only
    except (SyntaxError, TypeError, NameError, ZeroDivisionError):
        return None
