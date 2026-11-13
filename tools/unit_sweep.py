#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run every stream mode on a real unit, briefly, and say which ones work.

    tools/unit_sweep.py [--seconds S] [--out DIR] [--only TEXT]

Needs the unit reachable and w1700k-stream running on this host (host/stream). For each mode it
configures the band and sample rate, starts the stream under one packing, reads S seconds of it
from the host's ring, and stops it. The modes: every band at every sample rate with the packing
the controller picks for the link; on one band, every other packing that fits the link at each
rate; and the 6 GHz band's 640/2 capture. A mode passes when

  - the stream arrives at the sample rate that was set (within 1.5 %, or the 0.15 s by which
    the stream service's counters may trail the stream, whichever is more of the time read),
  - no more than 0.2 % of its slices are lost (a busy host drops a few hundredths),
  - the slices are of the kind the packing makes (lossless, or fewer bits),
  - the samples have a plausible level (not silence, not stuck at full scale),
  - the band's synthesizer is in lock and the receiver reports no error,
  - the front LED says so: the rate's colour, steady for lossless and breathing for fewer bits.

The receiver is put back as it was found. Results go to DIR/unit-sweep.json (default tests/build/)
and into the test report page beside it (tools/test_report.py).
"""
import argparse
import datetime
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'host'), str(ROOT / 'tests'), str(ROOT / 'tools')]

from w1700k import config as host_config  # noqa: E402
from w1700k.controller import PACKINGS, REDUCED, SPLIT, Controller, packing_fits, packing_for  # noqa: E402
import stream_shm  # noqa: E402
import test_report  # noqa: E402

RATES = (40_000_000, 80_000_000, 160_000_000, 320_000_000)
BAND_NAME = {0: '2.4 GHz', 1: '5 GHz', 2: '6 GHz'}
LED_HUE = {40: 180, 80: 240, 160: 300}          # kernel/w1700k_sdr/ledd.h; 320 Msps is the rainbow
STREAM_NAME = '/w1700k-stream'
# The service's counters are brought up to date about ten times a second: over a few seconds
# that alone is a few percent (seen: three modes of thirty reading 3.25 % low with nothing lost).
RATE_TOLERANCE, RATE_SLACK_SECONDS, LOSS_ALLOWED = 0.015, 0.15, 0.002


def reduced(packing):
    return packing in REDUCED or packing == SPLIT


def modes(link_mbps, piece):
    """(name, what, band, rate, capture rate, packing or None for the controller's choice)."""
    out = []
    for band in (0, 1, 2):
        for rate in RATES:
            out.append((f'{BAND_NAME[band]} at {rate // 10**6} Msps', 'the packing the link gives',
                        band, rate, rate, None))
    for rate in RATES:
        chosen = packing_for(rate, link_mbps, piece)
        for packing in PACKINGS:
            if packing != chosen and packing_fits(packing, rate, link_mbps, piece):
                out.append((f'{BAND_NAME[0]} at {rate // 10**6} Msps, packing {packing}', 'set by hand',
                            0, rate, rate, packing))
    out.append((f'{BAND_NAME[2]} at 320 Msps, captured at 640', 'every other sample kept', 2, 320_000_000,
                640_000_000, None))
    return out


def level_dbfs(shm, newest, slices=6):
    """The level of the newest few slices against full scale, and how many different values they hold."""
    total, count, values = 0.0, 0, set()
    for index in range(max(0, newest - slices), newest):
        if shm.tag(index) & stream_shm.TAG_KIND & ~stream_shm.TAG_FLAT:
            continue                                # an event or a loss: no samples
        slot = index & (shm.get('ring_slices') - 1)
        flat = struct.unpack_from('<%dh' % (2 * stream_shm.SLICE), shm.map, shm.get('samples_offset') + slot * 8192)
        total += sum(v * v for v in flat)
        count += len(flat) // 2
        values.update(flat[:512])
    if not count or not total:
        return -math.inf, len(values)
    return 10 * math.log10(total / count / 8192 ** 2), len(values)


def run_mode(ctl, shm, events, mode, seconds):
    name, what, band, rate, capture, packing = mode
    result = {'name': name, 'what': what, 'ok': False, 'packing': packing or '', 'msps': 0.0, 'lost': 0,
              'level_dbfs': 0.0, 'led': ''}
    problems = []
    del events[:]
    try:
        ctl.configure(band, rate, None, capture if capture != rate else None)
        ctl.start(packing)
        state = ctl.state()
        used = state['stream']['packing']
        result['packing'] = used
        time.sleep(1.5)                             # the stream settles, the LED fades to its look
        counters = ('slices', 'lost', 'late', 'reduced', 'packed', 'bad')
        before, began = {k: shm.get(k) for k in counters}, time.monotonic()
        time.sleep(seconds)
        after, took = {k: shm.get(k) for k in counters}, time.monotonic() - began
        delta = {k: after[k] - before[k] for k in counters}
        result['msps'] = delta['slices'] * stream_shm.SLICE / took / 1e6
        result['lost'] = delta['lost']
        level, distinct = level_dbfs(shm, shm.position()[1])
        result['level_dbfs'] = round(level, 1) if math.isfinite(level) else -99.0
        led = json.loads(ctl.unit.check(f'{ctl.unit.tool("w1700k-led")} /dev/w1700k-sdr --status'))
        result['led'] = (f'rainbow, {led["look"]}' if led.get('rainbow_seconds') else
                         f'hue {led["hue"]:.0f}, {led["look"]}')
        state = ctl.state()

        if abs(result['msps'] * 1e6 - rate) > rate * max(RATE_TOLERANCE, RATE_SLACK_SECONDS / took):
            problems.append(f'{result["msps"]:.1f} Msps arrived, not {rate / 1e6:g}')
        if delta['lost'] > LOSS_ALLOWED * max(delta['slices'], 1):
            problems.append(f'{delta["lost"]} of {delta["slices"]} slices lost')
        if delta['bad'] or delta['late']:
            problems.append(f'{delta["bad"]} bad datagrams, {delta["late"]} late slices')
        if packing and used != packing:
            problems.append(f'runs as {used}, not {packing}')
        if reduced(used) and delta['reduced'] < 0.9 * delta['slices']:
            problems.append(f'only {delta["reduced"]} of {delta["slices"]} slices came with fewer bits')
        if not reduced(used) and delta['reduced']:
            problems.append(f'{delta["reduced"]} slices came with fewer bits in a lossless packing')
        if not -70 < level < -1 or distinct < 8:
            problems.append(f'level {level:.1f} dBFS with {distinct} different values: not a signal')
        if state['radio']['synth_locked'][band] is not True:
            problems.append('the synthesizer is out of lock')
        if state['error']:
            problems.append(state['error'])
        want_look = 'breathing' if reduced(used) else 'steady'
        if led['look'] != want_look or (rate < 320_000_000 and round(led['hue']) != LED_HUE[rate // 10**6]) \
                or (rate >= 320_000_000 and not led.get('rainbow_seconds')):
            problems.append(f'the LED shows {result["led"]}')
    except Exception as error:                       # a refused or failed request is this mode's result
        problems.append(f'{type(error).__name__}: {error}')
    finally:
        try:
            ctl.stop()
        except Exception as error:
            problems.append(f'stop: {error}')
    problems += [e['text'] for e in events if e.get('event') == 'log' and e.get('level') in ('warn', 'error')
                 and not any(e['text'] in p for p in problems)]
    result['ok'] = not problems
    result['problem'] = '; '.join(problems)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--seconds', type=float, default=3.0, help='how long each mode is read (default: %(default)s)')
    parser.add_argument('--out', default=str(ROOT / 'tests' / 'build'), help='where the results go')
    parser.add_argument('--only', help='run only the modes whose name contains this')
    args = parser.parse_args()

    if not stream_shm.ready(STREAM_NAME):
        sys.exit('unit_sweep: w1700k-stream is not running on this host (host/stream/build/w1700k-stream)')
    shm = stream_shm.Shm(STREAM_NAME)
    events = []
    cfg = host_config.load()
    ctl = Controller(cfg, emit=events.append)
    ctl.attach()
    found = ctl.state()
    was = (found['radio']['band'], found['radio']['sample_rate'], found['radio']['lo_hz'],
           found['radio']['capture_rate'], found['stream']['running'])
    if found['stream']['running']:
        ctl.stop()
    if not found['radio']['loaded']:
        print('unit_sweep: loading the radio')
        ctl.load()
    speed, _ = ctl.stream.check_interface(cfg.export.interface)
    link = min(v for v in (speed, cfg.export.link_mbps, 10_000) if v)
    todo = [m for m in modes(link, cfg.export.piece) if not args.only or args.only in m[0]]
    print(f'unit_sweep: {len(todo)} modes on a {link} Mbit/s link, {args.seconds:g} s each')
    results = []
    try:
        for number, mode in enumerate(todo, 1):
            result = run_mode(ctl, shm, events, mode, args.seconds)
            results.append(result)
            print(f'  {number:2}/{len(todo)} {"ok  " if result["ok"] else "FAIL"} {result["name"]:44} '
                  f'{result["packing"]:6} {result["msps"]:6.1f} Msps  lost {result["lost"]:<4} '
                  f'{result["level_dbfs"]:6.1f} dBFS  {result["led"]}'
                  + (f'\n        {result["problem"]}' if result['problem'] else ''), flush=True)
    finally:
        try:                                        # as it was found
            band, rate, lo, capture, running = was
            ctl.configure(band, rate, lo, capture if capture != rate else None)
            if running:
                ctl.start()
        except Exception as error:
            print(f'unit_sweep: the receiver could not be put back as it was: {error}')
        ctl.close()
        shm.close()

    units = sorted(p.name for p in (Path('~/.local/share/w1700k/units').expanduser()).glob('*') if p.is_dir())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sweep = {'when': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
             'unit': units[0] if len(units) == 1 else cfg.unit.host, 'link_mbps': link, 'seconds': args.seconds,
             'commit': test_report.git('rev-parse', '--short', 'HEAD'), 'modes': results}
    (out / 'unit-sweep.json').write_text(json.dumps(sweep, indent=1) + '\n')
    if (out / 'report.json').is_file():             # the page beside it shows the sweep too
        (out / 'report.html').write_text(test_report.page(json.loads((out / 'report.json').read_text()), sweep))
    passed = sum(r['ok'] for r in results)
    print(f'unit_sweep: {passed} of {len(results)} modes passed; {out / "unit-sweep.json"}')
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
