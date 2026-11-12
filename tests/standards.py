#!/usr/bin/env python3
"""Pinned Web standards TDD ledger; listing never downloads or executes tests.

Decisions schema: {"schema":1,"specs":{"SHORTNAME":DECISION}}.
DECISION is {"status":"放弃兼容","reason":"...","user_impact":"..."}
or {"status":"支持","scope":"finite behavioral boundary",
"required_checks":["web/case-id"],"reason":"why these checks cover that boundary"}.
Only fresh PASS evidence for every required, spec-related actual web check enables
scoped support, never whole-standard conformance. Unrelated/unit checks reject.
A VERIFIED binary plus a revalidated build.receipt is required for support.
Missing checks or unbound observations remain TODO; observed failures win.
--report accepts fresh CI report.toon (or report.json), not arbitrary test output.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlparse

import ci
import evidence_view
import toon

CORE = Path(__file__).resolve().parent.parent
CATALOG = CORE / 'tests/standards/catalog.json'
DECISIONS = CORE / 'tests/standards/decisions.json'
PRIORITIES = CORE / 'tests/standards/priorities.json'
PRIORITY_LEVELS = ('P0', 'P1', 'P2', 'P3')
STATUSES = ('支持', 'TODO', '放弃兼容', '测试失败')
FAILURES = {'FAIL', 'ERROR', 'CRASH', 'TIMEOUT'}
CHECK_STATUSES = FAILURES | {'PASS', 'NOT_RUN', 'INCOMPLETE', 'UNSUPPORTED'}
# Associations follow the assertions, not API names, inherited features, or
# dependency between specifications. No mapping is a conformance assertion.
RELATED_SPECS = {
    'web/fetch-basic': ('fetch',),
    'web/search-form-data': ('html', 'xhr'),
    'web/scroll-hit-test': ('cssom-view-1', 'css-overflow-3', 'css-position-3', 'geometry-1'),
    'web/flex-solid-paint': ('css-flexbox-1', 'css-display-3', 'css-position-3', 'selectors-3'),
    'web/input-focus': ('html', 'uievents', 'input-events-2'),
    'web/overflow-clip-paint': ('css-overflow-3', 'css-position-3'),
    'web/scaled-overflow-capture': ('css-overflow-3', 'css-position-3', 'css-backgrounds-3'),
    'web/layout-relative-units': ('css-values-3', 'css-values-4', 'cssom-view-1', 'css-display-3',
                                  'selectors-3', 'css-fonts-4', 'geometry-1', 'css-viewport-1', 'cssom-1'),
    'web/search-button-submit': ('html', 'url', 'encoding', 'referrer-policy'),
    'web/search-submit-enter': ('html', 'url', 'encoding', 'referrer-policy'),
    'web/search-return': ('html',),
    'web/history-form-return': ('html',),
    'web/history-form-js-return': ('html', 'ecmascript'),
    'web/webvtt-entities': ('webvtt1',),
    'web/canvas-pixels': ('html',),
    'web/canvas-solid-paint': ('html',),
    'web/canvas-png-roundtrip': ('html', 'png-3', 'png-4'),
    'web/wpt-dom-appendchild': ('dom', 'webidl'),
    'web/wpt-dom-removechild': ('dom', 'webidl'),
    'web/wpt-dom-contains': ('dom', 'webidl'),
    'web/wpt-dom-textcontent': ('dom', 'webidl', 'ecmascript'),
    'web/native-frame-contract': ('html', 'uievents', 'cssom-1'),
    'web/storage-local': ('storage', 'html'),
    'web/streams-readable': ('streams',),
    'web/cookie-store-crud': ('cookiestore', 'permissions'),
    'web/grid-layout-columns': ('css-grid-1', 'css-grid-2', 'css-display-3'),
    'web/a11y-aria-mapping': ('html-aria',),
    'web/css-text-decor': ('css-text-decor-3',),
    'web/websocket-interface': ('websockets',),
    'web/console-interface': ('console',),
    'web/compression-streams': ('compression',),
    'web/css-custom-properties': ('css-variables-1', 'css-variables-2'),
    'web/observer-fullscreen-webdriver': ('webdriver2', 'fullscreen', 'resize-observer-1', 'intersection-observer'),
    'web/file-api-runtime': ('FileAPI',),
    'web/css-effects-styling': ('css-color-3', 'css-color-4', 'css-transitions-1', 'css-animations-1',
        'css-box-3', 'css-box-4', 'css-backgrounds-3', 'css-images-3', 'css-transforms-1',
        'css-contain-1', 'css-contain-2', 'css-conditional-3', 'css-syntax-3',
        'css-align-3', 'css-sizing-3', 'css-sizing-4', 'css-masking-1',
        'css-multicol-1', 'css-multicol-2', 'css-shapes-1', 'css-ui-3', 'css-ui-4',
        'filter-effects-1', 'compositing-1', 'css-scroll-snap-1', 'css-will-change-1',
        'css-writing-modes-3', 'css-writing-modes-4', 'css-inline-3', 'css-lists-3',
        'css-counter-styles-3', 'css-overscroll-1', 'css-color-adjust-1', 'css-contain-3',
        'css-content-3', 'css-logical-1', 'css-style-attr', 'css-tables-3', 'css-transforms-2',
        'css-transitions-2', 'css-animations-2', 'css-easing-1', 'css-scrollbars-1'),
    'web/web-apis-runtime': ('urlpattern', 'beacon', 'web-animations-1'),
    'web/storage-indexeddb': ('IndexedDB-3',),
    'web/perf-timing-runtime': ('hr-time-3', 'user-timing'),
    'web/advanced-apis-runtime': ('selection-api', 'webaudio-1.0', 'webaudio-1.1', 'webcodecs'),
    'web/graphics-webgl-svg': ('webgl1', 'SVG2', 'DOM-Parsing', 'geolocation', 'WEBGL_debug_renderer_info'),
    'web/events-pointer-touch': ('pointerevents3', 'pointerevents4', 'touch-events', 'webcrypto-2'),
    'web/runtime-locks-timing': ('web-locks', 'performance-timeline', 'screen-orientation'),
    'web/platform-compat-media': ('notifications', 'DOM-Level-2-Style', 'uievents-code', 'uievents-key',
        'compat', 'quirks', 'mediaqueries-3', 'mediaqueries-4', 'mediaqueries-5',
        'device-memory-1', 'svg-integration', 'ecma-402', 'wasm-js-api-2'),
}
WPT_FEATURES = {'web/wpt-dom-appendchild': 'appendChild', 'web/wpt-dom-removechild': 'removeChild',
                'web/wpt-dom-contains': 'contains', 'web/wpt-dom-textcontent': 'textContent'}


def subtest_counts(row):
    child = row.get('result', {}).get('results', [])
    if len(child) != 1 or child[0].get('kind') != 'wpt':
        return None
    details = child[0].get('details')
    rows = details.get('subtests') if isinstance(details, dict) else None
    if not isinstance(rows, list) or not rows or any(
            not isinstance(item, dict) or type(item.get('status')) is not int
            or item['status'] not in {0, 1, 2, 3, 4} for item in rows):
        return None
    return {'total': len(rows), 'passed': sum(item.get('status') == 0 for item in rows)}

DECISION_SCHEMA = {
    '放弃兼容': {'required': ['status', 'reason', 'user_impact']},
    '支持': {'required': ['status', 'scope', 'required_checks', 'reason'],
           'meaning': '限定范围；required_checks 只能是 RELATED_SPECS 关联的实际 web 检查且全部新鲜 PASS，二进制来源 VERIFIED 且 build.receipt 当前有效；不代表整项标准兼容'},
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_object)


def load_report(path):
    path = Path(path)
    machine = path.with_suffix('.json') if path.suffix == '.toon' else path
    report = load_json(machine)
    _schema(report, 'CI report')
    readable = report.get('readable_report')
    if path.suffix == '.toon' and (not isinstance(readable, dict) or readable.get('path') != path.name):
        raise ValueError('TOON evidence is not bound to its machine record')
    if readable is not None:
        if not isinstance(readable, dict):
            raise ValueError('invalid readable evidence binding')
        name = _text(readable.get('path'), 'readable report path')
        if Path(name).name != name:
            raise ValueError('readable report path must be a sibling filename')
        if ci.sha(machine.parent / name) != readable.get('sha256'):
            raise ValueError('readable evidence changed; rerun tests')
        if (machine.parent / name).read_text() != evidence_view.render(report):
            raise ValueError('TOON evidence contradicts its machine record')
    return report


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be nonempty text')
    return value


def _hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError(f'{label} must be a SHA-256 digest')


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f'{label} must be a list of nonempty strings')
    if len(value) != len(set(value)):
        raise ValueError(f'{label} contains duplicates')
    return value


def _schema(value, label):
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
        raise ValueError(f'unknown {label} schema; expected schema 1')


def validate_catalog(catalog):
    _schema(catalog, 'catalog')
    source = catalog.get('source')
    if not isinstance(source, dict):
        raise ValueError('catalog source metadata is required')
    if source.get('url') != 'https://w3c.github.io/browser-specs/index.json':
        raise ValueError('catalog must identify the pinned w3c/browser-specs source')
    _hash(source.get('sha256'), 'catalog source.sha256')
    stamp = _text(source.get('retrieved_utc'), 'catalog source.retrieved_utc')
    try:
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError('not UTC')
    except ValueError as exc:
        raise ValueError('catalog retrieved_utc must be a UTC timestamp') from exc
    specs = catalog.get('specs')
    if not isinstance(specs, list) or not specs:
        raise ValueError('catalog specs must be a nonempty list')
    seen = set()
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError('catalog spec must be an object')
        for field in ('id', 'title', 'url', 'description', 'standing', 'module'):
            _text(spec.get(field), f'catalog spec {field}')
        if spec['id'] in seen:
            raise ValueError(f'duplicate catalog spec: {spec["id"]}')
        seen.add(spec['id'])
        if urlparse(spec['url']).scheme not in {'https', 'http'}:
            raise ValueError(f'invalid official URL: {spec["id"]}')
        _strings(spec.get('categories'), f'{spec["id"]} categories')
        for path in _strings(spec.get('wpt_paths'), f'{spec["id"]} wpt_paths'):
            if path.startswith('/') or '..' in Path(path).parts or '://' in path:
                raise ValueError(f'invalid WPT directory: {path}')
    return seen


def known_checks():
    return {row['id']: row for row in ci.plan(list(ci.MODULES), broad=True)}


def validate_decisions(decisions, spec_ids, checks):
    _schema(decisions, 'decisions')
    entries = decisions.get('specs')
    if not isinstance(entries, dict):
        raise ValueError('decisions.specs must be an object')
    if set(entries) - spec_ids:
        raise ValueError(f'unknown decision spec IDs: {sorted(set(entries) - spec_ids)}')
    for identifier, decision in entries.items():
        if not isinstance(decision, dict) or decision.get('status') not in DECISION_SCHEMA:
            raise ValueError(f'invalid decision: {identifier}')
        required = DECISION_SCHEMA[decision['status']]['required']
        if set(decision) != set(required):
            raise ValueError(f'{identifier} decision requires exactly {required}')
        for field in set(required) - {'required_checks'}:
            _text(decision.get(field), f'{identifier} decision {field}')
        if decision['status'] == '支持':
            ids = _strings(decision.get('required_checks'), f'{identifier} required_checks')
            if not ids or set(ids) - checks.keys():
                raise ValueError(f'{identifier} requires nonempty known check IDs')
            related = {key for key, identifiers in RELATED_SPECS.items() if identifier in identifiers}
            if set(ids) - related:
                raise ValueError(f'{identifier} required_checks must be related actual web checks, not unrelated/unit evidence')
    return entries


def _rows(rows, checks, label):
    if not isinstance(rows, list):
        raise ValueError(f'report {label} must be a list')
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('id') not in checks:
            raise ValueError(f'unknown report {label} check ID')
        key = row['id']
        if key in indexed:
            raise ValueError(f'duplicate report {label} check: {key}')
        if row.get('kind') != checks[key]['kind'] or row.get('name') != checks[key]['name']:
            raise ValueError(f'contradictory check identity: {key}')
        indexed[key] = row
    return indexed


def validate_report(report, checks, root=CORE):
    _schema(report, 'CI report')
    source = report.get('source')
    if not isinstance(source, dict) or not source:
        raise ValueError('report.source input hashes are required')
    for name, digest in source.items():
        _hash(digest, f'report.source[{name}]')
        path = root / name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'invalid source path: {name}')
        if not path.is_file() or ci.sha(path) != digest:
            raise ValueError(f'stale report source: {name}')
    if source != ci.inventory():
        raise ValueError('stale report: source input inventory differs from current repository')
    binary = report.get('binary')
    if binary is not None:
        if not isinstance(binary, dict):
            raise ValueError('invalid report binary identity')
        _hash(binary.get('sha256'), 'report binary.sha256')
        path = Path(_text(binary.get('path'), 'report binary.path'))
        if not path.is_absolute():
            path = root / path
        if not path.is_file() or ci.sha(path) != binary['sha256']:
            raise ValueError('stale report binary')
        binding = binary.get('source_binding', 'NOT_VERIFIED')
        if binding not in {'VERIFIED', 'NOT_VERIFIED'}:
            raise ValueError('unknown binary source binding')
        if binding == 'VERIFIED':
            build = report.get('build')
            if not isinstance(build, dict) or not build.get('receipt'):
                raise ValueError('verified binary requires a revalidatable build receipt')
            receipt = Path(_text(build['receipt'], 'build.receipt'))
            if not receipt.is_absolute():
                receipt = root / receipt
            if ci.bind_binary(path, receipt) != build:
                raise ValueError('stale or contradictory binary build binding')
    plan = _rows(report.get('plan'), checks, 'plan')
    rows = _rows(report.get('results'), checks, 'results')
    if plan.keys() != rows.keys():
        raise ValueError('report results must cover every planned check, including NOT_RUN')
    for key, row in rows.items():
        if row.get('status') not in CHECK_STATUSES:
            raise ValueError(f'unknown check status: {key}')
        if row['status'] == 'PASS':
            if row.get('returncode', 0) != 0:
                raise ValueError(f'contradictory PASS exit code: {key}')
            if row['kind'] == 'web' and binary is None:
                raise ValueError(f'missing binary identity for executed web check: {key}')
            child = row.get('result')
            if row['kind'] == 'web' and child is None:
                raise ValueError(f'missing executed web child evidence: {key}')
            if child is not None:
                if not isinstance(child, dict) or child.get('status') != 'PASS':
                    raise ValueError(f'contradictory PASS child evidence: {key}')
                if row['kind'] == 'unit' and (type(child.get('tests_run')) is not int or child['tests_run'] < 1
                                              or any(child.get(k, 0) for k in ('failures', 'errors', 'skipped', 'expected_failures'))):
                    raise ValueError(f'contradictory PASS unit evidence: {key}')
                if row['kind'] == 'web':
                    records = child.get('results')
                    if not isinstance(records, list) or len(records) != 1 or records[0].get('id') != row['name'] or records[0].get('status') != 'PASS':
                        raise ValueError(f'contradictory PASS web evidence: {key}')
    if report.get('status') not in {'PASS', 'FAIL', 'ERROR', 'BLOCKED', 'INCOMPLETE', 'ACCEPTED_WITH_RISKS'}:
        raise ValueError('unknown overall CI report status')
    if report['status'] == 'PASS' and any(r['status'] != 'PASS' for r in rows.values()):
        raise ValueError('contradictory overall PASS with non-PASS results')
    return rows


def validate_priorities(priorities, spec_ids):
    if priorities is None:
        return {}
    _schema(priorities, 'priorities')
    defaults = priorities.get('defaults')
    overrides = priorities.get('specs')
    if not isinstance(defaults, dict) or set(defaults) != {'browser', 'other'} or not isinstance(overrides, dict):
        raise ValueError('priorities require browser/other defaults and specs overrides')
    if set(overrides) - spec_ids:
        raise ValueError('unknown priority spec IDs')
    for rule in list(defaults.values()) + list(overrides.values()):
        if not isinstance(rule, dict) or rule.get('priority') not in PRIORITY_LEVELS:
            raise ValueError('priority must be P0, P1, P2 or P3')
        _text(rule.get('reason'), 'priority reason')
    return priorities


def build_checklist(catalog, decisions, report=None, *, root=CORE, priorities=None):
    _schema(decisions, 'decisions')
    spec_ids = validate_catalog(catalog)
    priority_rules = validate_priorities(priorities, spec_ids)
    checks = known_checks() if report is not None or decisions.get('specs') else {}
    entries = validate_decisions(decisions, spec_ids, checks)
    results = validate_report(report, checks, root) if report is not None else {}
    binary = (report.get('binary') or {}) if report is not None else {}
    bound = bool(binary.get('source_binding') == 'VERIFIED'
                 and report.get('build') and report.get('status') != 'ERROR')
    specs = []
    for spec in catalog['specs']:
        identifier = spec['id']
        fallback = {'priority': 'P2' if 'browser' in spec['categories'] else 'P3',
                    'reason': '待目标用户任务证据确认优先级。'}
        rule = priority_rules.get('specs', {}).get(identifier) or priority_rules.get('defaults', {}).get(
            'browser' if 'browser' in spec['categories'] else 'other', fallback)
        decision = entries.get(identifier)
        required = decision.get('required_checks', []) if decision else []
        related = {key for key, ids in RELATED_SPECS.items() if identifier in ids}
        evidence = [dict(id=key, status=results[key]['status'],
                         scope='declared required check' if key in required else 'partial local regression only',
                         source_binding='VERIFIED' if bound else 'UNVERIFIED',
                         binary_sha256=binary.get('sha256'), subtests=subtest_counts(results[key]))
                    for key in sorted(related | set(required)) if key in results]
        failed = [e['id'] for e in evidence if e['status'] in FAILURES]
        missing = [key for key in required if key not in results or results[key]['status'] != 'PASS']
        status = 'TODO'
        gap = f'尚无覆盖《{spec["title"]}》的已声明有限范围及完整新鲜 PASS 证据；整项标准一致性未评估。'
        if failed:
            status = '测试失败'
            gap = f'《{spec["title"]}》相关实测失败：{", ".join(failed)}；不能用兼容决策隐藏失败。'
            if not bound:
                gap += ' 仅为未绑定当前构建的二进制观察；尚不能归因于当前引擎源码。'
        elif decision and decision['status'] == '放弃兼容':
            status = '放弃兼容'
            gap = f'{decision["reason"]}；用户影响：{decision["user_impact"]}；这不是测试通过。'
        elif decision and decision['status'] == '支持':
            if missing:
                gap = f'《{spec["title"]}》声明范围缺少实际 PASS：{", ".join(missing)}；未运行/不完整不计通过。'
            elif not bound:
                gap = f'《{spec["title"]}》局部检查通过，但缺少当前构建 receipt 的 VERIFIED 来源绑定；不得标记支持。'
            else:
                status = '支持'
                gap = f'只证明声明范围「{decision["scope"]}」；《{spec["title"]}》其余规范要求仍未评估。'
        elif evidence:
            gap += ' 已有本地局部证据，但不得据此宣称整项支持。'
        targets = ', '.join(spec['wpt_paths']) or f'从 {spec["url"]} 的规范要求选取可复现断言（目录源未提供 WPT 路径）'
        if failed:
            action = f'先以 {", ".join(failed)} 重现并修复失败，再为《{spec["title"]}》在 {targets} 增加失败先行断言并重跑。'
        elif status == '放弃兼容':
            action = f'对《{spec["title"]}》维护上述用户影响边界；需求改变时移除放弃决策，在 {targets} 先加入失败测试。'
        elif status == '支持':
            action = f'保留 {", ".join(required)} 回归；下一条《{spec["title"]}》范围外要求从 {targets} 先写失败测试，再扩展范围和 required_checks。'
        else:
            action = f'阅读《{spec["title"]}》({spec["url"]})，为 {spec["module"]} 模块选定有限行为范围；从 {targets} 先写/导入失败测试，实现后逐项重跑，在 RELATED_SPECS 登记规范关联并登记 required_checks。'
            if missing:
                action = f'先执行或修复 {", ".join(missing)}。' + action
            elif decision and decision['status'] == '支持' and not bound:
                action = '先使用当前构建 receipt 运行 make test LEVEL=2 BINARY=PATH RECEIPT=PATH，取得可重验来源绑定的报告。' + action
        scope = decision.get('scope') if decision else None
        specs.append(dict(spec, status=status,
                          priority=rule['priority'], priority_reason=rule['reason'],
                          status_label=(f'TODO {rule["priority"]}' if status == 'TODO' else
                                        f'支持（限定范围：{scope}；非整项一致性）' if status == '支持' else status),
                          scope=scope, required_checks=required, evidence=evidence, gap=gap,
                          next_action=action, decision=decision))
    return {'schema': 1, 'source': dict(catalog['source']), 'catalog_count': len(specs),
            'selected_count': len(specs), 'status_counts': {s: sum(row['status'] == s for row in specs) for s in STATUSES},
            'todo_priorities': {p: sum(r['status'] == 'TODO' and r['priority'] == p for r in specs) for p in PRIORITY_LEVELS},
            'scope': '完整 pinned w3c/browser-specs catalog（全部类别）；不是所有时代/机构的无限 Web 标准全集。支持仅限明确声明的有限范围，不是整项标准一致性。',
            'evidence': {'provided': report is not None, 'scope': 'fresh repository CI regressions; partial evidence only',
                         'report_status': report.get('status') if report is not None else None,
                         'source_binding': 'VERIFIED' if bound else 'UNVERIFIED',
                         'unmapped_checks': sorted(key for key in results if key not in RELATED_SPECS)},
            'decision_schema': DECISION_SCHEMA, 'specs': specs}


def select_checklist(ledger, spec_ids=(), modules=(), priorities=()):
    unknown = set(spec_ids) - {r['id'] for r in ledger['specs']}
    unknown_modules = set(modules) - {r['module'] for r in ledger['specs']}
    if unknown or unknown_modules:
        raise ValueError(f'unknown spec/module filters: {sorted(unknown | unknown_modules)}')
    if set(priorities) - set(PRIORITY_LEVELS):
        raise ValueError('unknown priority filter')
    rows = [r for r in ledger['specs'] if (not spec_ids or r['id'] in spec_ids)
            and (not modules or r['module'] in modules) and (not priorities or r['priority'] in priorities)]
    return dict(ledger, specs=rows, selected_count=len(rows),
                status_counts={s: sum(row['status'] == s for row in rows) for s in STATUSES},
                todo_priorities={p: sum(r['status'] == 'TODO' and r['priority'] == p for r in rows) for p in PRIORITY_LEVELS})


def _md(value):
    return str(value).replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace('<', '&lt;').replace('>', '&gt;').replace('\n', ' ')


def render_markdown(ledger):
    counts = ' | '.join(f'{s} {n}' for s, n in ledger['status_counts'].items())
    lines = [f'{ledger["catalog_count"]} 标准 | 显示 {ledger["selected_count"]} | {counts}']
    for row in sorted(ledger['specs'], key=lambda item: (item['priority'], item['id'])):
        evidence = row['evidence']
        if row['status'] == '支持':
            detail = '范围：' + row['scope']
        elif row['status'] == '放弃兼容':
            detail = row['decision']['reason'] + '；影响：' + row['decision']['user_impact']
        elif evidence:
            outcomes = []
            for item in evidence:
                label = WPT_FEATURES.get(item['id'], item['id'])
                counts = item.get('subtests')
                score = f'({counts["passed"]}/{counts["total"]} PASS)' if counts else ''
                outcomes.append(f'{label}={item["status"]}{score}')
            detail = ', '.join(outcomes)
            if row['status'] == 'TODO':
                detail += '；范围未验收'
            if ledger['evidence']['source_binding'] != 'VERIFIED':
                detail += '；未绑定当前构建'
        else:
            detail = '未测'
            if row['wpt_paths']:
                detail += '；WPT: ' + ', '.join(row['wpt_paths'])
        mark = 'x' if row['status'] in {'支持', '放弃兼容'} else ' '
        lines.append(f'- [{mark}] {_md(row["status_label"])} | {_md(row["title"])}'
                     f' (`{row["id"]}`) | {_md(detail)}')
    return '\n'.join(lines) + '\n'


def coverage_inventory(ledger):
    checks = known_checks()
    rows = []
    for spec in ledger['specs']:
        associated = sorted(key for key, ids in RELATED_SPECS.items()
                            if spec['id'] in ids and key in checks)
        decision = spec['decision']
        rows.append({'id': spec['id'], 'priority': spec['priority'], 'checks': associated,
                     'scope_declared': decision is not None,
                     'intent': decision['status'] if decision else None})
    groups = {}
    for priority in PRIORITY_LEVELS:
        selected = [row for row in rows if row['priority'] == priority]
        groups[priority] = {
            'standards': len(selected),
            'tests_registered': sum(bool(row['checks']) for row in selected),
            'scope_declared': sum(row['scope_declared'] for row in selected),
            'tests_missing': [row['id'] for row in selected if not row['checks']],
            'scope_missing': [row['id'] for row in selected if not row['scope_declared']],
        }
    return {'schema': 1, 'catalog_count': ledger['catalog_count'], 'selected_count': len(rows),
            'tests_registered': sum(bool(row['checks']) for row in rows),
            'checks_registered': len({key for row in rows for key in row['checks']}),
            'scope_declared': sum(row['scope_declared'] for row in rows),
            'by_priority': groups, 'specs': rows}


def render_coverage(inventory):
    total = inventory['selected_count']
    tested = inventory['tests_registered']
    declared = inventory['scope_declared']
    lines = [f'标准 {inventory["catalog_count"]} | 本次 {total} | 已关联测试 {tested}'
             f'（{inventory["checks_registered"]} 用例）| 未关联 {total - tested}'
             f' | 兼容范围已描述 {declared} | 待描述 {total - declared}']
    for priority, group in inventory['by_priority'].items():
        if not group['standards']:
            continue
        lines.append(f'{priority}: {group["standards"]} 项 | 未关联测试 {len(group["tests_missing"])}'
                     f' | 待定范围 {len(group["scope_missing"])}')
    urgent = next((group for group in inventory['by_priority'].values() if group['standards']), {})
    if urgent.get('tests_missing'):
        lines.append('先核对/补测试登记：' + ', '.join(urgent['tests_missing']))
    scoped = [row['id'] for row in inventory['specs'] if row['checks'] and not row['scope_declared']
              and row['priority'] == next((p for p, g in inventory['by_priority'].items() if g['standards']), None)]
    if scoped:
        lines.append('已有用例，先定兼容范围：' + ', '.join(scoped))
    lines.append('登记不等于充分覆盖；先复用/补测试与范围，再按实测失败修实现。')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--format', choices=('markdown', 'toon', 'json'), default='toon')
    parser.add_argument('--coverage', action='store_true', help='audit test registration and compatibility-scope gaps; does not run product tests')
    parser.add_argument('--spec', action='append', default=[], metavar='SHORTNAME', help='repeatable exact catalog ID filter')
    parser.add_argument('--module', action='append', default=[], metavar='NAME', help='repeatable catalog module filter; combines with --spec')
    parser.add_argument('--priority', action='append', choices=PRIORITY_LEVELS, default=[], help='repeatable work-priority filter')
    parser.add_argument('--report', type=Path, help='fresh CI report.toon; validator reads its bound machine record, never runs tests')
    args = parser.parse_args(argv)
    try:
        ledger = build_checklist(load_json(CATALOG), load_json(DECISIONS), load_report(args.report) if args.report else None,
                                 priorities=load_json(PRIORITIES))
        ledger = select_checklist(ledger, args.spec, args.module, args.priority)
        if args.coverage:
            inventory = coverage_inventory(ledger)
            if args.format == 'json':
                print(json.dumps(inventory, ensure_ascii=False, indent=2))
            elif args.format == 'toon':
                output_inventory = dict(inventory)
                if not args.spec and len(inventory['specs']) > 20:
                    output_inventory['specs'] = [row for row in inventory['specs'] if row['checks'] or row['scope_declared']]
                print(toon.encode(output_inventory))
            else:
                print(render_coverage(inventory), end='')
        else:
            if args.format == 'json':
                print(json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False))
            elif args.format == 'toon':
                print(toon.encode(ledger))
            else:
                print(render_markdown(ledger), end='')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
