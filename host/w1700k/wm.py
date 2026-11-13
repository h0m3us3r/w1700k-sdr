# SPDX-License-Identifier: MIT
"""Our own MT7996 firmware (docs/roadmap.md, section 1): load it and use our WM.

Our set (firmware.json, built by firmware/build.py) is the ROM patch, our WM and our WA. Our WM
serves radio commands (firmware/radio/radio_cmd.h: bring-up, channel sets, capture set-up,
streams) through its status block, and runs the control interpreter (firmware/stream/ctl.h) as
its main loop, so RF commands and, for development, raw MMIO and A-die op programs run on it at
any time through W1700K_SDR_CTL.

Its status block (firmware/wm/wm.h) at STATUS: magic, version, heartbeat, state, the core as the
ROM handed it over, and the last trap. A load or store that faults is recorded and skipped, so
the WM survives a read of an address that does not answer; such a read returns nothing usable,
and the readers here check the trap count around every program.
"""
import time

from . import rf

STATUS = 0x0090F300
STATUS_WORDS = ('magic', 'version', 'beats', 'state', 'entry_sp', 'entry_ra', 'entry_gp', 'entry_mstatus',
                'entry_mtvec', 'entry_mie', 'entry_a0', 'entry_a1', 'trap_cause', 'trap_epc', 'trap_tval', 'traps',
                'call', 'calls', 'arg0', 'arg1', 'arg2', 'arg3', 'ret', 'wa_start',
                'radio_ee', 'radio_req', 'radio_ack', 'radio_op', 'radio_arg0', 'radio_arg1', 'radio_arg2',
                'radio_arg3', 'radio_res', 'radio_fail_code', 'radio_fail_arg', 'radio_fails', 'radio_us',
                'radio_state', 'rf_build')
MAGIC = 0x4D57574F                # 'OWWM'
STATE_RUNNING, STATE_TRAPPED = 1, 2
TRAPS = STATUS + 4 * STATUS_WORDS.index('traps')
# Our WA's status block (firmware/wa/wa.h), right after.
WA_STATUS = 0x0090F3E0
WA_STATUS_WORDS = ('magic', 'beats', 'calls', 'last', 'entry_mcache', 'entry_mmisc', 'entry_pmacfg0',
                   'entry_pmacfg2')
WA_MAGIC = 0x4157574F             # 'OWWA'
CALL = STATUS + 4 * STATUS_WORDS.index('call')
ARGS = STATUS + 4 * STATUS_WORDS.index('arg0')
WA_START = STATUS + 4 * STATUS_WORDS.index('wa_start')
WA_START_REQ, WA_STARTED = 1, 2
START_TIMEOUT = 5.0
# Radio commands (firmware/radio/radio_cmd.h).
RADIO_REQ = STATUS + 4 * STATUS_WORDS.index('radio_req')
RADIO_OP = STATUS + 4 * STATUS_WORDS.index('radio_op')
RADIO_OPS = {'selftest': 1, 'identify': 2, 'bringup': 3, 'setup': 4, 'step': 5, 'capture': 6, 'select': 7,
             'stream': 8, 'rate': 9, 'dcoc': 10}
RADIO_PACKING = {'raw': 0, 'half': 1, 'full': 2}
RADIO_STEPS = {'patch_init': 1, 'common': 2, 'band': 3, 'rf_init': 4, 'dcoc': 5, 'post': 6, 'pool': 7}
RADIO_RESULTS = {0: 'ok', 1: 'unknown op', 2: 'bad argument', 3: 'a prerequisite has not run',
                 4: 'hardware access failed'}
RADIO_SELFTEST_MAGIC = 0x52414449
RADIO_FAILS = {1: 'poll timed out at', 2: 'A-die poll timed out at', 3: 'A-die transaction failed at',
               4: 'bad argument', 5: 'unexpected hardware state', 6: 'eFuse read failed at'}
EEPROM_BYTES = 7680


class WmError(RuntimeError):
    pass


def load(radio, firmware, module):
    """A fresh driver, then our set: patch, WM, WA."""
    radio.stage(firmware.containers)
    radio.replace_module(module)
    radio.download(firmware.containers)


