#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""How much of the stream this host takes in, and what the host was doing meanwhile.

    tools/host_bench.py [--seconds S] [--out DIR]          # watch the stream that flows
    tools/host_bench.py --run 320,160 [--seconds S]        # start the streams itself

By default it only watches. Use the receiver as you mean to (the spectrum page open in a browser,
a recording, a SoapySDR program) and let this run beside it: until Ctrl-C, or for S seconds.
Every second it writes down

  - what the stream service filed and what it counts as lost, late, or dropped at its sockets,
  - what the adapter itself says it missed (ethtool -S, where the driver counts it),
  - how busy each CPU was and at what clock, the package temperature and its power limit,

prints a line every ten seconds, and at the end says how the stream fared: the totals, how the
loss was spread over the seconds, which CPU took the interface's packets, and which programs
used the most CPU, so that a number can be told from the load it was measured under. It needs
w1700k-stream running, changes nothing and sends nothing to the unit but one question for the
stream's settings.

With --run it drives the unit instead: for each sample rate in turn it configures the band that
is captured, starts the stream with the packing the link gives, measures (S seconds the first,
a third of it the others; default 300), and stops; w1700k-stream is started if it is not
running, and the receiver is put back as it was found.

Results go to DIR/host-bench.json and DIR/host-bench.txt (default tests/build/).
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'host'), str(ROOT / 'tests'), str(ROOT / 'tools')]

from w1700k import config as host_config  # noqa: E402
from w1700k.controller import Controller  # noqa: E402
import stream_shm  # noqa: E402
import test_report  # noqa: E402

STREAM_NAME = '/w1700k-stream'
SERVICE = ROOT / 'host' / 'stream' / 'build' / 'w1700k-stream'
SERVICE_COUNTERS = ('datagrams', 'slices', 'lost', 'late', 'socket_drops', 'bad', 'overruns', 'reduced', 'packed')
TICK = os.sysconf('SC_CLK_TCK')


