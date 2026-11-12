#!/usr/bin/env python3
"""Unified Core regression entry: existing units, harness self-tests, native web."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import run_web
import toon


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layer', choices=('all', 'unit', 'web'), default='all')
    parser.add_argument('--binary', help='actual native binary; required for all/web')
    parser.add_argument('--output', type=Path, help='new/empty artifact directory outside source (or core/.local)')
    parser.add_argument('--filter', action='append', default=[], help='native web case-id substring, repeatable')
    parser.add_argument('--kind', choices=('assertion', 'reftest', 'native', 'wpt'))
    parser.add_argument('--suite', choices=('web', 'controls'), default='web')
    args = parser.parse_args(argv)
    if args.layer != 'unit' and not args.binary:
        parser.error('--binary is required for the default full system; use --layer unit intentionally for units only')
    output = args.output.expanduser().resolve() if args.output else Path(tempfile.mkdtemp(prefix='mioa-tests-'))
    if output.is_relative_to(run_web.CORE) and not output.is_relative_to(run_web.CORE / '.local'):
        parser.error('--output must be outside Core source or beneath ignored core/.local')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error('--output must be a new or empty directory')
    output.mkdir(parents=True, exist_ok=True)
    print(f'Artifacts: {output}', file=sys.stderr)
    report = {'schema': 1, 'layer': args.layer, 'status': 'PASS', 'layers': {}}
    if args.layer in ('all', 'unit'):
        environment = os.environ.copy()
        environment['PYTHONPATH'] = os.pathsep.join(str(run_web.CORE / name) for name in ('mioa-core', 'mioa-headless', 'mioa-os/api/system', 'mioa-os'))
        unit_results = []
        # Separate discover processes avoid cross-directory module-name collisions and
        # preserve the original suites' isolation. No duplicate or copied unit tests.
        roots = [('core-headless', run_web.CORE / 'mioa-headless' / 'tests'), ('web-harness', run_web.HERE)]
        for name, root in roots:
            command = [sys.executable, '-m', 'unittest', 'discover', '-s', str(root), '-p', 'test_*.py', '-v']
            try:
                completed = subprocess.run(command, cwd=run_web.CORE, env=environment,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
                (output / (name + '.log')).write_bytes(completed.stdout)
                sys.stdout.buffer.write(completed.stdout)
                sys.stdout.buffer.flush()
                unit_results.append({'id': name, 'status': 'PASS' if completed.returncode == 0 else 'FAIL', 'returncode': completed.returncode})
            except subprocess.TimeoutExpired as exc:
                (output / (name + '.log')).write_bytes(exc.stdout or b'')
                unit_results.append({'id': name, 'status': 'TIMEOUT', 'deadline_seconds': 180})
            except OSError as exc:
                unit_results.append({'id': name, 'status': 'ERROR', 'message': str(exc)})
        report['layers']['unit'] = unit_results
        if any(row['status'] != 'PASS' for row in unit_results):
            report['status'] = 'FAIL'
    if args.layer in ('all', 'web'):
        arguments = ['--binary', args.binary, '--output', str(output / 'web'), '--suite', args.suite]
        for term in args.filter:
            arguments += ['--filter', term]
        if args.kind:
            arguments += ['--kind', args.kind]
        code = run_web.main(arguments)
        report['layers']['web'] = {'status': 'PASS' if code == 0 else 'FAIL', 'report': 'web/results.toon'}
        if code:
            report['status'] = 'FAIL'
    run_web.json_write(output / 'results.json', report)
    (output / 'results.toon').write_text(toon.encode(report) + '\n')
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
