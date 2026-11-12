#!/usr/bin/env python3
"""Single quality controller: basic checks, runtime checks, candidate delivery.

Policy: tests/POLICY.md. No build, download, retry, or implicit acceptance.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

import evidence_view
import toon

HERE = Path(__file__).resolve().parent
CORE = HERE.parent
UNITS = CORE / 'mioa-headless/tests'
SOURCE_PATHS = [str(CORE / name) for name in ('mioa-core', 'mioa-headless', 'mioa-os/api/system', 'mioa-os')]
sys.path[:0] = SOURCE_PATHS
OS_BUILD_SOURCES = frozenset({
    'mioa-os/app_data.c',
    'mioa-os/api/system/app_data_v1.h',
    'mioa-os/environment.c',
    'mioa-os/api/system/environment_v1.h',
    'mioa-os/executable_path.c',
    'mioa-os/api/system/executable_path_v1.h',
    'mioa-os/process.zig',
    'mioa-os/api/system/process_v1.h',
    'mioa-os/terminal.c',
    'mioa-os/api/system/terminal_v1.h',
    'mioa-os/thread.zig',
    'mioa-os/api/system/thread_v1.h',
    'mioa-os/files.c',
    'mioa-os/api/system/graphics_file_v1.h',
    'mioa-os/broker.c',
    'mioa-os/api/system/broker_v1.h',
    'mioa-os/random.c',
    'mioa-os/api/system/random_v1.h',
    'mioa-os/clock.c',
    'mioa-os/api/system/clock_v1.h',
})


# Files may belong to several modules; the plan deduplicates each execution.
MODULES = {
    'network': {'unit': ['test_net'], 'web': ['http-authenticated-session', 'fetch-basic', 'progressive-html-stream'], 'gap': 'Loopback HTTP session, progressive HTML and basic Fetch only; no real TLS/navigation or live-site compatibility corpus.'},
    'dom': {'unit': ['test_axtree_observation'], 'web': ['wpt-dom-appendchild', 'wpt-dom-removechild', 'wpt-dom-contains', 'wpt-dom-textcontent', 'webvtt-entities'], 'gap': 'Only selected DOM node WPTs; broader HTML/script/framework tasks remain unmeasured.'},
    'layout': {'unit': [], 'web': ['layout-relative-units', 'scroll-hit-test', 'flex-solid-paint', 'overflow-clip-paint', 'scaled-overflow-capture'], 'gap': 'Only local layout fixtures; no broad CSS WPT or target-page corpus.'},
    'input': {'unit': ['test_native_guarded_input'], 'web': ['input-focus', 'search-form-data', 'search-button-submit', 'search-submit-enter', 'search-return', 'history-form-return', 'history-form-js-return', 'independent-noopener-pages'], 'gap': 'No representative IME/form submission compatibility corpus.'},
    'storage': {'unit': ['test_profile_service', 'test_profile_service_events', 'test_profile_resource_grant', 'test_profile_migration', 'test_profile_lease_transfer'], 'web': ['storage-local', 'cookie-store-crud', 'storage-indexeddb', 'storage-draft-navigation', 'storage-independent-tabs', 'storage-independent-profiles', 'http-authenticated-session'], 'gap': 'Local CRUD, database-open, standalone draft navigation/tab/Profile and loopback HTTP session fixtures do not establish restart durability, managed-domain isolation, popup session cloning or live-site authentication compatibility.'},
    'media': {'unit': ['test_icon_assets'], 'web': ['canvas-pixels', 'canvas-solid-paint', 'canvas-png-roundtrip', 'scaled-canvas-capture'], 'gap': 'No audio/video/codec compatibility corpus; PNG and scaled Canvas remain local regressions.'},
    'lifecycle': {'unit': ['test_native_lifecycle'], 'web': ['native-frame-contract', 'progressive-html-stream', 'independent-noopener-pages'], 'gap': 'No real-browser long-run/resource/concurrent-navigation qualification.'},
    'control': {'unit': ['test_service', 'test_native_service', 'test_frame_ring'], 'web': ['native-frame-contract', 'progressive-html-stream', 'independent-noopener-pages'], 'gap': 'No representative real-client end-to-end task corpus.'},
    'security': {'unit': ['test_profile_resource_grant', 'test_native_guarded_input'], 'host_unit': ['test_os_authorization'], 'web': [], 'gap': 'No browser origin/TLS/permission boundary qualification or sanitizer campaign.'},
    'harness': {'unit': [], 'web': [], 'gap': None},
}
HARNESS = ['test_web_harness', 'test_quality_native', 'test_quality_fuzz', 'test_quality_browser', 'test_png_exports', 'test_ci', 'test_standards', 'test_leak_direct_os_api', 'test_os_files', 'test_os_app_data', 'test_os_environment', 'test_os_executable_path', 'test_os_process', 'test_os_terminal', 'test_os_threads', 'test_file_exports', 'test_os_endpoints', 'test_os_leases', 'test_artifact_discovery', 'test_host_capabilities', 'test_process_environment', 'test_blocking_calls', 'test_unix_messages', 'test_profile_lease_transfer', 'test_profile_lease_boundary']


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def inventory():
    """Freeze source + test inputs, excluding environments, caches, and build output."""
    ignored = {'.local', '.venv', '__pycache__', 'build', 'target', '.git', '.zig-cache', 'zig-out', 'zig-pkg'}
    result = {}
    for root in (HERE, CORE / 'mioa-core', CORE / 'mioa-headless', CORE / 'runtime/engine/src', CORE / 'mioa-os'):
        for directory, dirs, names in os.walk(root):
            dirs[:] = sorted(n for n in dirs if n not in ignored and not n.endswith('.egg-info'))
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix in {'.py', '.json', '.html', '.js', '.zig', '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.m', '.mm', '.rs', '.toml'}:
                    result[path.relative_to(CORE).as_posix()] = sha(path)
    return result


def bind_binary(binary, receipt):
    """Match completed build, all recorded runtime and OS sources, and added sources."""
    record = json.loads(receipt.read_text())
    artifact = record.get('artifacts', {}).get('engine') or record.get('binary', {})
    if record.get('status') != 'completed' or artifact.get('sha256') != sha(binary):
        raise ValueError('binary does not match completed build receipt')
    files = record.get('sources', {}).get('files', [])
    if not files:
        raise ValueError('build receipt has no source inventory')
    expected = {}
    for row in files:
        name = row['path']
        path = CORE / name
        if name in expected or not (name.startswith('runtime/') or name in OS_BUILD_SOURCES) or '..' in Path(name).parts or not path.resolve().is_relative_to(CORE):
            raise ValueError('invalid build source inventory')
        expected[name] = row['sha256']
        if not path.is_file() or sha(path) != row['sha256']:
            raise ValueError(f'build receipt source mismatch: {name}')
    ignored = {'.local', '.zig-cache', 'zig-out', 'target', 'zig-pkg', '.lp-cache'}
    actual = set()
    for directory, dirs, names in os.walk(CORE / 'runtime'):
        dirs[:] = [n for n in dirs if n not in ignored]
        actual.update((Path(directory) / n).relative_to(CORE).as_posix() for n in names if n not in ignored)
    actual.update(OS_BUILD_SOURCES)
    if actual != set(expected):
        raise ValueError('build receipt source inventory differs from current runtime')
    return {'binary': str(binary), 'binary_sha256': sha(binary),
            'receipt': str(receipt.resolve()), 'receipt_sha256': sha(receipt)}


def plan(modules, broad=False):
    rows = {'source/leak-direct-os-api': {
        'id': 'source/leak-direct-os-api', 'kind': 'source', 'name': 'leak-direct-os-api',
        'modules': list(modules),
    }}
    for module in modules:
        spec = MODULES[module]
        suites = ((HERE, HARNESS),) if module == 'harness' else (
            (UNITS, spec['unit']), (HERE, spec.get('host_unit', ())))
        for root, names in suites:
            for name in names:
                key = 'unit/' + name
                rows.setdefault(key, {'id': key, 'kind': 'unit', 'root': str(root), 'name': name, 'modules': []})['modules'].append(module)
        for name in spec['web']:
            key = 'web/' + name
            rows.setdefault(key, {'id': key, 'kind': 'web', 'name': name, 'modules': []})['modules'].append(module)
    if broad:
        # New tests cannot disappear merely because the module map has not been updated.
        for root in (UNITS, HERE):
            for path in sorted(root.glob('test_*.py')):
                key = 'unit/' + path.stem
                rows.setdefault(key, {'id': key, 'kind': 'unit', 'root': str(root), 'name': path.stem, 'modules': ['unmapped']})
        import run_web
        for case in run_web.load_suite('web'):
            key = 'web/' + case['id']
            rows.setdefault(key, {'id': key, 'kind': 'web', 'name': case['id'], 'modules': ['unmapped']})
    return list(rows.values())


def unit_worker(root, name, output):
    sys.path.insert(0, root)
    suite = unittest.defaultTestLoader.loadTestsFromName(name)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    status = 'PASS' if result.wasSuccessful() and result.testsRun > 0 else 'FAIL'
    if status == 'PASS' and (result.skipped or result.expectedFailures):
        status = 'INCOMPLETE'
    write_json(output, {'status': status, 'tests_run': result.testsRun,
                       'skipped': [{'id': test.id(), 'reason': reason} for test, reason in result.skipped],
                       'expected_failures': len(result.expectedFailures),
                       'failures': len(result.failures), 'errors': len(result.errors)})
    return 0 if status == 'PASS' else 1


def launch(command, log, timeout, environment=None):
    started = time.monotonic()
    with log.open('wb') as stream:
        try:
            process = subprocess.Popen(command, cwd=CORE, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True, env=environment)
        except OSError as exc:
            return {'status': 'ERROR', 'error': str(exc), 'duration_seconds': time.monotonic() - started}
        try:
            code = process.wait(timeout=timeout)
            return {'status': 'PASS' if code == 0 else 'CRASH' if code < 0 else 'FAIL',
                    'returncode': code, 'duration_seconds': time.monotonic() - started}
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return {'status': 'TIMEOUT', 'duration_seconds': time.monotonic() - started}


def execute(row, output, binary, timeout):
    directory = output / row['id'].replace('/', '-')
    directory.mkdir()
    if row['kind'] == 'web' and binary is None:
        return dict(row, status='NOT_RUN', reason='explicit --binary required')
    if row['kind'] == 'source':
        result_path = directory / 'result.json'
        command = [sys.executable, str(HERE / 'leak_direct_os_api.py'), '--root', str(CORE), '--report', str(result_path)]
    elif row['kind'] == 'unit':
        result_path = directory / 'result.json'
        command = [sys.executable, str(HERE / 'ci.py'), '_unit', row['root'], row['name'], str(result_path)]
    else:
        result_path = directory / 'artifacts/results.json'
        command = [sys.executable, str(HERE / 'run_web.py'), '--binary', str(binary), '--filter', row['name'], '--output', str(result_path.parent)]
    environment = os.environ.copy()
    environment['PYTHONPATH'] = os.pathsep.join(SOURCE_PATHS)
    environment.pop('MIOA_COOKIE_TEST_BINARY', None)
    if binary:
        environment['MIOA_COOKIE_TEST_BINARY'] = str(binary)
    execution = launch(command, directory / 'execution.log', timeout, environment)
    result = dict(row, **execution, command=command, log=str(directory / 'execution.log'))
    try:
        child = json.loads(result_path.read_text())
        if row['kind'] == 'web':
            records = child['results']
            if len(records) != 1 or records[0]['id'] != row['name']:
                raise ValueError('web selection differs from exact planned case')
            child_status = records[0]['status']
            if child_status == 'PASS' and child.get('status') != 'PASS':
                raise ValueError('web parent/child verdict conflict')
        elif row['kind'] == 'source':
            if type(child.get('files_checked')) is not int or child['files_checked'] < 1:
                raise ValueError('source boundary inspected no files')
            child_status = child['status']
        else:
            if type(child.get('tests_run')) is not int or child['tests_run'] < 1:
                raise ValueError('unit suite executed no tests')
            child_status = child['status']
        if child_status not in {'PASS', 'FAIL', 'ERROR', 'TIMEOUT', 'CRASH', 'INCOMPLETE', 'UNSUPPORTED'}:
            raise ValueError('unknown child status')
        if execution['status'] not in {'ERROR', 'TIMEOUT', 'CRASH'}:
            result['status'] = child_status
            if execution.get('returncode') != 0 and child_status == 'PASS':
                result.update(status='ERROR', reason='nonzero exit contradicts PASS')
        result['result'] = child
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if execution['status'] not in {'ERROR', 'TIMEOUT', 'CRASH'}:
            result.update(status='ERROR', reason=f'invalid child evidence: {exc}')
    return result


def assess(rows, gaps, mode, acceptance):
    """Only explicit, dated user-impact decisions can accept compatibility gaps/failures."""
    decisions = {}
    for item in acceptance:
        key = item['id']
        if key in decisions or not item.get('reason', '').strip() or not item.get('user_impact', '').strip() or date.fromisoformat(item['expires']) < date.today():
            raise ValueError('duplicate, incomplete, or expired acceptance')
        decisions[key] = item
    issues = [dict(row) for row in rows if row['status'] != 'PASS'] + list(gaps)
    issue_ids = {row['id'] for row in issues}
    if set(decisions) - issue_ids:
        raise ValueError('acceptance contains stale or unknown issue IDs')
    for issue in issues:
        # Failures of arbitrary unit tests may hide safety/data-loss defects. Never waive them here.
        allowed = issue['id'].startswith('gap/') or (
            issue.get('kind') == 'web' and issue['status'] == 'FAIL'
            and issue['id'] != 'web/native-frame-contract')
        issue['accepted'] = allowed and issue['id'] in decisions
        if issue['accepted']:
            issue['decision'] = decisions[issue['id']]
    blocked = any(not issue['accepted'] for issue in issues)
    if mode == 'deploy':
        return ('BLOCKED' if blocked else 'ACCEPTED_WITH_RISKS' if issues else 'PASS'), issues
    if any(row['status'] not in {'PASS', 'NOT_RUN', 'INCOMPLETE'} for row in rows):
        return 'FAIL', issues
    return ('INCOMPLETE' if issues else 'PASS'), issues


def next_action(report):
    """Select the first unmet obligation, never promote on skipped evidence."""
    level = report['level']
    command = report.get('rerun', f'make test LEVEL={level}')
    if not report['results'] and not (report.get('error') or report.get('standards_error')):
        return {'action': 'repair-missing-execution-evidence', 'rerun': command}
    if report.get('error') or report.get('standards_error'):
        return {'action': 'repair-preflight-or-evidence',
                'reason': report.get('error') or report['standards_error'], 'rerun': command}
    for row in report['results']:
        if row['kind'] == 'source' and row['status'] != 'PASS':
            groups = row.get('result', {}).get('inventory', {}).get('groups', [])
            focus = sorted(groups, key=lambda item: (-item['candidate'], item['capability']))[:3]
            return {'action': 'review-and-fix-os-boundary', 'check': row['id'],
                    'guidance': '失败阻止验收推进，不禁止编码。先判定归属；缺公开能力就补契约与OS实现，再迁移Core，不加豁免。',
                    'edit_locations': {'core': ['mioa-core/mioa_core/', 'runtime/engine/src/'],
                                       'api': 'mioa-os/api/', 'os': 'mioa-os/'},
                    'steps': [
                        '核对focus中的调用；区分宿主能力、纯计算和业务同名，不能按引用数自动迁移。',
                        '复用mioa-os/api已有能力；缺少时在同一公开入口定义版本、权限、范围、寿命和失败语义。',
                        '在mioa-os实现缺失服务；复用已有Graphics实现须经公开接口，不让Core导入私有后端。',
                        '按可独立验收的能力切片迁移Core及调用者；保留短读写、偏移和批量快路径，不建直连fallback。',
                        '补真实资源与撤销/越权/陈旧句柄回归；进程隔离单独验证，API包装不等于sandbox。',
                        '允许运行时复验所属场景和原make test；禁止执行时继续读写实现，标记未验证，不改FAIL。'],
                    'focus': [dict(capability=item['capability'], guidance=item['guidance'],
                                   apis=item['apis'][:3], locations=item['locations'][:2]) for item in focus],
                    'evidence': row.get('log'), 'rerun': command}
    for row in report['results']:
        if row['kind'] != 'source' and row['status'] != 'PASS':
            if row['kind'] == 'web' and row['status'] == 'NOT_RUN' and not report.get('binary'):
                return {'action': 'supply-runtime-artifact', 'required': ['BINARY'],
                        'rerun': command + ' BINARY=/absolute/path/to/current-binary'}
            return {'action': 'resolve-first-failure-or-missing-evidence', 'check': row['id'],
                    'status': row['status'], 'evidence': row.get('log'), 'rerun': command}
    if level == 1:
        return {'action': 'run-runtime-level', 'required': ['BINARY'],
                'rerun': 'make test LEVEL=2 BINARY=/absolute/path/to/current-binary'}
    if level == 2:
        return {'action': 'review-coverage-and-prepare-delivery', 'remaining_gaps': len(report['gaps']),
                'required': ['BINARY', 'RECEIPT'],
                'rerun': 'make test LEVEL=3 BINARY=/absolute/path/to/current-binary RECEIPT=/absolute/path/to/current-receipt'}
    if report['status'] in {'PASS', 'ACCEPTED_WITH_RISKS'}:
        return {'action': 'delivery-gate-complete', 'scope': report['scope'],
                'remaining_gaps': len(report['gaps'])}
    return {'action': 'resolve-declared-coverage-or-review-eligible-risks',
            'remaining_gaps': len(report['gaps']), 'rerun': command}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--level', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--module', action='append', choices=MODULES, default=[])
    parser.add_argument('--list', action='store_true', help='print exact plan without executing')
    parser.add_argument('--format', choices=('toon', 'json'), default='toon')
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--keep-going', action='store_true', help='observe every selected check after failures')
    parser.add_argument('--acceptance', type=Path, help='JSON list of {id,reason,user_impact,expires}; deploy only')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=int, default=180, help='per suite deadline, seconds')
    args = parser.parse_args(argv)
    args.mode = {1: 'dev', 2: 'diagnose', 3: 'deploy'}[args.level]
    if args.level == 1 and (args.binary or args.receipt):
        parser.error('LEVEL=1 is source/unit only; use LEVEL=2 for runtime artifacts')
    if args.receipt and not args.binary:
        parser.error('--receipt requires --binary')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.acceptance and args.mode != 'deploy':
        parser.error('--acceptance is deploy-only')
    if args.level == 3 and args.module:
        parser.error('LEVEL=3 always covers every module')
    modules = args.module or list(MODULES)
    selected = plan(modules, broad=not args.module)
    if args.level == 1:
        selected = [row for row in selected if row['kind'] != 'web']
    gaps = [{'id': 'gap/' + name, 'status': 'NOT_RUN', 'reason': MODULES[name]['gap']} for name in modules if MODULES[name]['gap']]
    if args.mode != 'dev':
        gaps.extend({'id': 'gap/' + name, 'status': 'NOT_RUN', 'reason': reason} for name, reason in {
            'native-suites': 'Broader Zig/Rust engine suites are outside this Python/web runner; no execution evidence imported.',
            'target-tasks': 'No reviewed representative target-user task corpus or weighted success denominator.',
            'wpt': 'Only four pinned DOM testharness pages; full wptrunner/testdriver coverage is not integrated.',
            'installed-artifacts': 'Source-mode regression does not validate installed wheels or deployment packaging.',
        }.items())
    if args.list:
        payload = {'level': args.level, 'mode': args.mode, 'modules': modules, 'plan': selected, 'gaps': gaps}
        print(json.dumps(payload, indent=2) if args.format == 'json' else toon.encode(payload))
        return 0
    if args.output:
        output = args.output.resolve()
    else:
        base = CORE / '.local/test-ci'
        base.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix=args.mode + '-', dir=base))
    if output.is_relative_to(CORE) and not output.is_relative_to(CORE / '.local'):
        parser.error('--output must be outside source or under .local')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error('--output must be new or empty')
    output.mkdir(parents=True, exist_ok=True)
    binary = args.binary.expanduser().resolve() if args.binary else None
    report = {'schema': 1, 'level': args.level, 'mode': args.mode, 'modules': modules, 'plan': selected,
              'results': [], 'gaps': gaps, 'scope': 'selected source/unit regressions; no runtime/browser qualification'
              if args.level == 1 else 'repository regressions; not measured target-user compatibility', 'status': 'ERROR'}
    rerun = [f'make test LEVEL={args.level}']
    if args.module:
        rerun.append('MODULE=' + shlex.quote(' '.join(args.module)))
    if args.keep_going:
        rerun.append('KEEP_GOING=1')
    for key, value in [('BINARY', args.binary), ('RECEIPT', args.receipt), ('ACCEPTANCE', args.acceptance)]:
        if value:
            rerun.append(key + '=' + shlex.quote(str(value.expanduser().resolve())))
    report['rerun'] = ' '.join(rerun)
    before = inventory()
    report['source'] = before
    try:
        if args.mode == 'deploy' and (binary is None or args.receipt is None):
            raise ValueError('deploy requires --binary and --receipt for the current build')
        if binary:
            report['binary'] = {'path': str(binary), 'sha256': sha(binary),
                                'source_binding': 'VERIFIED' if args.receipt else 'NOT_VERIFIED'}
        bound = bind_binary(binary, args.receipt.resolve()) if binary and args.receipt else None
        report['build'] = bound
        for row in selected:
            result = execute(row, output, binary, args.timeout)
            report['results'].append(result)
            if not args.keep_going and ((result['kind'] == 'source' and result['status'] != 'PASS') or (
                    args.mode == 'dev' and result['status'] not in {'PASS', 'NOT_RUN', 'INCOMPLETE'})):
                break
        executed = {row['id'] for row in report['results']}
        report['results'].extend(dict(row, status='NOT_RUN', reason='stopped after failure') for row in selected if row['id'] not in executed)
        if before != inventory():
            raise ValueError('source/test inputs changed during execution; rerun affected checks')
        if binary and sha(binary) != report['binary']['sha256']:
            raise ValueError('binary changed during execution')
        if bound and bind_binary(binary, args.receipt.resolve()) != bound:
            raise ValueError('build identity changed during execution')
        acceptance = json.loads(args.acceptance.read_text()) if args.acceptance else []
        report['status'], report['issues'] = assess(report['results'], gaps, args.mode, acceptance)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report.update(status='ERROR', error=str(exc))
        executed = {row['id'] for row in report['results']}
        report['results'].extend(dict(row, status='NOT_RUN', reason='preflight or execution aborted') for row in selected if row['id'] not in executed)
    if args.mode != 'dev' or any(name != 'harness' for name in modules):
        import standards
        try:
            ledger = standards.build_checklist(standards.load_json(standards.CATALOG),
                                              standards.load_json(standards.DECISIONS), report,
                                              priorities=standards.load_json(standards.PRIORITIES))
            (output / 'standards.md').write_text(standards.render_markdown(ledger))
            report['standards_checklist'] = {'text': 'standards.md', 'coverage': 'coverage.txt'}
            report['standards_todo_priorities'] = ledger['todo_priorities']
            coverage = standards.coverage_inventory(ledger)
            (output / 'coverage.txt').write_text(standards.render_coverage(coverage))
            if args.format == 'json':
                write_json(output / 'standards.json', ledger)
                write_json(output / 'coverage.json', coverage)
            else:
                (output / 'standards.toon').write_text(toon.encode(ledger) + '\n')
                (output / 'coverage.toon').write_text(toon.encode(coverage) + '\n')
            report['test_coverage'] = {key: coverage[key] for key in (
                'catalog_count', 'tests_registered', 'checks_registered', 'scope_declared')}
            if args.mode == 'dev' and args.format == 'toon':
                observed = [spec for spec in ledger['specs'] if any(
                    item['status'] != 'NOT_RUN' for item in spec['evidence'])]
                selected_ledger = dict(ledger, specs=observed, selected_count=len(observed),
                    status_counts={status: sum(spec['status'] == status for spec in observed)
                                   for status in standards.STATUSES})
                print(toon.encode(selected_ledger))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report.update(status='ERROR', standards_error=str(exc))
    report['next_action'] = next_action(report)
    readable = output / 'report.toon'
    readable.write_text(evidence_view.render(report))
    report['readable_report'] = {'path': readable.name, 'sha256': sha(readable)}
    write_json(output / 'report.json', report)
    summary = {
        'status': report['status'],
        'level': args.level,
        'next_action': report['next_action'],
        'checks': len(report['results']),
        'counts': {status: sum(row['status'] == status for row in report['results'])
                   for status in sorted({row['status'] for row in report['results']})},
        'gaps': len(report['gaps']),
        'report': str(readable),
    }
    if report.get('error') or report.get('standards_error'):
        summary['error'] = report.get('error') or report.get('standards_error')
    if 'test_coverage' in report:
        summary['test_coverage'] = report['test_coverage']
    if args.mode == 'deploy':
        summary['deployment_regression_gate_accepted'] = report['status'] in {'PASS', 'ACCEPTED_WITH_RISKS'}
    if args.format == 'json':
        summary['data'] = str(output / 'report.json')
        print(json.dumps(summary))
    else:
        print(toon.encode(summary))
    if report['status'] in {'ERROR', 'FAIL', 'BLOCKED'}:
        return 1
    # Diagnostic INCOMPLETE means observed, not approved. Deployment never exits zero incomplete.
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_unit':
        raise SystemExit(unit_worker(sys.argv[2], sys.argv[3], Path(sys.argv[4])))
    raise SystemExit(main())
