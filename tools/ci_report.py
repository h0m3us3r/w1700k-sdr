#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Put what `make check` did on a GitHub run's own page, where it is read without opening a log.

    tools/ci_report.py LOG [REPORT]

Run by the workflow after `make check`, whether that passed or failed. LOG is what it printed;
REPORT is the record tools/test_report.py leaves (default tests/build/report.json).

GitHub lists a run by its jobs and their steps, and `make check` is one step: it shows no tests.
This writes the run's summary instead: how many tests passed, failed and were skipped, a line for
each test module, every skipped test with its reason (they are the tests that did not run there),
and every failed test with the end of what it said. The count is also a notice on the run, and
each failed test an error annotation, which is what a pull request's checks show.

GitHub shows at most ten error annotations from one step: the first nine failed tests are
annotated and the tenth says how many more there are. The summary has every one. When there is
no record (the run stopped before the tests, or in the middle of one), the end of the log stands
in for it.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
SHOWN = 9               # annotations for single tests; one more is kept for "and N more"
DETAIL_MOST = 3000      # the end of a traceback is the part that says what went wrong
LOG_LINES = 40
BAD = ('failed', 'error')


def data(text):
    """Text as a workflow command's message carries it."""
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def value(text):
    """Text as a workflow command's property carries it."""
    return data(text).replace(':', '%3A').replace(',', '%2C')


def cell(text):
    """Text as one cell of a table in the summary holds it."""
    return ' '.join(str(text).split()).replace('|', '\\|')


def named(report, outcomes):
    """The tests with one of these outcomes: (name, what it said)."""
    return [(f'{module["name"]}.{test["group"]}.{test["name"]}'.replace('..', '.').rstrip('.'),
             test.get('detail') or '')
            for module in report.get('modules', []) for test in module.get('tests', [])
            if test.get('outcome') in outcomes]


def counted(report):
    """"605 tests in 110 s: 587 passed, 18 skipped", with only the outcomes that occurred."""
    counts = report.get('counts', {})
    bad = sum(counts.get(key, 0) for key in BAD)
    parts = [f'{number} {word}' for number, word in ((counts.get('passed', 0), 'passed'), (bad, 'failed'),
                                                     (counts.get('skipped', 0), 'skipped')) if number]
    return f'{report.get("total", 0)} tests in {report.get("seconds", 0):.0f} s: ' + (', '.join(parts) or 'none ran')


def annotations(report, log):
    """The lines that make the annotations."""
    if report is None:
        tail = '\n'.join(log.splitlines()[-LOG_LINES:]) or 'the run printed nothing'
        return [f'::error title={value("make check left no record of its tests")}::{data(tail)}']
    tests = named(report, BAD)
    lines = [f'::{"error" if tests else "notice"} title={value(counted(report))}::{data(report.get("host", ""))}']
    lines += [f'::error title={value(name)}::{data(detail[-DETAIL_MOST:] or "failed")}'
              for name, detail in tests[:SHOWN - 1]]
    if len(tests) > SHOWN - 1:
        rest = ', '.join(name for name, _ in tests[SHOWN - 1:])
        lines.append(f'::error title={value(f"and {len(tests) - SHOWN + 1} more")}::{data(rest[:DETAIL_MOST])}')
    return lines


def summary(report, log):
    """The run's summary page, in Markdown."""
    if report is None:
        tail = '\n'.join(log.splitlines()[-LOG_LINES:])
        return f'### make check left no record of its tests\n\n```\n{tail}\n```\n'
    parts = [f'### {counted(report)}\n', f'{report.get("host", "")}\n']
    for name, detail in named(report, BAD):
        parts.append(f'<details open><summary>failed: <code>{name}</code></summary>\n\n'
                     f'```\n{detail[-DETAIL_MOST:]}\n```\n\n</details>\n')
    parts.append('| | module | tests | failed | skipped | seconds | what it tests |\n|---|---|--:|--:|--:|--:|---|')
    for module in report.get('modules', []):
        counts = module.get('counts', {})
        bad = sum(counts.get(key, 0) for key in BAD)
        mark = '❌' if bad else '✅'
        parts.append(f'| {mark} | `{module["name"]}` | {len(module.get("tests", []))} | {bad or ""} | '
                     f'{counts.get("skipped", 0) or ""} | {module.get("seconds", 0):.1f} | {cell(module.get("about", ""))} |')
    skipped = named(report, ('skipped',))
    if skipped:
        parts.append(f'\n<details><summary>{len(skipped)} skipped, and why</summary>\n\n| test | reason |\n|---|---|')
        parts += [f'| `{name}` | {cell(detail)} |' for name, detail in skipped]
        parts.append('\n</details>')
    return '\n'.join(parts) + '\n'


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    log = Path(argv[1])
    path = Path(argv[2]) if len(argv) == 3 else ROOT / 'tests/build/report.json'
    text = log.read_text(errors='replace') if log.is_file() else ''
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        report = None
    for line in annotations(report, text):
        print(line)
    where = os.environ.get('GITHUB_STEP_SUMMARY')
    if where:
        with open(where, 'a') as page:
            page.write(summary(report, text))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
