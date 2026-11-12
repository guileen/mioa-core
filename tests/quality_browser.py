#!/usr/bin/env python3
"""Finite entity/Canvas observations in one owned headless browser, not WPT."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

import run_web
import toon

HERE = Path(__file__).resolve().parent
CORE = HERE.parent
ASSETS = HERE / 'quality_browser'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    run_web.json_write(path, value)


def result_status(statuses):
    order = ('FAIL', 'FLAKY', 'NOT_RUN', 'UNSUPPORTED', 'KNOWN_FAIL')
    if not statuses or any(status not in order + ('PASS',) for status in statuses):
        return 'FAIL'
    return next((status for status in order if status in statuses), 'PASS')


def payload():
    manifest = json.loads((ASSETS / 'manifest.json').read_text())
    source = ASSETS / 'entities.test'
    if digest(source) != manifest['corpus']['sha256']:
        raise ValueError('html5lib corpus identity mismatch')
    if digest(ASSETS / manifest['corpus']['license_file']) != manifest['corpus']['license_sha256']:
        raise ValueError('html5lib license identity mismatch')
    original = json.loads(source.read_text())['tests']
    cases = []
    for index in manifest['selection']:
        test = original[index]
        tokens = test['output']
        if test.get('initialStates') or test.get('doubleEscaped'):
            raise ValueError('selection requires unsupported tokenizer state/escaping')
        if all(token[0] == 'Character' for token in tokens):
            context, expected = 'dom-text', ''.join(token[1] for token in tokens)
        elif len(tokens) == 1 and tokens[0][0] == 'StartTag' and set(tokens[0][2]) == {'a'}:
            context, expected = 'dom-attribute', tokens[0][2]['a']
        else:
            raise ValueError('selection cannot be faithfully projected into observed DOM')
        cases.append({'id': f'html5lib-entities-{index:03d}', 'context': context,
                      'input': test['input'], 'expected': expected, 'description': test['description'],
                      'tokenizer_output': tokens, 'unobserved_tokenizer_errors': test.get('errors', [])})
    cases.extend(manifest['whatwg_supplement'])
    rgba = [255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255,
            0, 0, 0, 0, 255, 255, 255, 255, 17, 93, 201, 255]
    cases.append({'id': 'canvas-rgba-export', 'context': 'canvas-rgba-export',
                  'rgba': rgba, 'expected': rgba})
    return manifest, {'schema': 1, 'cases': cases}


async def mioa(args, script, data, report):
    from mioa_core import profile
    from mioa_core.native import launch_native
    from mioa_core.native_spec import create_native_spec
    from mioa_os import LocalArtifactContent, LocalArtifactDiscovery, LocalChildProcesses, LocalHostFiles, LocalProcessEnvironment, LocalProcessIdentity, LocalProfileLeases, LocalSecureRandom, LocalStreamConnections
    binary = args.binary.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError(f'native executable missing: {binary}')
    report['identity']['binary'] = {'path': str(binary), 'sha256': digest(binary), 'label': args.binary_label}
    old_base = profile.BASE
    with tempfile.TemporaryDirectory(prefix='mioa-differential-') as temporary:
        profile.BASE = Path(temporary)
        session = None
        try:
            name = 'quality-browser'
            cfg = create_native_spec(name, runtime='mioa', binary=binary,
                storage_path=profile.profile_dir(name) / 'data', seed='01' * 32,
                overrides={'screen_width': 320, 'screen_height': 320, 'avail_width': 320,
                           'avail_height': 320, 'device_scale_factor': 1},
                network={'allow_private_network': False}, artifact_discovery=LocalArtifactDiscovery(),
                artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
            profile.save_config(name, cfg)
            report['identity']['configuration']['native_spec'] = cfg
            with profile.exclusive(name, leases=LocalProfileLeases()) as owned:
                if not owned:
                    raise RuntimeError('isolated profile lease unavailable')
                try:
                    session = await run_web.bounded(launch_native(name, cfg, True,
                        child_processes=LocalChildProcesses(),
                        process_environment=LocalProcessEnvironment(), identity=LocalProcessIdentity(),
                        artifact_content=LocalArtifactContent(),
                        connections=LocalStreamConnections(), files=LocalHostFiles(),
                        profile_lease=owned, random=LocalSecureRandom()), 40)
                    report['identity']['receipt'] = session.receipt
                    page = session.context.pages[0] if session.context.pages else await run_web.bounded(session.context.new_page(), 5)
                    await page.goto('about:blank', timeout=10000)
                    result = await run_web.bounded(page.evaluate(script, data), args.timeout)
                    if session.process.returncode is not None:
                        raise RuntimeError(f'native process exited: {session.process.returncode}')
                    return result
                finally:
                    if session:
                        try:
                            await session.close()
                        finally:
                            (args.output / 'native.log').write_text(session.log_text())
        finally:
            profile.BASE = old_base


async def reference(args, script, data, report):
    try:
        version = importlib.metadata.version('playwright')
    except importlib.metadata.PackageNotFoundError as exc:
        raise FileNotFoundError('Playwright reference dependency is not installed') from exc
    report['identity']['tools']['playwright'] = version
    for name in ('pyee', 'greenlet', 'typing-extensions'):
        distribution = importlib.metadata.distribution(name)
        record = distribution.read_text('RECORD')
        report['identity']['tools'][name] = {
            'version': distribution.version,
            'installed_record_sha256': hashlib.sha256(record.encode()).hexdigest() if record else None,
        }
    if version != '1.56.0':
        raise ValueError(f'reference Playwright version must be pinned 1.56.0, got {version}')
    import playwright
    from playwright.async_api import async_playwright
    metadata = Path(playwright.__file__).parent / 'driver/package/browsers.json'
    report['identity']['tools']['browsers_json'] = {'sha256': digest(metadata), 'content': json.loads(metadata.read_text())}
    driver = Path(playwright.__file__).parent / 'driver'
    report['identity']['tools']['driver_sha256'] = {
        path.relative_to(driver).as_posix(): digest(path)
        for path in (driver / 'node', driver / 'package/cli.js', driver / 'package/package.json')
        if path.is_file()
    }
    async with async_playwright() as pw:
        kind = getattr(pw, args.engine)
        executable = args.executable.resolve() if args.executable else Path(kind.executable_path).resolve()
        if not executable.is_file():
            raise FileNotFoundError(f'pinned {args.engine} executable missing: {executable}')
        report['identity']['binary'] = {'path': str(executable), 'sha256': digest(executable), 'label': 'explicit-reference' if args.executable else 'playwright-pinned'}
        # launch() owns a new process and temporary profile; never attach or use persistent context.
        browser = await kind.launch(headless=True, executable_path=str(executable), timeout=40000)
        try:
            report['identity']['tools']['browser_version'] = browser.version
            context = await browser.new_context(viewport={'width': 320, 'height': 240}, device_scale_factor=1, locale='en-US', timezone_id='UTC')
            await context.route('**/*', lambda route: route.abort())
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto('about:blank', timeout=10000)
            result = await asyncio.wait_for(page.evaluate(script, data), args.timeout)
            if errors:
                raise RuntimeError('uncaught page errors: ' + repr(errors))
            return result
        finally:
            await browser.close()


def checks_for(args, data, observed):
    rows = observed.get('results') if isinstance(observed, dict) else None
    if not isinstance(rows, list) or observed.get('complete') is not True or [r.get('id') for r in rows] != [r['id'] for r in data['cases']]:
        raise ValueError('browser omitted complete ordered observation envelope')
    checks = []
    for case, result in zip(data['cases'], rows):
        requirement = 'QV-ENT-DATA' if case['context'].startswith('dom-') else 'QV-PNG-ORACLE'
        passed = 'error' not in result and result.get('value') == case['expected']
        checks.append({'id': case['id'], 'requirement': requirement, 'oracle': 'WHATWG HTML; pinned html5lib-tests expected token projection' if requirement == 'QV-ENT-DATA' else 'WHATWG Canvas putImageData/getImageData exact RGBA8',
                       'status': 'PASS' if passed else 'FAIL', 'detail': {'context': case['context'], 'expected': case['expected'], 'actual': result}})
        if case['context'] == 'canvas-rgba-export':
            check = {'id': 'canvas-png-independent-decode', 'requirement': 'QV-PNG-ORACLE', 'oracle': 'independent libpng RGBA8 decode and exact source pixels', 'status': 'NOT_RUN', 'detail': 'explicit --png-decoder is required'}
            checks.append(check)
            if 'error' in result:
                check.update(status='FAIL', detail='browser Canvas export failed before decode')
                continue
            try:
                png = base64.b64decode(result['png'].split(',', 1)[1], validate=True)
                png_file = args.output / 'canvas.png'
                png_file.write_bytes(png)
                if args.png_decoder:
                    decoder = args.png_decoder.resolve()
                    if not decoder.is_file():
                        raise FileNotFoundError(f'libpng decoder missing: {decoder}')
                    run = subprocess.run([str(decoder), str(png_file), str(args.output / 'canvas.rgba')], capture_output=True, timeout=args.timeout)
                    (args.output / 'libpng.stdout').write_bytes(run.stdout)
                    (args.output / 'libpng.stderr').write_bytes(run.stderr)
                    metadata = json.loads(run.stdout) if run.returncode == 0 else None
                    actual = (args.output / 'canvas.rgba').read_bytes() if run.returncode == 0 else b''
                    ok = run.returncode == 0 and metadata == {'width': 3, 'height': 2, 'format': 'RGBA8'} and actual == bytes(case['rgba'])
                    check.update(status='PASS' if ok else 'FAIL', detail={'returncode': run.returncode, 'metadata': metadata, 'rgba': list(actual), 'decoder_sha256': digest(decoder), 'png_sha256': digest(png_file)})
            except FileNotFoundError as exc:
                check.update(status='NOT_RUN', detail=str(exc))
            except Exception as exc:
                check.update(status='FAIL', detail=f'{type(exc).__name__}: {exc}')
    return checks


async def run(args):
    report = {'schema': 1, 'status': 'NOT_RUN', 'scope': 'finite same-input DOM entity/Canvas observations; no primitive lookup or WPT conformance claim',
              'identity': {'source_files': {}, 'tools': {'python': sys.version, 'python_executable': str(Path(sys.executable).resolve()), 'python_sha256': digest(sys.executable)}, 'configuration': {'engine': args.engine, 'timeout_seconds': args.timeout, 'headless': True, 'isolated_profile': True}},
              'checks': [], 'observations': None}
    try:
        for root in (ASSETS, CORE / 'mioa-core/mioa_core', CORE / 'mioa-headless/mioa_headless', CORE / 'runtime/engine/src'):
            if not root.is_dir():
                raise FileNotFoundError(f'required identity root missing: {root}')
            for path in sorted(root.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py', '.js', '.json', '.test', '.zig', '.c', '.h'}:
                    report['identity']['source_files'][str(path.relative_to(CORE))] = digest(path)
        for path in (Path(__file__), HERE / 'run_web.py'):
            report['identity']['source_files'][str(path.relative_to(CORE))] = digest(path)
        manifest, data = payload()
        report['identity']['corpora'] = [manifest['corpus']]
        report['identity']['configuration']['normalization'] = manifest['normalization']
        save(args.output / 'payload.json', data)
        script = (ASSETS / 'cases.js').read_text()
        observed = await (mioa(args, script, data, report) if args.engine == 'mioa' else reference(args, script, data, report))
        report['observations'] = observed
        save(args.output / 'observations.json', observed)
        report['checks'] = checks_for(args, data, observed)
        statuses = [c['status'] for c in report['checks']]
        report['status'] = result_status(statuses)
    except (FileNotFoundError, importlib.metadata.PackageNotFoundError) as exc:
        report['checks'].append({'id': 'browser-execution', 'requirement': 'QV-DIFFERENTIAL', 'oracle': 'required actual isolated browser', 'status': 'NOT_RUN', 'detail': str(exc)})
    except Exception as exc:
        report['status'] = 'FAIL'
        report['checks'].append({'id': 'browser-execution', 'requirement': 'QV-DIFFERENTIAL', 'oracle': 'required actual isolated browser', 'status': 'FAIL', 'detail': f'{type(exc).__name__}: {exc}'})
        (args.output / 'error.txt').write_text(traceback.format_exc())
    save(args.output / 'report.json', report)
    (args.output / 'report.toon').write_text(toon.encode(report) + '\n')
    print(toon.encode({'status': report['status'], 'report': str(args.output / 'report.toon')}), flush=True)
    return 0 if report['status'] == 'PASS' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, choices=('mioa', 'chromium', 'firefox'))
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--binary-label', default='explicit-user-supplied')
    parser.add_argument('--executable', type=Path, help='explicit Playwright-compatible reference executable, never an existing profile')
    parser.add_argument('--png-decoder', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args(argv)
    if args.engine == 'mioa' and not args.binary:
        parser.error('--binary is required for mioa')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    args.output = args.output.expanduser().resolve()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('--output must be new or empty')
    args.output.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
