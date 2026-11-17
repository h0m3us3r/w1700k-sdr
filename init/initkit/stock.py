# SPDX-License-Identifier: MIT
"""Getting an image into RAM through stock U-Boot, over TFTP or the serial line.

TFTP, the default: stock U-Boot (2014.04, vendor build) can only fetch from UDP
69, and its TFTP gives up after ten timeouts per transfer which its broken timer
spends on wall clock, not on lost packets. Transfers therefore go in 32 KiB
pieces, each one a separate `tftpboot` with a fresh budget, at a 1468-byte block
size.

The serial line, `--stock-transport serial`: stock's `loadb` takes the image by
Kermit over the console itself (the OpenWrt wiki's page for this unit names the
command). It needs no network, so no listener on UDP 69, no root and no redirect,
and costs about half a minute an image where TFTP takes seconds.

Either way the image is checked in RAM with `crc32` against the host's value
before anything uses it, and stock checks the FIT's own SHA-1 hashes with
`iminfo`. Kermit's own check is six bits a packet, so over the serial line that
`crc32` is what vouches for the load.
"""
from pathlib import Path
import re
import zlib

from . import kermit
from .console import StepFailed, command, verify_fit

STAGE_ADDRESS = 0x89000000
CHUNK = 32768
BLOCK_SIZE = 1468
TFTP_TIMEOUT_MS = 3000
# Each piece spends part of a budget that cannot be refilled; past this many
# pieces an image is too big to stage reliably and should be compressed.
MAX_PIECES = 24
TRANSFERRED = re.compile(r'Bytes transferred = (\d+)')
TIMEOUT_MARK = re.compile(r'T(?= |\r|\n)')
CRC32 = re.compile(r'==>\s*([0-9a-fA-F]{1,8})')
TRANSPORTS = ('tftp', 'serial')
# `loadb` announces itself with one line and reports the size when it ends.
LOADB_READY = re.compile(r'## Ready for binary \(kermit\) download[^\n]*bps\.\.\.')
LOADB_TOTAL = re.compile(r'## Total Size\s*=\s*0x[0-9a-fA-F]+\s*=\s*(\d+) Bytes')
# 115200 baud, eight data bits with a start and a stop bit.
LINE_BYTES_PER_SECOND = 11520
# A packet unanswered for this long is sent again, this many times in all.
PACKET_SECONDS = 2.0
PACKET_TRIES = 12
ENV_LINE = re.compile(r'^([A-Za-z_][\w#.-]*)=(.*)$')


def parse_env(text):
    """`printenv` output as a dict."""
    found = {}
    for line in text.replace('\r', '\n').split('\n'):
        match = ENV_LINE.match(line.strip())
        if match and match.group(1) != 'Environment size':
            found[match.group(1)] = match.group(2)
    return found


def environment(console):
    return parse_env(console.must_run('printenv', limit=20))


def network(console, settings):
    console.must_run('; '.join((f'setenv ipaddr {settings.device_ip}',
                                f'setenv serverip {settings.host_ip}',
                                f'setenv tftpblocksize {BLOCK_SIZE}',
                                f'setenv tftptimeout {TFTP_TIMEOUT_MS}',
                                'setenv netretry no')), limit=20)


def split(image, directory):
    """Cut `image` into pieces in `directory`; returns [(name, bytes)] in order."""
    data = Path(image).read_bytes()
    pieces = []
    for index in range(0, len(data), CHUNK):
        name = f'{Path(image).stem}.p{index // CHUNK:02d}'
        (Path(directory) / name).write_bytes(data[index:index + CHUNK])
        pieces.append((name, len(data[index:index + CHUNK])))
    return pieces


