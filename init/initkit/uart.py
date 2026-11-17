# SPDX-License-Identifier: MIT
"""The unit's serial console: record it, stop autoboot, drive U-Boot and Linux.

`BootCatcher` decides when to transmit during a boot and nothing else.
`ConsoleSession` owns the port for a whole run and runs commands at a confirmed
prompt. Every received byte is kept unchanged in RUN/rx.bin. Writing to
RUN/tx.fifo hands the console to the operator for the rest of the run.

Driving a port requires pyserial.
"""
import fcntl
import os
from pathlib import Path
import re
import secrets
import select
import termios
import threading
import time


MAX_RX = 16 * 1024 * 1024
KEY_INTERVAL = 0.05
KEY_BURST = b'    '
INTERRUPT_SECONDS = 15
MAX_KEYS = 1200
BOOT_SECONDS = 45
VERIFY_SECONDS = 5
# The echo that verifies the console is read-only, so one that came back damaged is sent again.
VERIFY_ATTEMPTS = 3
QUIET_SECONDS = 0.1
# Stock U-Boot can lose characters of a line that arrives in one burst: seen on one unit, a
# 26-character line echoed with four characters missing. So nothing is sent faster than
# TX_GAP per character (a character takes 87 us on the wire; a 16-byte FIFO then holds 16 ms of
# typing), and at a U-Boot prompt a command is typed, its echo compared with what was sent, and
# only then entered: a line that lost a digit of an address must not run.
TX_GAP = 0.001
TYPE_ATTEMPTS = 3
ECHO_SECONDS = 2.0
UBOOT_PROMPTS = ('stock', 'dumper', 'restorer', 'loader')
ANSI = re.compile(rb'\x1b\[[0-?]*[ -/]*[@-~]')
COUNTDOWN = re.compile(rb'(?i)(?:hit|press)\s+(?:any\s+)?key\s+to\s+'
                       rb'(?:stop|abort|interrupt)\s+(?:auto\s*)?boot')
PROMPT = re.compile(rb'(?:^|\n)(?:ECNT>|=>)[ \t]*$')
LINUX = re.compile(rb'(?i)(?:Starting kernel|Booting (?:the )?Linux|'
                   rb'Linux version\s+\d|## (?:Booting|Loading) kernel|'
                   rb'Reading kernel|(?:^|\n)[^\n]*login:)')
FUZZY_COUNTDOWN = ((b'hit any key to stop autoboot', 3),
                   (b'press any key to stop autoboot', 3),
                   (b'press any key to interrupt autoboot', 3),
                   (b'press key to abort boot', 2))
FUZZY_LINUX = ((b'starting kernel', 2), (b'booting linux', 1),
               (b'linux version', 1), (b'## booting kernel', 2),
               (b'## loading kernel', 2), (b'reading kernel', 1))


def display(data):
    """Normalize only a matching view; raw RX is always retained unchanged."""
    return ANSI.sub(b'', data).replace(b'\r', b'\n').replace(b'\x08', b'')


def edit_distance(pattern, data, substring=False):
    """Byte insertions/deletions/substitutions; optionally match any substring."""
    row = list(range(len(pattern) + 1))
    best = len(pattern)
    for byte in data:
        current = [0 if substring else row[0] + 1]
        for column, expected in enumerate(pattern, 1):
            current.append(min(current[-1] + 1, row[column] + 1,
                               row[column - 1] + (byte != expected)))
        row = current
        best = min(best, row[-1])
    return best if substring else row[-1]


def boot_evidence(view, exact, fuzzy):
    match = exact.search(view)
    if match:
        return dict(pattern=match.group().decode('ascii', 'backslashreplace'), edits=0)
    # Bound work and expose the damage budget. Unknown bytes count as edits;
    # they are not discarded to manufacture a clean phrase from arbitrary noise.
    recent = re.sub(rb'\s+', b' ', view[-512:].lower())
    for phrase, limit in fuzzy:
        edits = edit_distance(phrase, recent, substring=True)
        if edits <= limit:
            return dict(pattern=phrase.decode(), edits=edits)
    return None