class Wm:
    """Our running WM, through one mt7996-ctl session."""

    WRITE_WORDS = rf.IOCTL_WORDS          # words per direct mailbox-page write or read

    def __init__(self, unit, sleep=time.sleep, clock=time.monotonic, session=None):
        self.session = session or rf.CtlSession(unit, own_wm=True)
        self.sleep, self.clock = sleep, clock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.session.close()

    def status(self):
        values, _ = self.session.ctl(read=(STATUS, len(STATUS_WORDS)))
        return dict(zip(STATUS_WORDS, values))

    def wait_running(self, timeout=START_TIMEOUT):
        """The status once our WM's main loop runs and its heartbeat moves; raises otherwise."""
        deadline = self.clock() + timeout
        first = None
        while True:
            st = self.status()
            if st['state'] == STATE_TRAPPED and st['magic'] == MAGIC:
                raise WmError(f'our WM trapped: cause {st["trap_cause"]:#x} at {st["trap_epc"]:#010x}')
            if st['magic'] == MAGIC and st['state'] == STATE_RUNNING:
                if first is not None and st['beats'] != first:
                    return st
                first = st['beats']
            if self.clock() > deadline:
                raise WmError('our WM did not start' if st['magic'] != MAGIC else 'our WM does not run its loop')
            self.sleep(0.05)

    def start_wa(self, timeout=START_TIMEOUT):
        """Have our WM start the WA (firmware/wm/wm.h WM_WA_START) and wait for our WA's main loop.
        Only once the radio is configured: a WA started earlier leaves it without antenna signal."""
        st = self.status()
        if st['wa_start'] != WA_STARTED:
            self.session.ctl([(WA_START, WA_START_REQ)])
        deadline = self.clock() + timeout
        first = None
        while True:
            if self.status()['wa_start'] == WA_STARTED:
                wa = self.wa_status()
                if wa['magic'] == WA_MAGIC:
                    if first is not None and wa['beats'] != first:
                        return wa
                    first = wa['beats']
            if self.clock() > deadline:
                raise WmError('our WA did not start')
            self.sleep(0.05)

    def wa_status(self):
        values, _ = self.session.ctl(read=(WA_STATUS, len(WA_STATUS_WORDS)))
        return dict(zip(WA_STATUS_WORDS, values))

    def traps(self):
        return self.session.ctl(read=(TRAPS, 1))[0][0]

    def run(self, ops, timeout=2.0):
        """Every op's result. Raises WmError on a failed op or a trap during the program."""
        before = self.traps()
        try:
            results = self.session.run(ops, timeout)
        except rf.CtlError as exc:
            raise WmError(str(exc))
        if self.traps() != before:
            st = self.status()
            raise WmError(f'trap during the program: cause {st["trap_cause"]:#x}, address {st["trap_tval"]:#010x}')
        return results

    def read(self, addrs):
        """Words at MCU addresses; None where the read faulted."""
        addrs, out = list(addrs), []
        for i in range(0, len(addrs), rf.CTL_MAX):
            chunk = addrs[i:i + rf.CTL_MAX]
            try:
                out += self.run([rf.r32(a) for a in chunk])
            except WmError:
                out += [self._read_one(a) for a in chunk]
        return out

    def _read_one(self, addr):
        try:
            return self.run([rf.r32(addr)])[0]
        except WmError:
            if self.status()['state'] != STATE_RUNNING:
                raise
            return None

    def read_rf(self, regs):
        """A-die registers [(ant, index)], ant = die << 4 | k (development)."""
        ops = [rf.ar(rf.reg(ant >> 4, ant & 15, index)) for ant, index in regs]
        out = []
        for i in range(0, len(ops), rf.CTL_MAX):
            out += self.run(ops[i:i + rf.CTL_MAX])
        return out

    def start(self, fn, *args):
        """Have our WM call fn(args...) and do not wait (the stream producer returns only at
        the stream's end); the call count it had before."""
        if len(args) > 4:
            raise ValueError('at most four arguments')
        before = self.status()['calls']
        args = [a & 0xFFFFFFFF for a in args] + [0] * (4 - len(args))
        self.session.ctl([(ARGS + 4 * i, a) for i, a in enumerate(args)] + [(CALL, fn)])
        return before

    def call(self, fn, *args, timeout=5.0):
        """Call fn(args...) on our WM (ROM routines included: it runs with the ROM's gp, interrupts
        off); its return value. Up to four word arguments."""
        before = self.start(fn, *args)
        deadline = self.clock() + timeout
        while True:
            st = self.status()
            if st['calls'] != before:
                return st['ret']
            if st['state'] != STATE_RUNNING or self.clock() > deadline:
                raise WmError(f'call to {fn:#010x} did not return (state {st["state"]})')
            self.sleep(0.01)

    def radio_post(self, op, *args):
        """Post a radio command (RADIO_OPS name) and return its request number without waiting."""
        args = [a & 0xFFFFFFFF for a in args] + [0] * (4 - len(args))
        st = self.status()
        if st['radio_req'] != st['radio_ack']:
            raise WmError('a radio command is still running')
        req = (st['radio_req'] + 1) & 0xFFFFFFFF or 1
        self.session.ctl([(RADIO_OP + 4 * i, v) for i, v in enumerate([RADIO_OPS[op]] + args)] + [(RADIO_REQ, req)])
        return req

    def radio_wait(self, req, op='', timeout=60.0):
        """The outcome of a posted radio command as a dict (res, fail_code, fail_arg, fails, us,
        state), once our WM has acknowledged it."""
        deadline = self.clock() + timeout
        while True:
            st = self.status()
            if st['radio_ack'] == req:
                return {k[6:]: st[k] for k in STATUS_WORDS if k.startswith('radio_') and
                        k not in ('radio_req', 'radio_ack', 'radio_op') and not k.startswith('radio_arg')}
            if st['state'] != STATE_RUNNING:
                raise WmError(f'our WM trapped during radio {op}: cause {st["trap_cause"]:#x} at '
                              f'{st["trap_epc"]:#010x}')
            if self.clock() > deadline:
                raise WmError(f'radio {op} did not finish in {timeout:.0f} s')
            self.sleep(0.05)

    def radio(self, op, *args, timeout=60.0):
        """Run a radio command (RADIO_OPS name) on our WM and wait for it."""
        return self.radio_wait(self.radio_post(op, *args), op, timeout)

    def put_eeprom(self, image):
        """The unit's EEPROM image into our WM's buffer (WM_RADIO_EE), read back to check."""
        image = bytes(image[:EEPROM_BYTES]).ljust(EEPROM_BYTES, b'\0')
        base = self.status()['radio_ee']
        words = [int.from_bytes(image[i:i + 4], 'little') for i in range(0, EEPROM_BYTES, 4)]
        self.write([(base + 4 * i, w) for i, w in enumerate(words)])
        if self.read([base + 4 * i for i in range(len(words))]) != words:
            raise WmError('the EEPROM image did not land in our WM')

    def write(self, pairs):
        """Write (address, value) pairs in order."""
        ops = [rf.w32(a, v) for a, v in pairs]
        for i in range(0, len(ops), rf.CTL_MAX):
            self.run(ops[i:i + rf.CTL_MAX])
