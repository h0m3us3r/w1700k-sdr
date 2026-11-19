# SPDX-License-Identifier: MIT
"""The W1700K's GPS receiver from the host: an Airoha AG3352 on the unit's high-speed serial
port, which the unit hands to the host over ssh (`w1700k-gps link` there: platform/README.md,
"The GPS receiver").

    w1700k gps status          whether it runs, what start-up does with it, whether its port is there
    w1700k gps on [--keep]     let it run; with --keep, at every start-up too (and its port with it)
    w1700k gps off [--keep]    switch it off
    w1700k gps check           listen for a few seconds: does it talk, and has it a fix
    w1700k gps pty [--baud N] [--name PATH]
                               its serial port as a terminal device on this host, until stopped

The receiver is off unless its owner turns it on, and its port is there only at a boot where it
is kept on: `on --keep`, restart the unit, then `check` or `pty`.

`pty` is the port itself: what the receiver sends (NMEA 0183, a position every second) comes out
of the device it names, and what is written there goes to the receiver. gpsd, pygpsclient or
anything else that reads a serial GPS opens it as one; `--name` gives it a path that stays the
same from run to run.

Where the unit is, is its owner's to know. `check` reads what the receiver sends and says only
what kinds of sentence came, whether there is a fix and from how many satellites: never the
position. Nothing here stores what the receiver says, and nothing of the receiver's is used by
the unit or by this package for anything else.
"""
import time
from functools import reduce

from . import portlink

BAUD = 921600                             # the receiver's, as the factory set it
BAUDS = (115200, 230400, 460800, 921600)  # what the port reaches
FIX = {'1': 'none', '2': '2d', '3': '3d'}


class GpsError(portlink.LinkError):
    pass


class Link(portlink.Link):
    """The receiver's port (the unit's `w1700k-gps link` over ssh)."""
    error = GpsError
    ended = 'the link to the receiver ended'


def fields(line):
    """The fields of one NMEA sentence whose checksum holds, its address first ('GNGGA', ...);
    None for anything else."""
    line = line.strip()
    if not line.startswith(b'$') or line[-3:-2] != b'*':
        return None
    body = line[1:-3]
    try:
        if reduce(lambda a, b: a ^ b, body, 0) != int(line[-2:], 16):
            return None
        return body.decode('ascii').split(',')
    except ValueError:
        return None


def check(link, seconds=3.0):
    """What the receiver sends in `seconds`: {'sentences': {kind: count}, 'bad': lines that were
    not sentences, 'fix': 'none'|'2d'|'3d'|None, 'satellites': those used, or None}. A sentence's
    kind is its last three letters (GGA, RMC, ...; a proprietary one's whole address). The
    position fields are not looked at. Listening starts wherever the receiver is, so a first
    line that is the end of a sentence is passed over, not counted as bad."""
    kinds, bad, fix, used, first = {}, 0, None, None, True
    pending, deadline = b'', time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        pending += link.read(4096, min(left, 0.25))
        *lines, pending = pending.split(b'\n')
        for line in lines:
            got = fields(line)
            started, first = first, False
            if got is None:
                bad += bool(line.strip()) and not (started and b'$' not in line and line.strip()[-3:-2] == b'*')
                continue
            kind = got[0] if got[0].startswith('P') else got[0][-3:]
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == 'GSA' and len(got) > 2:
                fix = FIX.get(got[2], fix)
            elif kind == 'GGA' and len(got) > 7:
                if got[6] == '0':
                    fix = 'none'
                if got[7].isdigit():
                    used = int(got[7])
    if not kinds and not bad:
        raise GpsError(f'the receiver said nothing in {seconds:g} s (switched off, or just switched on)')
    if not kinds:
        raise GpsError('what the receiver sent is not NMEA: another speed, or another protocol')
    return {'sentences': dict(sorted(kinds.items())), 'bad': bad, 'fix': fix, 'satellites': used}


# --- on a unit ---

def tool(unit):
    return unit.tool('w1700k-gps')


def _said(text):
    return dict(line.split('=', 1) for line in text.split() if '=' in line)


def status(unit):
    """{'gps': 'on'|'off', 'start': 'on'|'off', 'port': 'on'|'off'} as the unit says it."""
    return _said(unit.check(f'{tool(unit)} status'))


def switch(unit, on, keep=False):
    return _said(unit.check(f'{tool(unit)} {"on" if on else "off"}{" --keep" if keep else ""}'))


def open_link(unit, baud=BAUD):
    if baud not in BAUDS:
        raise ValueError(f'baud: one of {", ".join(map(str, BAUDS))}')
    return Link(unit.popen_bytes(f'{tool(unit)} link --baud {baud}'))


def pty(unit, baud=BAUD, announce=print, name=None, rounds=None):
    """The receiver's port as a pseudo-terminal on this host, until interrupted or the link ends
    (portlink.pty)."""
    portlink.pty(open_link(unit, baud), announce, name, rounds)