def load_serial(console, log, image, data):
    """`data` to STAGE_ADDRESS with stock's `loadb`."""
    start = console.mark()
    console.send_async(f'loadb {STAGE_ADDRESS:#x}')
    if not console.wait(lambda: LOADB_READY.search(console.view(start)) is not None, 15,
                        'loadb to announce itself'):
        console.wake()
        raise StepFailed('stock U-Boot did not start `loadb`; without it the image must '
                         f'go by TFTP (--stock-transport tftp).\n{console.view(start)[-400:]}')
    seconds = kermit.line_bytes(data) / LINE_BYTES_PER_SECOND
    print(f'  {image.name}: to stock U-Boot over the serial line (about {seconds:.0f} s)')
    port = console.binary()
    sender = kermit.Sender(port.write, port.read, timeout=PACKET_SECONDS, tries=PACKET_TRIES)
    try:
        stats = sender.send(data, image.name.encode())
    except kermit.Failed as error:
        console.wake()                  # Ctrl-C ends loadb's wait for a packet
        log.event('stage_serial_failed', image=image.name, **sender.stats)
        raise StepFailed(f'{image.name} did not go over the serial line: {error}. Nothing '
                         'was written; retry, or use --stock-transport tftp.') from error
    console.expect_prompt('stock', timeout=30)
    total = LOADB_TOTAL.search(console.view(start))
    log.event('stage_serial', image=image.name, bytes=len(data),
              reported=int(total.group(1)) if total else None, **stats)
    if not total or int(total.group(1)) != len(data):
        raise StepFailed(f'stock U-Boot reports {total.group(1) if total else "no size"} '
                         f'for {image.name}, not {len(data)} bytes')


def load_tftp(console, log, settings, image, served_dir):
    """`image` to STAGE_ADDRESS with stock's `tftpboot`; returns the pieces it took.

    `served_dir` is the directory the stock-port TFTP server serves.
    """
    pieces = split(image, served_dir)
    if len(pieces) > MAX_PIECES:
        raise StepFailed(f'{image.name} needs {len(pieces)} transfers; stock U-Boot '
                         f'cannot be relied on for more than {MAX_PIECES}')
    network(console, settings)
    for index, (name, length) in enumerate(pieces):
        address = STAGE_ADDRESS + index * CHUNK
        text = console.must_run(f'tftpboot {address:#x} {name}', limit=300)
        moved = TRANSFERRED.search(text)
        log.event('stage_piece', name=name, expected=length,
                  moved=int(moved.group(1)) if moved else None,
                  timeouts=len(TIMEOUT_MARK.findall(text)))
        if not moved or int(moved.group(1)) != length:
            raise StepFailed(f'{name} did not arrive ({length} bytes expected). Check '
                             'that the stock-port TFTP redirect is active (see '
                             'init/README.md) and retry, or send it over the serial line '
                             f'instead (--stock-transport serial).\n{text[-400:]}')
    return len(pieces)


def stage(console, log, settings, image, served_dir):
    """Load `image` to STAGE_ADDRESS through stock and verify it there."""
    image = Path(image)
    data = image.read_bytes()
    if settings.stock_transport == 'serial':
        load_serial(console, log, image, data)
        pieces = 0
    else:
        pieces = load_tftp(console, log, settings, image, served_dir)
    want = zlib.crc32(data) & 0xffffffff
    text = command(console, f'crc32 {STAGE_ADDRESS:#x} {len(data):#x}', limit=120)
    got = CRC32.search(text)
    if not got or int(got.group(1), 16) != want:
        raise StepFailed(f'{image.name} is corrupt in RAM: crc32 '
                         f'{got.group(1) if got else "missing"}, expected {want:08x}')
    verify_fit(console, STAGE_ADDRESS)
    log.event('staged', image=image.name, bytes=len(data), pieces=pieces,
              transport=settings.stock_transport)
    return len(data)


def boot(console, prompt, address=STAGE_ADDRESS, interrupt=False):
    """`bootm` the FIT at `address` and wait for `prompt`.

    The loader counts down before it autoboots; `interrupt` stops it there.
    """
    console.send_async(f'bootm {address:#x}')
    if interrupt:
        console.interrupt_autoboot(prompt, limit=60)
    console.expect_prompt(prompt, timeout=60)