def command_prompt(view, allow_fuzzy):
    match = PROMPT.search(view)
    if match:
        return match.start(), 0
    if allow_fuzzy:
        start = view.rfind(b'\n') + 1
        line = view[start:].strip(b' \t').lower()
        if 4 <= len(line) <= 6:
            edits = edit_distance(b'ecnt>', line)
            if edits <= 1:
                return start, edits
    return None


class BootCatcher:
    """One bounded boot attempt; operator input permanently takes over this run."""
    def __init__(self, send, event, token=None):
        self.send = send
        self.event = event
        self.token = (token or 'uart_' + secrets.token_hex(8)).encode('ascii')
        self.state = 'armed'
        self.tail = b''
        self.first_rx = None
        self.last_rx = None
        self.until = None
        self.next_key = 0
        self.keys = 0
        self.dirty = True
        self.view = b''
        self.linux = None
        self.boot = None
        self.verifies = 0

    def transition(self, state, reason, **evidence):
        self.state = state
        self.event('catcher_state', state=state, reason=reason, keys=self.keys, **evidence)

    def verify(self, now):
        self.verifies += 1
        self.until = now + VERIFY_SECONDS
        self.tail = b''  # Only output received after this command can confirm it.
        self.dirty = True
        self.send(b'echo ' + self.token + b'\r', 'verify U-Boot console')

    def manual(self):
        if self.state != 'manual':
            self.transition('manual', 'operator FIFO input disabled automatic TX')

    def receive(self, data, now):
        if self.first_rx is None:
            self.first_rx = now
        self.last_rx = now
        self.tail = (self.tail + data)[-16384:]
        self.dirty = True

    def tick(self, now):
        if self.state not in ('armed', 'interrupting', 'verifying'):
            return
        if self.dirty:
            self.view = display(self.tail)
            self.linux = boot_evidence(self.view, LINUX, FUZZY_LINUX)
            # Keys sent during stock's Ethernet initialisation can stall its boot,
            # so nothing is transmitted before the countdown or a prompt appears.
            self.boot = (boot_evidence(self.view, COUNTDOWN, FUZZY_COUNTDOWN)
                         if self.state == 'armed' and not self.linux else None)
            self.dirty = False
        view = self.view
        # Later-stage evidence outranks an earlier banner/countdown in the tail.
        if self.linux:
            self.transition('missed', 'Linux handoff/login observed; automatic TX stopped',
                            match=self.linux)
            return
        prompt = command_prompt(view, allow_fuzzy=self.state in ('interrupting', 'verifying'))
        quiet = self.last_rx is not None and now - self.last_rx >= QUIET_SECONDS
        if self.state == 'verifying':
            reply = re.search(rb'(?:^|\n)' + re.escape(self.token) + rb'[ \t]*\n', view)
            if now >= self.until:
                self.transition('inconclusive', 'console echo verification timed out')
            elif prompt and quiet and reply and reply.start() < prompt[0]:
                self.transition('confirmed', 'unique echo output and subsequent U-Boot prompt',
                                prompt_edits=prompt[1])
            elif prompt and quiet and self.verifies < VERIFY_ATTEMPTS:
                # Back at a prompt without the token: the line was damaged on its way in.
                self.event('verify_retry', attempt=self.verifies, seen=view[-80:].decode('latin-1'))
                self.verify(now)
            return
        if self.first_rx is not None and now - self.first_rx >= BOOT_SECONDS:
            self.transition('inconclusive', 'no verified prompt within first-RX deadline')
            return
        if prompt:
            # Stop keying immediately; wait for the prompt to settle before a command.
            if quiet:
                self.transition('verifying', 'U-Boot prompt observed; checking read-only echo',
                                prompt_edits=prompt[1])
                self.verifies = 0
                self.verify(now)
                return
        if self.state == 'armed' and self.boot:
            self.until = now + INTERRUPT_SECONDS
            self.transition('interrupting', 'autoboot countdown observed',
                            match=self.boot)
        if self.state == 'interrupting':
            if now >= self.until or self.keys >= MAX_KEYS:
                self.transition('inconclusive', 'bounded keying ended without a command prompt')
            elif not prompt and now >= self.next_key:
                burst = KEY_BURST[:MAX_KEYS - self.keys]
                self.keys += len(burst)
                self.next_key = now + KEY_INTERVAL
                self.send(burst, 'bounded autoboot interrupt burst')