def read(path, default=''):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def adapter_counters(interface):
    """The adapter's own counters (ethtool -S), {} where it has none or ethtool is missing."""
    try:
        out = subprocess.run(['ethtool', '-S', interface], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    counters = {}
    for line in out.splitlines()[1:]:
        name, _, value = line.partition(':')
        if value.strip().lstrip('-').isdigit():
            counters[name.strip()] = int(value)
    return counters


def missed(counters):
    """Datagrams the adapter had no room for, by whatever name its driver gives them."""
    names = [n for n in counters if any(k in n for k in ('rx_missed', 'rx_fifo', 'rx_no_buffer', 'rx_over'))]
    return sum(counters[n] for n in names) if names else None


def cpu_times():
    """Per CPU: (busy, total) in clock ticks, from /proc/stat."""
    times = {}
    for line in read('/proc/stat').splitlines():
        parts = line.split()
        if parts and parts[0].startswith('cpu') and parts[0] != 'cpu':
            values = [int(v) for v in parts[1:9]]
            times[int(parts[0][3:])] = (sum(values) - values[3] - values[4], sum(values), values[6])
    return times


def busy_percent(before, after):
    """{cpu: (percent busy, percent in softirq)} between two cpu_times()."""
    out = {}
    for cpu in sorted(before):
        total = after[cpu][1] - before[cpu][1]
        out[cpu] = (round(100 * (after[cpu][0] - before[cpu][0]) / total) if total else 0,
                    round(100 * (after[cpu][2] - before[cpu][2]) / total) if total else 0)
    return out


def clock_ghz():
    values = [int(read(p, '0')) for p in Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/scaling_cur_freq')]
    return round(sum(values) / len(values) / 1e6, 2) if values else None


def package_celsius():
    for zone in sorted(Path('/sys/class/thermal').glob('thermal_zone*')):
        if read(zone / 'type') == 'x86_pkg_temp':
            return int(read(zone / 'temp', '0')) // 1000
    return None


def power_limits():
    """The package's long-term and short-term power limits in watts (Intel RAPL), where shown."""
    base = Path('/sys/class/powercap/intel-rapl:0')
    out = {}
    for n in (0, 1):
        name, value = read(base / f'constraint_{n}_name'), read(base / f'constraint_{n}_power_limit_uw')
        if name and value.isdigit():
            out[name] = int(value) / 1e6
    return out


def receive_placement(interface, sys_root='/sys', proc_root='/proc'):
    """Which CPU the interface's interrupt is delivered to, and where its packets are steered:
    {'queues', 'irq', 'irq_cpu', 'steering'} (init/host/link sets the last two)."""
    net = Path(sys_root, 'class', 'net', interface)
    out = {'queues': len(list((net / 'queues').glob('rx-*'))), 'irq': None, 'irq_cpu': None,
           'steering': read(net / 'queues' / 'rx-0' / 'rps_cpus') or None}
    try:
        device = (net / 'device').resolve(strict=True)
    except OSError:
        return out
    while device != device.parent and str(device) != str(Path(sys_root).resolve()):
        if (device / 'msi_irqs').is_dir():
            numbers = sorted(int(p.name) for p in (device / 'msi_irqs').iterdir() if p.name.isdigit())
            out['irq'] = numbers[0] if numbers else None
            break
        if read(device / 'irq', '0').isdigit() and int(read(device / 'irq', '0')) > 0:
            out['irq'] = int(read(device / 'irq'))
            break
        if re.fullmatch(r'usb\d+', device.name):
            # A USB controller that is no PCI device shows neither: its interrupt is listed under
            # the name of one of its buses (xhci-hcd:usb1), not always the adapter's.
            buses = [device.name] + sorted(p.name for p in device.parent.glob('usb[0-9]*') if p != device)
            listed = read(Path(proc_root, 'interrupts'))
            found = [int(m.group(1)) for bus in buses
                     for m in re.finditer(rf'^\s*(\d+):.*:{bus}(?:,|\s*$)', listed, re.M)]
            if found:
                out['irq'] = found[0]
                break
        device = device.parent
    if out['irq'] is not None:
        out['irq_cpu'] = read(Path(proc_root, 'irq', str(out['irq']), 'effective_affinity_list')) or None
    return out


def process_ticks():
    """{pid: (name, user and system ticks)} of every process."""
    out = {}
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            text = stat.read_text()
        except OSError:
            continue
        name = text[text.index('(') + 1:text.rindex(')')]
        fields = text[text.rindex(')') + 2:].split()
        out[int(stat.parent.name)] = (name, int(fields[11]) + int(fields[12]))
    return out


def busiest(before, after, seconds, count=8):
    """[(name, percent of one CPU)], the most first; processes of one name added up."""
    used = {}
    for pid, (name, ticks) in after.items():
        if pid in before and before[pid][0] == name:
            used[name] = used.get(name, 0) + ticks - before[pid][1]
    rows = sorted(((ticks / TICK / seconds * 100, name) for name, ticks in used.items()), reverse=True)
    return [(name, round(percent)) for percent, name in rows[:count] if percent >= 1]


def summarise(samples, seconds):
    """Totals and the spread over the seconds of a run's one-second samples."""
    total = {k: sum(s[k] for s in samples) for k in SERVICE_COUNTERS}
    lost = sorted(s['lost'] for s in samples)
    with_loss = [n for n in lost if n]
    out = {'seconds': round(seconds, 1), **total,
           'msps': round(total['slices'] * stream_shm.SLICE / seconds / 1e6, 1) if seconds else 0.0,
           'lost_percent': round(100 * total['lost'] / total['slices'], 5) if total['slices'] else None,
           'samples': len(samples), 'samples_with_loss': len(with_loss), 'worst_sample': lost[-1] if lost else 0,
           'longest_clean_seconds': 0.0}
    run = 0.0                                       # a sample is a second and the time it took to read
    for s in samples:
        run = 0.0 if s['lost'] else run + s.get('dt', 1.0)
        out['longest_clean_seconds'] = round(max(out['longest_clean_seconds'], run), 1)
    known = [s['missed'] for s in samples if s['missed'] is not None]
    out['adapter_missed'] = sum(known) if known else None
    clocks = [s['clock_ghz'] for s in samples if s['clock_ghz']]
    out['clock_ghz'] = round(sum(clocks) / len(clocks), 2) if clocks else None
    temps = [s['celsius'] for s in samples if s['celsius'] is not None]
    out['celsius_max'] = max(temps) if temps else None
    return out


def progress_line(samples, seconds):
    """The last ten seconds in a line, for the terminal."""
    recent = samples[-10:]
    slices, lost = sum(s['slices'] for s in recent), sum(s['lost'] for s in recent)
    known = [s['missed'] for s in recent if s['missed'] is not None]
    total, total_lost = sum(s['slices'] for s in samples), sum(s['lost'] for s in samples)
    return (f'{seconds:6.0f} s  {slices * stream_shm.SLICE / sum(s["dt"] for s in recent) / 1e6:6.1f} Msps  lost {lost:5d} in 10 s'
            + (f'  adapter missed {sum(known):4d}' if known else '')
            + f'  clock {recent[-1]["clock_ghz"]} GHz  {recent[-1]["celsius"]} C'
            + (f'  |  so far {100 * total_lost / total:.4f} % lost' if total else '  |  no stream yet'))


def measure(shm, interface, seconds, progress=False):
    """One-second samples for `seconds` (None: until Ctrl-C), and what ran meanwhile."""
    samples = []
    counters = {k: shm.get(k) for k in SERVICE_COUNTERS}
    adapter = missed(adapter_counters(interface))
    cpus, processes, began = cpu_times(), process_ticks(), time.monotonic()
    last = began
    first_cpus, deadline = cpus, None if seconds is None else began + seconds
    while deadline is None or time.monotonic() < deadline:
        try:
            time.sleep(1.0 if deadline is None else max(0.0, min(1.0, deadline - time.monotonic())))
        except KeyboardInterrupt:
            print()
            break
        now = {k: shm.get(k) for k in SERVICE_COUNTERS}
        now_adapter, now_cpus = missed(adapter_counters(interface)), cpu_times()
        sample = {k: now[k] - counters[k] for k in SERVICE_COUNTERS}
        sample['dt'], last = round(time.monotonic() - last, 3), time.monotonic()
        sample['missed'] = None if adapter is None or now_adapter is None else now_adapter - adapter
        sample['clock_ghz'], sample['celsius'] = clock_ghz(), package_celsius()
        sample['busiest_cpu'] = max(busy_percent(cpus, now_cpus).items(), key=lambda item: item[1][0])
        samples.append(sample)
        counters, adapter, cpus = now, now_adapter, now_cpus
        if progress and len(samples) % 10 == 0:
            print(progress_line(samples, time.monotonic() - began), flush=True)
    took = time.monotonic() - began
    return samples, took, busy_percent(first_cpus, cpu_times()), busiest(processes, process_ticks(), took)


def text_report(report):
    lines = [f"Host benchmark, {report['when']}, commit {report['commit']}",
             f"  interface {report['interface']}: {report['link_mbps']} Mbit/s, MTU {report['mtu']}, "
             f"{report['placement']['queues']} receive queue(s), interrupt on CPU {report['placement']['irq_cpu']}, "
             f"steering mask {report['placement']['steering']}",
             '  power limits: ' + (', '.join(f'{name.replace("_", "-")} {watts:g} W' for name, watts in
                                             report['power_limits'].items()) or 'not shown')
             + f"; {report['cpus']} CPUs"]
    for run in report['runs']:
        t = run['totals']
        lines += ['', f"  {run['rate_msps'] or '?'} Msps, packing {run['packing'] or '?'}, {t['seconds']:.0f} s"
                      f"{' watched' if run.get('watched') else ''}: {t['msps']} Msps arrived"]
        if run.get('watched') and run['idle_seconds']:
            lines.append(f"    no stream arrived in {run['idle_seconds']} of those seconds")
        if run.get('problem'):
            lines.append(f"    PROBLEM: {run['problem']}")
        lines += [f"    slices {t['slices']}, lost {t['lost']} ({t['lost_percent']} %), late {t['late']}, "
                  f"dropped at the sockets {t['socket_drops']}, bad datagrams {t['bad']}",
                  f"    the adapter missed {t['adapter_missed']} datagrams"
                  + (f" ({t['adapter_missed'] / t['seconds']:.2f} a second)" if t['adapter_missed'] is not None else ''),
                  f"    samples (about a second each) with any loss: {t['samples_with_loss']} of {t['samples']}; "
                  f"the worst lost {t['worst_sample']} slices; longest stretch without loss "
                  f"{t['longest_clean_seconds']:.0f} s",
                  f"    clock {t['clock_ghz']} GHz mean, package up to {t['celsius_max']} C",
                  '    CPUs, percent busy (of it softirq): '
                  + ' '.join(f"{cpu}:{busy}({soft})" for cpu, (busy, soft) in sorted(run['cpus'].items())),
                  '    most CPU: ' + ', '.join(f'{name} {percent} %' for name, percent in run['busiest'])]
    return '\n'.join(lines) + '\n'


def write(report, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    text = text_report(report)
    (out / 'host-bench.json').write_text(json.dumps(report, indent=1) + '\n')
    (out / 'host-bench.txt').write_text(text)
    print()
    print(text, end='')
    print(f'host_bench: written to {out / "host-bench.txt"} and {out / "host-bench.json"}')


def new_report(cfg, interface, mtu, service_started):
    return {'when': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            'commit': test_report.git('rev-parse', '--short', 'HEAD'), 'interface': interface, 'mtu': mtu,
            'link_mbps': None, 'placement': receive_placement(interface), 'power_limits': power_limits(),
            'cpus': os.cpu_count(), 'service_started_here': service_started, 'runs': []}


def watch(cfg, interface, mtu, seconds, out):
    """Measure the stream that flows, as it is used; nothing is started, stopped or changed."""
    if not stream_shm.ready(STREAM_NAME):
        sys.exit('host_bench: w1700k-stream is not running on this host (host/stream/build/w1700k-stream); '
                 'start it and a stream, or use --run')
    shm = stream_shm.Shm(STREAM_NAME)
    report = new_report(cfg, interface, mtu, False)
    run = {'rate_msps': None, 'packing': '', 'problem': '', 'watched': True, 'log': []}
    try:                                            # what is streaming, for the report's labels
        ctl = Controller(cfg, emit=lambda event: None)
        ctl.attach()
        state = ctl.state()
        ctl.close()
        run['rate_msps'] = state['radio']['sample_rate'] // 10**6
        run['packing'] = state['stream']['packing'] or ''
        report['link_mbps'] = state['stream'].get('link_mbps')
        if not state['stream']['running']:
            print('host_bench: the receiver is not streaming yet; watching all the same')
    except Exception as error:
        print(f'host_bench: the unit did not say what it streams ({error}); watching all the same')
    print('host_bench: watching ' + (f'for {seconds:.0f} s' if seconds else 'until Ctrl-C') + f' on {interface}', flush=True)
    samples, took, cpus, programs = measure(shm, interface, seconds, progress=True)
    shm.close()
    flowing = [sample for sample in samples if sample['slices']]
    run.update(samples=samples, totals=summarise(flowing, sum(sample['dt'] for sample in flowing)), cpus=cpus,
               busiest=programs, idle_seconds=len(samples) - len(flowing))
    if not flowing:
        run['problem'] = 'no stream arrived'
    report['runs'].append(run)
    write(report, out)
    return 0 if flowing else 1


def drive(cfg, interface, mtu, rates, seconds_first, out):
    """Start each stream, measure it, stop it; the receiver is put back as it was found."""
    service = None
    if not stream_shm.ready(STREAM_NAME):
        if not SERVICE.is_file():
            sys.exit(f'host_bench: {SERVICE} is not built (make -C host/stream)')
        print('host_bench: starting w1700k-stream')
        service = subprocess.Popen([str(SERVICE), '--bind', cfg.export.dst_ip], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        for _ in range(100):
            if stream_shm.ready(STREAM_NAME):
                break
            time.sleep(0.1)
        else:
            service.terminate()
            sys.exit('host_bench: w1700k-stream did not start')
        time.sleep(2.5)                             # it finds the CPU to stay off in its first seconds
    shm = stream_shm.Shm(STREAM_NAME)
    events = []
    ctl = Controller(cfg, emit=events.append)
    ctl.attach()
    found = ctl.state()
    was = (found['radio']['band'], found['radio']['sample_rate'], found['radio']['lo_hz'],
           found['radio']['capture_rate'], found['stream']['running'])
    report = new_report(cfg, interface, mtu, service is not None)
    try:
        if found['stream']['running']:
            ctl.stop()
        if not found['radio']['loaded']:
            print('host_bench: loading the radio')
            ctl.load()
        band = ctl.state()['radio']['band']
        for number, rate in enumerate(rates):
            seconds = seconds_first if number == 0 else max(30.0, seconds_first / 3)
            run = {'rate_msps': rate // 10**6, 'packing': '', 'problem': ''}
            del events[:]
            try:
                ctl.configure(band, rate)
                ctl.start()
                state = ctl.state()
                run['packing'] = state['stream']['packing']
                report['link_mbps'] = state['stream'].get('link_mbps') or report['link_mbps']
                print(f'host_bench: {rate // 10**6} Msps, packing {run["packing"]}, {seconds:.0f} s ...', flush=True)
                time.sleep(3)                       # the stream settles
                samples, took, cpus, programs = measure(shm, interface, seconds, progress=True)
                run.update(samples=samples, totals=summarise(samples, took), cpus=cpus, busiest=programs)
                state = ctl.state()
                if not state['stream']['running']:
                    run['problem'] = state['error'] or 'the stream ended by itself'
            except Exception as error:               # a refused or failed request is this run's result
                run['problem'] = f'{type(error).__name__}: {error}'
                run.setdefault('samples', [])
                run.setdefault('totals', summarise([], 0.0))
                run.setdefault('cpus', {})
                run.setdefault('busiest', [])
            finally:
                try:
                    ctl.stop()
                except Exception as error:
                    run['problem'] = (run['problem'] + '; ' if run['problem'] else '') + f'stop: {error}'
            run['log'] = [e['text'] for e in events if e.get('event') == 'log' and e.get('level') in ('warn', 'error')]
            report['runs'].append(run)
    finally:
        try:                                        # as it was found
            band, rate, lo, capture, running = was
            ctl.configure(band, rate, lo, capture if capture != rate else None)
            if running:
                ctl.start()
        except Exception as error:
            print(f'host_bench: the receiver could not be put back as it was: {error}')
        ctl.close()
        shm.close()
        if service:
            service.terminate()
            service.wait(timeout=10)
    write(report, out)
    return 0 if all(not run['problem'] for run in report['runs']) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--seconds', type=float,
                        help='how long to measure (default: until Ctrl-C; with --run, 300 for the first rate '
                             'and a third of it for the others)')
    parser.add_argument('--run', metavar='RATES',
                        help='start the streams itself: sample rates in Msps, in order (for example 320,160)')
    parser.add_argument('--out', default=str(ROOT / 'tests' / 'build'), help='where the results go')
    args = parser.parse_args()
    cfg = host_config.load()
    interface, _, mtu = host_config.local_link_for(cfg.export.dst_ip)
    if args.run:
        rates = [int(float(r) * 1e6) for r in args.run.split(',') if r]
        return drive(cfg, interface, mtu, rates, args.seconds or 300.0, args.out)
    return watch(cfg, interface, mtu, args.seconds, args.out)


if __name__ == '__main__':
    sys.exit(main())
