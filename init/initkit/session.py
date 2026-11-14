# SPDX-License-Identifier: MIT
"""One tool run: host checks, the console, two TFTP servers and the run log."""
import argparse
import contextlib
import fcntl
import socket
import subprocess
import termios

from . import tftp, uart
from .log import RunLog


class Session:
    def __init__(self, settings, log, console, served):
        self.settings = settings
        self.log = log
        self.console = console
        # Files placed here are served to both U-Boots; uploads land here too.
        self.served = served


def local_interface(address):
    """The host interface holding `address`, or None."""
    listing = subprocess.run(['ip', '-brief', 'address'], capture_output=True,
                             text=True).stdout
    for line in listing.splitlines():
        fields = line.split()
        if any(field.split('/')[0] == address for field in fields[2:]):
            return fields[0]
    return None


def port_free(address, port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((address, port))
    except OSError as error:
        return error.strerror
    finally:
        probe.close()
    return None


def uart_free(device):
    """None when the console can be opened exclusively, else the reason."""
    import serial
    try:
        port = serial.Serial(port=None, baudrate=115200, timeout=0, exclusive=True)
        port.dtr = port.rts = False
        port.port = device
        port.open()
    except Exception as error:      # pyserial raises several unrelated types
        return str(error)
    try:
        # pyserial set TIOCEXCL; clear it so the real open is not refused.
        fcntl.ioctl(port.fileno(), termios.TIOCNXCL)
    finally:
        port.close()
    return None


def preflight(settings, network=True):
    """Check the host before the operator is asked to touch the unit."""
    problems = []
    if network:
        if local_interface(settings.host_ip) is None:
            problems.append(f'{settings.host_ip} is not configured on any interface '
                            '(see init/README.md, "Host setup")')
        ports = [('transfer', settings.transfer_port)]
        if settings.stock_transport == 'tftp':
            ports.insert(0, ('stock', settings.stock_port))
        for name, port in ports:
            reason = port_free(settings.host_ip, port)
            if reason:
                problems.append(f'cannot listen on {settings.host_ip}:{port} ({name}): {reason}')
    reason = uart_free(settings.uart)
    if reason:
        problems.append(f'cannot open the console {settings.uart}: {reason}')
    if problems:
        raise SystemExit('host check failed:\n  ' + '\n  '.join(problems))


@contextlib.contextmanager
def open_session(settings, tool, write=False, passive=False):
    """Run log, console and (unless passive) the stock and transfer TFTP servers.

    `write` lets the console transmit NAND and environment writes; without it,
    the console refuses them.
    """
    preflight(settings, network=not passive)
    log = RunLog(settings.data_dir, tool)
    served = log.dir / 'served'
    served.mkdir()
    servers = []
    if not passive:
        servers = [
            tftp.ServerThread(served, settings.host_ip, settings.transfer_port,
                              allow_write=True, timeout=5.0, retries=5,
                              name='tftp-transfer',
                              log=lambda line: log.event('tftp', line=line)),
        ]
        if settings.stock_transport == 'tftp':
            # Stock's budget-limited TFTP client needs a fast, retransmitting server.
            # With the serial transport nothing listens for stock at all.
            servers.insert(0, tftp.ServerThread(
                served, settings.host_ip, settings.stock_port, timeout=1.0, retries=30,
                priority=True, name='tftp-stock',
                log=lambda line: log.event('tftp', line=line)))
    console = uart.ConsoleSession(settings.uart, log.dir, log.event, passive=passive,
                                  allow_mutating=write)
    print(f'{tool}: run log in {log.dir}')
    try:
        for server in servers:
            server.start()
        console.open()
        yield Session(settings, log, console, served)
    finally:
        console.close()
        for server in servers:
            server.stop()
        log.close()


def settings_from(args):
    """The connection settings of a parsed command line."""
    return argparse.Namespace(uart=args.uart, host_ip=args.host_ip,
                              device_ip=args.device_ip, stock_port=args.stock_port,
                              stock_transport=args.stock_transport,
                              transfer_port=args.transfer_port,
                              data_dir=args.data_dir.expanduser())
