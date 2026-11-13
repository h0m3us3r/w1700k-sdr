# SPDX-License-Identifier: MIT
"""w1700k: control a W1700K SDR from the host.

    w1700k load [--band B]          load our firmware and bring the radio up, every band at its
                                    LO and sample rate (a few seconds)
    w1700k band B [--rate 40|80|160|320|640/2] [--lo FREQ]
                                    capture band B (at another sample rate, Msps, or LO); 640/2:
                                    band 2 captured at 640 Msps, every other sample streamed
    w1700k start [--packing P]      stream IQ: 14-bit raw where the link carries it, else packed, else fewer bits
                                    to 28 bits (lossless): every chunk, or every other at 320 Msps
    w1700k stop
    w1700k status [--rf]            receiver state (--rf: read the active band's RF registers)
    w1700k tune FREQ                LO of the active band, e.g. 5250M, 5.25G or 2441.000123M
                                    (any Hz in the band's range, on the synthesizer's grid)
    w1700k gain (--agc | --lna 0..4 --lpf 0..9)
    w1700k filter 20|40|80|160|320|auto
                                    analog filter of the active band, MHz, set by hand; auto:
                                    every band's filter follows its sample rate again (the default)
    w1700k trim 0..31               filter RC trim of the active band
    w1700k serve [--packing P]      JSON requests on stdin, replies and events on stdout
    w1700k push [--from DIR] [--to DIR]
                                    development: copy a fresh driver and clients to the unit
    w1700k fw                       development: our WM's and WA's status blocks

The configuration is ~/.config/w1700k/config.json (see config.py).
"""
import argparse
import json
from dataclasses import replace
import re
import sys
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

from . import config as config_mod
from .config import REPO
from .controller import PACKINGS, Controller, serve
from .radio import RATE_CODE
from .rf import FILTER_CODE
from .unit import Unit

CLIENTS = ('mt7996-probe', 'mt7996-download', 'mt7996-stream', 'mt7996-ctl')
MODULE = 'w1700k_sdr.ko'
DEV_DIR = '/tmp/w1700k'
# --rate: '40'..'320' (captured at the sample rate) and '640/2' (radio.RATE_CODE)
RATE_CHOICES = {(f'{sr // 1_000_000}' if sr == cr else f'{cr // 1_000_000}/{cr // sr}'): (sr, cr)
                for sr, cr in RATE_CODE}


def frequency(text):
    """Hz from '5250000000', '5250M', '5.25G' or '5250e6'."""
    match = re.fullmatch(r'\s*([0-9.]+(?:e[0-9]+)?)\s*([kKmMgG]?)(?:[hH][zZ])?\s*', text)
    if not match:
        raise argparse.ArgumentTypeError(f'not a frequency: {text}')
    scale = {'': 1, 'k': 10 ** 3, 'm': 10 ** 6, 'g': 10 ** 9}[match.group(2).lower()]
    try:
        return int((Decimal(match.group(1)) * scale).to_integral_value(ROUND_HALF_EVEN))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f'not a frequency: {text}')


def _print_event(event):
    kind = event.get('event')
    if kind == 'progress':
        print(f'[{event["fraction"] * 100:3.0f}%] {event["text"]}', file=sys.stderr, flush=True)
    elif kind == 'log':
        print(f'{event["level"]}: {event["text"]}', file=sys.stderr, flush=True)


def push(config, source, target):
    """Copy the driver and the clients to the unit (development builds)."""
    unit = Unit(config.unit)
    names = (MODULE,) + CLIENTS
    missing = [n for n in names if not (source / n).is_file()]
    if missing:
        raise SystemExit(f'{source}: missing {", ".join(missing)}; build kernel/w1700k_sdr first')
    for name in names:
        unit.push((source / name).read_bytes(), f'{target}/{name}', mode=None if name == MODULE else '755')
    print(f'pushed {len(names)} files to {unit.target}:{target}. Use them with\n'
          f'  "unit": {{"bin_dir": "{target}", "module": "{target}/{MODULE}"}}\n'
          'in the configuration; the next `w1700k load` loads this driver.', file=sys.stderr)


