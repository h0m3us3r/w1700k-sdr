#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the test suite and write what happened as a web page.

    tools/test_report.py [--tests DIR] [--out DIR] [--pattern GLOB]

Runs every test under DIR (default tests/) as `python3 -m unittest discover` does, with the same
progress on the terminal and the same exit status, and writes OUT/report.html (default
tests/build/, one self-contained file) and OUT/report.json: every test with its outcome and
time, grouped by module under what the module says it tests; failures with their tracebacks,
skips with their reasons. `make check` runs this.

A sweep of the stream modes on a real unit (tools/unit_sweep.py) leaves OUT/unit-sweep.json; when
one is there, the page shows it too, with when it was run.
"""
import argparse
import ast
import datetime
import html
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
OUTCOMES = ('failed', 'error', 'skipped', 'passed')     # the order a module's tests are listed in
LABEL = {'passed': 'passed', 'failed': 'failed', 'error': 'error', 'skipped': 'skipped'}


class Recorder(unittest.TextTestResult):
    """The terminal output of the standard runner, and a record of every test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self._began = {}

    def startTest(self, test):
        self._began[test.id()] = time.perf_counter()
        super().startTest(test)

    def _record(self, test, outcome, detail=''):
        began = self._began.pop(test.id(), None)
        module, _, rest = test.id().partition('.')
        if test.id().partition(' (')[0] in ('setUpClass', 'tearDownClass', 'setUpModule', 'tearDownModule'):
            name, _, where = test.id().partition(' (')          # setUpClass (module.Class)
            module, _, group = where.rstrip(')').partition('.')
        else:
            group, _, name = rest.partition('.')
        self.records.append({
            'module': module, 'group': group, 'name': name, 'outcome': outcome, 'detail': detail,
            'seconds': round(time.perf_counter() - began, 3) if began is not None else 0.0,
            'doc': (test.shortDescription() or '') if hasattr(test, 'shortDescription') else '',
        })

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, 'passed')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, 'failed', self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, 'error', self.errors[-1][1])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, 'skipped', reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, 'passed', 'failed as expected')

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, 'failed', 'passed, and was expected to fail')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            kind = 'failed' if issubclass(err[0], test.failureException) else 'error'
            self._record(subtest, kind, (self.failures if kind == 'failed' else self.errors)[-1][1])


def describe(tests, module):
    """What a test module says it tests: its docstring."""
    path = Path(tests) / f'{module}.py'
    try:
        return ' '.join((ast.get_docstring(ast.parse(path.read_text())) or '').split())
    except (OSError, SyntaxError):
        return ''