# ---------------------------------------------------------------------------
# Command driving, for the stages that run after a prompt has been confirmed.
# ---------------------------------------------------------------------------

# Which program is at the other end decides what a command does, so every step
# names the prompt it expects. Ours are the images' CONFIG_SYS_PROMPT (a test
# checks them against the defconfigs); stock's is the vendor U-Boot's.
PROMPTS = {
    'stock': re.compile(rb'(?:^|\n)ECNT>[ \t]*$'),
    'dumper': re.compile(rb'(?:^|\n)dumper>[ \t]*$'),
    'restorer': re.compile(rb'(?:^|\n)restore>[ \t]*$'),
    'loader': re.compile(rb'(?:^|\n)SDR loader>[ \t]*$'),
    # BusyBox ash prints the working directory, `~` for root's home. Only these
    # two exact root-shell prompts count, not any `#` that boot output may end in.
    'linux': re.compile(rb'(?:^|\n)(?:/|~) #[ \t]*$'),
}
SETTLED = re.compile(r'(?:ECNT>|=>|dumper>|restore>|SDR loader>|(?:/|~) #)[ \t]*$')
# How long the port must be silent at a prompt before a command counts as
# finished: over a thousand byte-times at 115200 baud.
QUIET_SETTLE = 0.1
# Commands that can change NAND or a saved environment. A session refuses them
# unless it was opened for writing.
MUTATING = re.compile(r'(?:^|[;&|]\s*)(?:erase|markbad)\b|'
                      r'\b(saveenv|env\s+save|ubi\s|mtd\s+(write|erase|markbad)|'
                      r'nand\s+(write|erase|markbad)|'
                      r'flash\s+(write|erase|upgrade|imgwrite)|'
                      r'sf\s+(write|erase|update)|nandsync(?!\.check))', re.I)


class Refused(Exception):
    """A command this session will not transmit."""


class BinaryPort:
    """The session's port as a file-transfer protocol needs it: bytes out at the
    line's own speed and bytes in as they come. The reader thread still owns the
    port and logs every received byte; this reads its mirror from where it began."""

    def __init__(self, session):
        self.session = session
        self.position = session.mark()

    def write(self, data):
        self.session.check()
        with self.session.write_lock:
            written = self.session.uart.write(data)
            self.session.uart.flush()
        if written != len(data):
            raise RuntimeError('partial UART write; inspect the transcript before retry')

    def read(self, timeout):
        """What has arrived since the last read, waiting up to `timeout` for any."""
        session = self.session
        deadline = time.monotonic() + timeout
        with session.changed:
            while len(session.buffer) <= self.position:
                session.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b''
                session.changed.wait(min(remaining, 0.2))
            data = bytes(session.buffer[self.position:])
            self.position = len(session.buffer)
            return data