def build_parser():
    ap = argparse.ArgumentParser(prog='w1700k', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', type=Path, help='configuration file')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('load', help='load our firmware and bring the radio up')
    p.add_argument('--band', type=int, choices=(0, 1, 2), help='band to capture afterwards')
    p = sub.add_parser('band', help='capture another band, sample rate or LO')
    p.add_argument('band', type=int, choices=(0, 1, 2))
    p.add_argument('--rate', choices=RATE_CHOICES, help='Msps; 640/2: band 2 at 640 Msps, every other sample')
    p.add_argument('--lo', type=frequency)
    p = sub.add_parser('start', help='start streaming')
    p.add_argument('--packing', choices=PACKINGS,
                   help='set the packing by hand, instead of the one the sample rate and the link give')
    sub.add_parser('stop', help='stop streaming')
    p = sub.add_parser('status', help='print the receiver state')
    p.add_argument('--rf', action='store_true', help="also read the active band's RF registers")
    p = sub.add_parser('tune', help='retune the active band')
    p.add_argument('frequency', type=frequency)
    p = sub.add_parser('gain', help='gain of the active band')
    p.add_argument('--agc', action='store_true')
    p.add_argument('--lna', type=int, choices=range(5))
    p.add_argument('--lpf', type=int, choices=range(10))
    p = sub.add_parser('filter', help='analog filter of the active band')
    p.add_argument('mhz', choices=[str(w) for w in sorted(FILTER_CODE)] + ['auto'],
                   help='MHz, set by hand; auto: follow the sample rate')
    p = sub.add_parser('trim', help='filter RC trim of the active band')
    p.add_argument('code', type=int, choices=range(32))
    p = sub.add_parser('serve', help='JSON control protocol on stdin/stdout')
    p.add_argument('--packing', choices=PACKINGS,
                   help="set every stream's packing by hand (as export.packing in the configuration)")
    p = sub.add_parser('push', help='copy a development driver and clients to the unit')
    p.add_argument('--from', dest='source', type=Path, default=REPO / 'kernel' / 'w1700k_sdr' / 'build')
    p.add_argument('--to', dest='target', default=DEV_DIR)
    sub.add_parser('fw', help="our WM's and WA's status blocks (development)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        raise SystemExit(f'configuration: {exc}')
    if args.command == 'push':
        push(config, args.source, args.target)
        return 0
    if args.command == 'serve':
        if args.packing:
            config = replace(config, export=replace(config.export, packing=args.packing))
        serve(Controller(config))
        return 0
    controller = Controller(config, emit=_print_event)
    try:
        controller.attach()
        result = run_command(controller, args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'w1700k {args.command}: {exc}', file=sys.stderr)
        return 1
    finally:
        controller.close()
    print(json.dumps(result if result is not None else controller.state(), indent=1))
    return 0


def run_command(controller, args):
    command = args.command
    if command == 'load':
        controller.load(args.band)
    elif command == 'band':
        rate, capture = RATE_CHOICES.get(args.rate, (None, None))
        controller.configure(args.band, rate, args.lo, capture)
    elif command == 'start':
        controller.start(args.packing)
    elif command == 'stop':
        controller.stop()
    elif command == 'status':
        if args.rf:
            return dict(controller.state(), rf_registers=controller.rf_status())
    elif command == 'tune':
        controller.tune(args.frequency)
    elif command == 'gain':
        if not args.agc and (args.lna is None or args.lpf is None):
            raise ValueError('gain: --agc, or both --lna and --lpf')
        controller.gain(args.agc, args.lna, args.lpf)
    elif command == 'filter':
        if args.mhz == 'auto':
            controller.filter_follow(True)
        else:
            controller.filter(int(args.mhz))
    elif command == 'trim':
        controller.trim(args.code)
    elif command == 'fw':
        w = controller._wm()
        show = lambda st, plain: {k: v if k in plain else f'{v:#010x}' for k, v in st.items()}
        return {'wm': show(w.status(), ('beats', 'traps', 'state', 'version', 'calls')),
                'wa': show(w.wa_status(), ('beats', 'calls'))}
    return None


if __name__ == '__main__':
    sys.exit(main())
