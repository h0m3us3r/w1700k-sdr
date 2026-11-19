# SPDX-License-Identifier: MIT
"""A serial port of the unit on this host: the unit joins the port to a command's standard input
and output (`w1700k-efr32 link`, `w1700k-gps link`), ssh carries them, and here they are a `Link`
to read and write, or a pseudo-terminal that other programs open as if it were the port.

The low-power radio (efr32.py) and the GPS receiver (gps.py) are both reached this way.
"""
import os
import select
import subprocess
import time


class LinkError(RuntimeError):
    pass


class Link:
    """A port: a process whose stdin and stdout are it (the unit's link command over ssh; any
    stand-in for it in a test)."""
    error = LinkError
    ended = 'the link to the port ended'

    def __init__(self, process):
        self.process = process
        self.pending = b''

    def write(self, data):
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise self.error(self._ended()) from None

    def _ended(self):
        """Why the link ended: the last thing its command said, where it said anything."""
        said = b''
        if self.process.poll() is not None and self.process.stderr:
            said = self.process.stderr.read() or b''
        text = said.decode(errors='replace').strip().splitlines()
        return text[-1] if text else self.ended

    def read(self, count, timeout):
        """`count` bytes, or fewer when `timeout` seconds bring no more."""
        deadline = time.monotonic() + timeout
        fd = self.process.stdout.fileno()
        while len(self.pending) < count:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                if not self.pending:
                    raise self.error(self._ended())
                break
            self.pending += chunk
        out, self.pending = self.pending[:count], self.pending[count:]
        return out

    def close(self):
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def pty(link, announce=print, name=None, rounds=None):
    """`link` as a pseudo-terminal on this host, until interrupted or the link ends. Its device
    path is announced once it is there: `name` when one is given, which is then a symbolic link
    to the device for as long as this runs (a path that stays the same from run to run, for a
    program's configuration), else the device itself (/dev/pts/N)."""
    master, slave = os.openpty()
    made = None
    try:
        import termios
        import tty
        tty.setraw(slave)
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        device = os.ttyname(slave)
        if name is not None:
            name = os.path.abspath(os.path.expanduser(str(name)))
            if os.path.islink(name):            # left by a run that was killed
                os.unlink(name)
            os.symlink(device, name)            # anything else already there is not ours to replace
            made = name
        announce(made or device)
        out = link.process.stdout.fileno()
        while rounds is None or rounds > 0:
            if rounds is not None:
                rounds -= 1
            ready, _, _ = select.select([master, out], [], [], 0.5)
            if link.process.poll() is not None and out not in ready:
                raise link.error(link._ended())
            if out in ready:
                data = os.read(out, 4096)
                if not data:
                    raise link.error(link._ended())
                os.write(master, data)
            if master in ready:
                link.write(os.read(master, 4096))
    finally:
        if made:
            try:
                os.unlink(made)
            except OSError:
                pass
        os.close(master)
        os.close(slave)
        link.close()