def git(*arguments):
    try:
        return subprocess.run(['git', '-C', str(ROOT), *arguments], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def gather(tests, records, seconds):
    modules = {}
    for record in records:
        modules.setdefault(record['module'], []).append(record)
    counts = {key: sum(r['outcome'] == key for r in records) for key in OUTCOMES}
    return {
        'when': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'seconds': round(seconds, 1),
        'commit': git('rev-parse', '--short', 'HEAD'), 'branch': git('rev-parse', '--abbrev-ref', 'HEAD'),
        'modified': bool(git('status', '--porcelain', '--untracked-files=no')),
        'host': f'{platform.system()} {platform.machine()}, Python {platform.python_version()}',
        'counts': counts, 'total': len(records),
        'modules': [{'name': name, 'about': describe(tests, name), 'tests': rows,
                     'seconds': round(sum(r['seconds'] for r in rows), 2),
                     'counts': {key: sum(r['outcome'] == key for r in rows) for key in OUTCOMES}}
                    for name, rows in sorted(modules.items())],
    }


# --- the page ---

CSS = '''
:root { --bg:#fbfbfa; --panel:#ffffff; --ink:#1c1e21; --soft:#5d6470; --line:#e3e5e8; --bar:#c9d3e0;
        --ok:#1f7a45; --ok-bg:#e5f3ea; --bad:#b3261e; --bad-bg:#fbe9e7; --skip:#8a5a00; --skip-bg:#fbf0d9; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181b; --panel:#1e2125; --ink:#e6e8eb; --soft:#9aa3ae; --line:#31363c; --bar:#3b4654;
          --ok:#6fcf97; --ok-bg:#1d3327; --bad:#ff8a80; --bad-bg:#3d201e; --skip:#f0c060; --skip-bg:#3a2f17; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1040px; margin:0 auto; padding:28px 16px 64px; }
h1 { font-size:22px; margin:0 0 2px; } h2 { font-size:16px; margin:34px 0 10px; }
.sub { color:var(--soft); font-size:13px; }
.verdict { display:flex; flex-wrap:wrap; gap:10px; margin:18px 0 6px; }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 16px; min-width:112px; }
.tile b { display:block; font-size:24px; line-height:1.2; font-variant-numeric:tabular-nums; }
.tile span { color:var(--soft); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
.tile.ok b { color:var(--ok); } .tile.bad b { color:var(--bad); } .tile.skip b { color:var(--skip); }
details { background:var(--panel); border:1px solid var(--line); border-radius:10px; margin:8px 0; }
summary { cursor:pointer; padding:10px 14px; display:grid; grid-template-columns:1fr auto; gap:4px 16px; align-items:center; }
summary .name { font-weight:600; } summary .about { grid-column:1 / -1; color:var(--soft); font-size:13px; }
.meter { display:flex; align-items:center; gap:10px; font-size:13px; color:var(--soft); font-variant-numeric:tabular-nums; white-space:nowrap; }
.meter i { display:block; height:6px; border-radius:3px; background:var(--bar); }
.pill { border-radius:999px; padding:1px 9px; font-size:12px; font-weight:600; white-space:nowrap; }
.pill.passed { color:var(--ok); background:var(--ok-bg); } .pill.failed, .pill.error { color:var(--bad); background:var(--bad-bg); }
.pill.skipped { color:var(--skip); background:var(--skip-bg); }
table { width:100%; border-collapse:collapse; font-size:14px; }
td, th { padding:6px 14px; border-top:1px solid var(--line); text-align:left; vertical-align:top; }
th { font-size:12px; color:var(--soft); font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
td.t, th.t { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; color:var(--soft); }
td .group { color:var(--soft); font-size:12px; display:block; }
pre { margin:6px 0 2px; padding:10px 12px; background:var(--bg); border:1px solid var(--line); border-radius:8px;
      overflow-x:auto; font:12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; white-space:pre-wrap; }
.scroll { overflow-x:auto; }
.note { color:var(--soft); font-size:13px; margin:6px 0 0; }
'''


def words(name):
    """test_a_stream_that_stalls -> a stream that stalls."""
    return name[5:].replace('_', ' ') if name.startswith('test_') else name


def pill(outcome, text=None):
    return f'<span class="pill {outcome}">{html.escape(text or LABEL[outcome])}</span>'


def module_html(module, longest):
    counts = module['counts']
    bad = counts['failed'] + counts['error']
    state = pill('failed', f'{bad} failed') if bad else pill('passed', f'{counts["passed"]} passed')
    if counts['skipped']:
        state += ' ' + pill('skipped', f'{counts["skipped"]} skipped')
    width = max(2, round(120 * module['seconds'] / longest)) if longest else 2
    rows = []
    for test in sorted(module['tests'], key=lambda t: (OUTCOMES.index(t['outcome']), t['group'], t['name'])):
        detail = ''
        if test['outcome'] in ('failed', 'error'):
            detail = f'<pre>{html.escape(test["detail"])}</pre>'
        elif test['detail']:
            detail = f'<div class="note">{html.escape(test["detail"])}</div>'
        rows.append(f'<tr><td>{html.escape(words(test["name"]))}<span class="group">{html.escape(test["group"])}'
                    f'</span>{detail}</td><td>{pill(test["outcome"])}</td>'
                    f'<td class="t">{test["seconds"]:.2f} s</td></tr>')
    return (f'<details{" open" if bad else ""}><summary><span class="name">{html.escape(module["name"])}</span>'
            f'<span class="meter">{state}<i style="width:{width}px"></i>{module["seconds"]:.1f} s</span>'
            f'<span class="about">{html.escape(module["about"])}</span></summary>'
            f'<div class="scroll"><table><tr><th>Test</th><th>Outcome</th><th class="t">Time</th></tr>'
            f'{"".join(rows)}</table></div></details>')


def sweep_html(sweep):
    """The modes run on a real unit, from tools/unit_sweep.py."""
    if not sweep:
        return ('<h2>On a unit</h2><p class="note">No sweep of the stream modes on a real unit has been recorded '
                'here. <code>tools/unit_sweep.py</code> runs one and adds it to this page.</p>')
    rows = []
    for mode in sweep['modes']:
        ok = mode['ok']
        rows.append(
            f'<tr><td>{html.escape(mode["name"])}<span class="group">{html.escape(mode.get("what", ""))}</span>'
            + (f'<div class="note">{html.escape(mode["problem"])}</div>' if mode.get('problem') else '')
            + f'</td><td>{pill("passed" if ok else "failed")}</td>'
            f'<td class="t">{html.escape(mode.get("packing", ""))}</td>'
            f'<td class="t">{mode.get("msps", 0):.1f}</td><td class="t">{mode.get("lost", 0)}</td>'
            f'<td class="t">{mode.get("level_dbfs", 0):.1f}</td><td class="t">{html.escape(mode.get("led", ""))}</td></tr>')
    passed = sum(m['ok'] for m in sweep['modes'])
    return (f'<h2>On a unit</h2><p class="sub">{passed} of {len(sweep["modes"])} stream modes passed on unit '
            f'{html.escape(sweep.get("unit", "?"))}, {html.escape(sweep["when"])}, commit {html.escape(sweep.get("commit", "?"))}, '
            f'link {sweep.get("link_mbps", "?")} Mbit/s. Each mode: configure, start, {sweep.get("seconds", "?")} s of '
            f'the stream read from the host\'s ring, stop.</p>'
            '<details open><summary><span class="name">Stream modes</span><span class="about">Every sample rate, every '
            'packing the link carries, every band: the samples arrive at the rate set, none lost beyond the '
            'allowance, at a plausible level, with the LED saying so.</span></summary><div class="scroll"><table>'
            '<tr><th>Mode</th><th>Outcome</th><th class="t">Packing</th><th class="t">Msps read</th>'
            '<th class="t">Slices lost</th><th class="t">Level dBFS</th><th class="t">LED</th></tr>'
            + ''.join(rows) + '</table></div></details>')


def page(report, sweep=None):
    counts = report['counts']
    bad = counts['failed'] + counts['error']
    longest = max((m['seconds'] for m in report['modules']), default=0)
    slow = sorted((t for m in report['modules'] for t in m['tests']), key=lambda t: -t['seconds'])[:8]
    verdict = 'All tests passed' if not bad else f'{bad} test{"s" if bad != 1 else ""} failed'
    commit = report['commit'] + (' with uncommitted changes' if report['modified'] else '')
    slow_rows = ''.join(f'<tr><td>{html.escape(words(t["name"]))}<span class="group">{html.escape(t["module"])}'
                        f'</span></td><td class="t">{t["seconds"]:.2f} s</td></tr>' for t in slow)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>W1700K SDR test report</title><style>{CSS}</style></head><body><main>
<h1>{verdict}</h1>
<div class="sub">W1700K SDR test suite · {html.escape(report['when'])} · {html.escape(report['branch'])} at {html.escape(commit)}
 · {html.escape(report['host'])}</div>
<div class="verdict">
<div class="tile ok"><b>{counts['passed']}</b><span>passed</span></div>
<div class="tile{' bad' if bad else ''}"><b>{bad}</b><span>failed</span></div>
<div class="tile{' skip' if counts['skipped'] else ''}"><b>{counts['skipped']}</b><span>skipped</span></div>
<div class="tile"><b>{report['seconds']:.0f} s</b><span>in all</span></div>
<div class="tile"><b>{len(report['modules'])}</b><span>modules</span></div>
</div>
<p class="note">These run without hardware: simulated units, emulated radio cores, and the host programs fed over
loopback. What was run on a real unit is at the end.</p>
<h2>By module</h2>
{''.join(module_html(m, longest) for m in report['modules'])}
<h2>Slowest tests</h2>
<details><summary><span class="name">The eight that took longest</span><span class="about">A test's time does not
include what its class builds once before its first test.</span></summary><div class="scroll"><table>
<tr><th>Test</th><th class="t">Time</th></tr>{slow_rows}</table></div></details>
{sweep_html(sweep)}
</main></body></html>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tests', default=str(ROOT / 'tests'), help='where the tests are (default: tests/)')
    parser.add_argument('--out', help='where the report goes (default: TESTS/build/)')
    parser.add_argument('--pattern', default='test*.py', help='which files hold tests (default: %(default)s)')
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path(args.tests) / 'build'
    began = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(args.tests, pattern=args.pattern)
    result = unittest.TextTestRunner(resultclass=Recorder).run(suite)
    report = gather(args.tests, result.records, time.perf_counter() - began)
    out.mkdir(parents=True, exist_ok=True)
    sweep = None
    if (out / 'unit-sweep.json').is_file():
        try:
            sweep = json.loads((out / 'unit-sweep.json').read_text())
        except ValueError:
            sweep = None
    (out / 'report.json').write_text(json.dumps(report, indent=1) + '\n')
    (out / 'report.html').write_text(page(report, sweep))
    print(f'report: {os.path.relpath(out / "report.html")}')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