class ConsoleSession:
    """The UART for a whole run: raw log, boot catcher, operator FIFO, commands.

    One reader thread owns the port. It appends every received byte to `rx.bin`
    unchanged and to an in-memory mirror that `run()` slices, so a command's
    output is read from the same bytes the transcript keeps.
    """

    def __init__(self, device, directory, event, baud=115200, passive=False,
                 allow_mutating=False, max_rx=MAX_RX):
        self.device = device
        self.dir = Path(directory)
        self.event = event
        self.baud = baud
        self.passive = passive
        self.allow_mutating = allow_mutating
        self.max_rx = max_rx
        self.uart = None
        self.control = None
        self.fifo = self.dir / 'tx.fifo'
        self.raw = None
        self.catcher = None
        self.buffer = bytearray()
        # Reentrant on purpose: `wait()` holds this lock while it evaluates a
        # predicate, and the predicates read the buffer through the same lock.
        # A plain Lock deadlocks there, silently, with no timeout to end it.
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.write_lock = threading.Lock()
        self.last_rx = None
        self.count = 0
        self.failure = None
        self.thread = None
        self.stopping = threading.Event()
        self.sent_commands = []

    # -- lifecycle ----------------------------------------------------------
    def open(self):
        import serial                       # pyserial, needed only to drive a port
        self.raw = (self.dir / 'rx.bin').open('xb', buffering=0)
        os.mkfifo(self.fifo, 0o600)
        self.control = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK)
        uart = serial.Serial(port=None, baudrate=self.baud, timeout=0,
                            write_timeout=1, exclusive=True,
                            xonxoff=False, rtscts=False, dsrdtr=False)
        uart.dtr = False
        uart.rts = False
        uart.port = self.device
        uart.open()
        fcntl.ioctl(uart.fileno(), termios.TIOCEXCL)
        self.uart = uart
        self.arm()
        self.event('armed', device=self.device, baud=self.baud, pid=os.getpid(),
                   key_interval=KEY_INTERVAL, key_burst_bytes=len(KEY_BURST),
                   interrupt_seconds=INTERRUPT_SECONDS, max_keys=MAX_KEYS,
                   first_rx_deadline=BOOT_SECONDS,
                   verification_seconds=VERIFY_SECONDS)
        self.thread = threading.Thread(target=self._reader, name='uart',
                                       daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)
        for shut in (lambda: self.uart and self.uart.close(),
                     lambda: self.control is not None and os.close(self.control),
                     lambda: self.fifo.exists() and self.fifo.unlink(),
                     lambda: self.raw and self.raw.close()):
            try:
                shut()
            except OSError as exc:
                self.event('console_close_error', message=str(exc))

    # -- the reader thread --------------------------------------------------
    def _reader(self):
        try:
            while not self.stopping.is_set():
                readable, _, _ = select.select([self.uart.fileno(), self.control],
                                               [], [], KEY_INTERVAL)
                now = time.monotonic()
                if self.uart.fileno() in readable:
                    data = self.uart.read(min(self.uart.in_waiting or 1, 4096))
                    if not data:
                        raise RuntimeError('UART disconnected or returned no data')
                    self.raw.write(data)
                    offset = self.count
                    self.count += len(data)
                    self.event('rx', offset=offset, bytes=len(data))
                    with self.changed:
                        self.buffer += data
                        self.last_rx = now
                        self.changed.notify_all()
                    self.catcher.receive(data, now)
                    if self.count >= self.max_rx:
                        raise RuntimeError(f'{self.max_rx} byte UART limit reached; '
                                           'start a new run')
                if self.control in readable:
                    data = os.read(self.control, 4096)
                    if data:
                        self.catcher.manual()
                        self._transmit(data, 'operator FIFO')
                self.catcher.tick(time.monotonic())
        except Exception as exc:              # the flow must see this, not hang
            self.failure = exc
            self.event('console_error', message=str(exc))
            with self.changed:
                self.changed.notify_all()

    def _transmit(self, data, reason):
        """The only writer. Two threads reach it: this flow, and the operator FIFO."""
        self.event('tx_attempt', hex=data.hex(), reason=reason)
        with self.write_lock:
            written = 0
            for index in range(len(data)):
                written += self.uart.write(data[index:index + 1])
                if index + 1 < len(data):
                    self.uart.flush()
                    time.sleep(TX_GAP)
        self.event('tx_written', bytes=written)
        if written != len(data):
            raise RuntimeError('partial UART write; inspect the transcript before retry')

    # -- observation --------------------------------------------------------
    def check(self):
        if self.failure is not None:
            raise RuntimeError(f'UART reader stopped: {self.failure}')

    def view(self, start=0):
        with self.lock:
            return display(bytes(self.buffer[start:])).decode('latin-1')

    def mark(self):
        with self.lock:
            return len(self.buffer)

    def wait(self, ready, timeout, description):
        """Block until `ready()` is true, the reader dies, or the deadline passes.

        Nothing is logged while holding the lock: the reader thread takes the
        log's lock first and this one second.
        """
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                self.check()
                if ready():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.changed.wait(min(remaining, 0.2))
        self.event('wait_timeout', waiting_for=description, seconds=round(timeout, 1))
        return False

    def binary(self, quiet=QUIET_SETTLE, limit=5.0):
        """A raw channel for a file-transfer protocol, once the unit is silent.

        A typed command goes a character a millisecond with its echo compared; a
        protocol with its own checks and retries needs neither and would take ten
        times as long. But stock U-Boot loses what arrives while it prints, so
        the first packet must not meet the end of the line that announced the
        transfer: this returns when nothing has arrived for `quiet` seconds.
        """
        self.check()
        deadline = time.monotonic() + limit
        with self.changed:
            while True:
                now = time.monotonic()
                idle = now - self.last_rx if self.last_rx is not None else quiet
                if idle >= quiet or now >= deadline:
                    break
                self.changed.wait(max(0.005, quiet - idle))
        try:
            # A USB adapter otherwise holds each short reply for 16 ms.
            self.uart.set_low_latency_mode(True)
        except Exception:                   # one that cannot is only slower
            pass
        self.event('binary')
        return BinaryPort(self)

    def arm(self):
        """Watch the next boot for stock U-Boot's countdown and stop it there.

        Call before the command that reboots the unit; the catcher only acts on
        output received after this.
        """
        self.catcher = BootCatcher(self._transmit, self.event)
        if self.passive:
            self.catcher.transition('passive', 'automatic transmission disabled')

    def wait_for_catch(self, timeout):
        """Wait for the catcher to confirm a stock prompt by its own echo token."""
        terminal = ('confirmed', 'missed', 'inconclusive')
        caught = self.wait(lambda: self.catcher.state in terminal, timeout,
                           'autoboot interception')
        if not caught:
            raise RuntimeError(f'no autoboot countdown within {timeout:.0f}s; '
                               'the unit may not have been powered on')
        if self.catcher.state != 'confirmed':
            raise RuntimeError(f'autoboot interception ended {self.catcher.state}: '
                               f'{self.catcher.view[-400:]!r}')
        return self.catcher.state

    # -- command driving ----------------------------------------------------
    def check_command(self, line):
        """The write guard. A command this session will not transmit never leaves here."""
        if MUTATING.search(line) and not self.allow_mutating:
            raise Refused(f'refusing to transmit a mutating command: {line!r}')
        return line

    def send_line(self, line):
        """Transmit one command. The FIFO stays the operator's alone.

        Our own commands do not go through `tx.fifo`, which would mark the run as
        taken over by the operator.
        """
        self.check_command(line)
        if any(self.at_prompt(name) for name in UBOOT_PROMPTS):
            self._type_checked(line)
        else:
            self._transmit((line + '\n').encode(), 'tool command')

    def _type_checked(self, line):
        """At a U-Boot prompt: type `line`, and enter it only once its echo is exactly what was
        sent. A damaged line is discarded with Ctrl-C and typed again; after TYPE_ATTEMPTS the
        run stops, with nothing entered."""
        wanted = line.encode()
        echoed = b''
        for attempt in range(1, TYPE_ATTEMPTS + 1):
            # The prompt's own last byte may still be on its way (`SDR loader>` seen, its
            # space not yet): what precedes the echo must have arrived before it is marked.
            self.wait(lambda: self.last_rx is None
                      or time.monotonic() - self.last_rx >= QUIET_SECONDS, 1.0, 'a quiet prompt')
            start = self.mark()

            def echo():
                with self.lock:
                    return bytes(self.buffer[start:])
            self._transmit(wanted, 'tool command, typed')
            self.wait(lambda: len(echo()) >= len(wanted), ECHO_SECONDS, 'the echo of a typed command')
            echoed = echo()
            if echoed == wanted:
                self._transmit(b'\n', 'tool command, entered')
                return
            self.event('echo_mismatch', sent=line, echoed=echoed.decode('latin-1'), attempt=attempt)
            self._transmit(b'\x03', 'discard a damaged line')
            self.wait(lambda: any(self.at_prompt(name) for name in UBOOT_PROMPTS), ECHO_SECONDS,
                      'a prompt after discarding a line')
        raise RuntimeError(f'the console did not take {line!r} intact in {TYPE_ATTEMPTS} attempts '
                           f'(the last echo was {echoed!r}); nothing was entered')

    def run(self, line, expect=None, limit=20.0, quiet=QUIET_SETTLE, heartbeat=None,
            about=None):
        """Send one line and return (console text, returned to a prompt).

        A command is finished when the port is back at a prompt and has gone
        quiet. Settling on an output pattern instead could return while the device
        is still printing, and the tail of one command would be read as the next's.

        `expect` is advisory: recorded, not required; callers check the output.
        `heartbeat` reports elapsed time every so many seconds and `about` is the
        typical duration, because some commands (reading all of the NAND) print nothing
        for a minute. The lock is released before reporting: the reader thread
        takes the log's lock before this one.
        """
        self.check()
        start = self.mark()
        self.sent_commands.append(line)
        self.event('command', line=line, limit=limit, about_seconds=about)
        self.send_line(line)
        began = time.monotonic()
        deadline = began + limit
        next_beat = began + heartbeat if heartbeat else None
        settled = timed_out = False
        text = ''
        while True:
            with self.changed:
                self.check()
                text = display(bytes(self.buffer[start:])).decode('latin-1')
                now = time.monotonic()
                at_prompt = bool(text) and SETTLED.search(text) is not None
                idle = now - self.last_rx if self.last_rx is not None else quiet
                if at_prompt and idle >= quiet:
                    settled = True
                elif now >= deadline:
                    timed_out = True
                else:
                    # Sleep only as long as what is being waited for needs: the rest
                    # of the quiet window when a prompt is already in view, otherwise
                    # a short slice, since an arriving byte wakes this immediately.
                    slices = [0.05 if not at_prompt else max(0.0, quiet - idle),
                              deadline - now]
                    if next_beat:
                        slices.append(next_beat - now)
                    self.changed.wait(max(0.005, min(slices)))
            if settled or timed_out:
                break
            now = time.monotonic()
            if next_beat and now >= next_beat:
                self.event('still_running', line=line,
                           elapsed_seconds=round(now - began, 1),
                           about_seconds=about, received=len(text))
                next_beat = now + heartbeat
        matched = None if expect is None else bool(expect.search(text))
        self.event('command_result', line=line, settled=settled,
                   seconds=round(time.monotonic() - began, 2),
                   expected_matched=matched, bytes=len(text), tail=text[-200:])
        return text, settled

    def must_run(self, line, **options):
        text, settled = self.run(line, **options)
        if not settled:
            raise RuntimeError(f'the console did not settle after {line!r}:\n{text[-800:]}')
        return text

    def wake(self):
        """Ask whatever is at the other end for a fresh prompt, transmitting Ctrl-C.

        Not a newline: at a U-Boot prompt an empty line repeats the previous
        command (after `tftpput`, the dumper would upload the whole backup again),
        while Ctrl-C only discards the line. BusyBox ash prints a new prompt too.
        """
        self.check()
        self.event('command', line='<ctrl-c>', mode='wake')
        self._transmit(b'\x03', 'wake: Ctrl-C')

    def send_async(self, line):
        """Transmit a command without waiting for a prompt.

        For a handoff like `bootm`: what follows is an autoboot countdown, not a
        prompt, so the caller drives `interrupt_autoboot` next instead of settling.
        The write guard still applies.
        """
        self.check()
        self.sent_commands.append(line)
        self.event('command', line=line, mode='async')
        self.send_line(line)

    def interrupt_autoboot(self, which, limit=40.0, key_interval=0.2):
        """Key the console through an autoboot countdown until prompt `which`.

        It transmits only interrupt bursts, never a command.
        """
        self.check()
        self.event('interrupt_autoboot', prompt=which, limit=limit)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self.at_prompt(which):
                self.event('prompt_confirmed', prompt=which)
                return which
            self._transmit(KEY_BURST, f'autoboot interrupt ({which})')
            self.wait(lambda: self.at_prompt(which), key_interval, f'{which} prompt')
        raise RuntimeError(f'did not reach the {which} prompt within {limit:.0f}s of '
                           'interrupting autoboot; the loader may have booted past '
                           'its countdown or printed a different prompt')

    def at_prompt(self, which):
        with self.lock:
            return PROMPTS[which].search(display(bytes(self.buffer[-4096:]))) is not None

    def expect_prompt(self, which, timeout=20.0):
        """Confirm which U-Boot is at the other end before a stage acts."""
        pattern = PROMPTS[which]

        def ready():
            with self.lock:
                return pattern.search(display(bytes(self.buffer[-4096:]))) is not None

        if not self.wait(ready, timeout, f'{which} prompt'):
            raise RuntimeError(f'not at the {which} prompt after {timeout:.0f}s; '
                               f'tail: {self.view(max(0, self.mark() - 400))!r}')
        self.event('prompt_confirmed', prompt=which)
        return which
