#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Say on GitHub which tests failed, where a reader sees it without opening a log.

    tools/ci_failures.py LOG [REPORT]

Run by the workflow after `make check` has failed. LOG is what that printed; REPORT is the record
tools/test_report.py leaves (default tests/build/report.json). Each failed test becomes an error
annotation on the run, with the end of what it said, and the run's summary page lists them all.
When no test is on record as failed (the run stopped before the tests, or in the middle of one),
the end of the log is the annotation instead.

GitHub shows at most ten error annotations from one step: the first nine tests are annotated and
the tenth says how many more there are. The summary has every one.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
SHOWN = 9               # annotations for single tests; one more is kept for "and N more"
DETAIL_MOST = 3000      # the end of a traceback is the part that says what went wrong
LOG_LINES = 40


def data(text):
    """Text as a workflow command's message carries it."""
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def value(text):
    """Text as a workflow command's property carries it."""
    return data(text).replace(':', '%3A').replace(',', '%2C')


def failed(report):
    """The tests on record as failed: (name, what it said)."""
    return [(f'{module["name"]}.{test["group"]}.{test["name"]}'.replace('..', '.').rstrip('.'),
             test.get('detail') or '')
            for module in report.get('modules', []) for test in module.get('tests', [])
            if test.get('outcome') in ('failed', 'error')]


def annotations(tests, log):
    """The lines that make the annotations."""
    if not tests:
        tail = '\n'.join(log.splitlines()[-LOG_LINES:]) or 'the run printed nothing'
        return [f'::error title={value("make check stopped")}::{data(tail)}']
    lines = [f'::error title={value(name)}::{data(detail[-DETAIL_MOST:] or "failed")}'
             for name, detail in tests[:SHOWN]]
    if len(tests) > SHOWN:
        rest = ', '.join(name for name, _ in tests[SHOWN:])
        lines.append(f'::error title={value(f"and {len(tests) - SHOWN} more")}::{data(rest[:DETAIL_MOST])}')
    return lines


def summary(tests, log):
    """The same, as the run's summary page shows it."""
    if not tests:
        tail = '\n'.join(log.splitlines()[-LOG_LINES:])
        return f'### make check stopped\n\n```\n{tail}\n```\n'
    parts = [f'### {len(tests)} failed\n']
    for name, detail in tests:
        parts.append(f'<details><summary><code>{name}</code></summary>\n\n```\n{detail[-DETAIL_MOST:]}\n```\n\n</details>\n')
    return '\n'.join(parts)


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    log = Path(argv[1])
    report = Path(argv[2]) if len(argv) == 3 else ROOT / 'tests/build/report.json'
    text = log.read_text(errors='replace') if log.is_file() else ''
    try:
        tests = failed(json.loads(report.read_text()))
    except (OSError, ValueError):
        tests = []
    for line in annotations(tests, text):
        print(line)
    where = os.environ.get('GITHUB_STEP_SUMMARY')
    if where:
        with open(where, 'a') as page:
            page.write(summary(tests, text))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
