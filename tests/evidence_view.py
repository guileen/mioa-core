"""Agent-facing evidence projection.

`report.json` / `results.json` stay the validator's machine record. This view
contains exactly what a person or Agent needs to judge the run, and is encoded
as TOON so no one has to read the JSON intermediate.
"""
import hashlib
import json

import toon


def view(report):
    rows = report.get('results', [])
    out = {
        'status': report['status'],
        'mode': report.get('mode', report.get('suite', 'web')),
        'checks': len(rows),
    }
    if report.get('scope'):
        out['scope'] = report['scope']
    source = report.get('source') or {}
    if source:
        out['source_sha256'] = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    binary = report.get('binary') or source
    if binary.get('sha256') or binary.get('binary_sha256'):
        out['binary'] = {
            'sha256': binary.get('sha256') or binary['binary_sha256'],
            'source_binding': binary.get('source_binding', 'NOT_VERIFIED'),
        }
    error = report.get('error') or report.get('standards_error')
    if error:
        out['error'] = error.get('message', error) if isinstance(error, dict) else str(error)

    results = []
    for row in rows:
        child = row.get('result', {})
        records = child.get('results', [])
        observed = records[0] if len(records) == 1 else row
        details = observed.get('details', {})
        item = {'status': row['status'], 'id': row['id']}
        if 'tests_run' in child:
            item['tests'] = child['tests_run']
        subtests = details.get('subtests') if isinstance(details, dict) else None
        if not isinstance(subtests, list):
            subtests = None
        if isinstance(subtests, list) and all(isinstance(check, dict) for check in subtests):
            item['subtests'] = {
                'passed': sum(check.get('status') in (0, 'PASS') for check in subtests),
                'total': len(subtests),
            }
        for key in ('reason', 'message', 'shutdown_error'):
            value = row.get(key) or observed.get(key)
            if value:
                item[key] = str(value).replace('\n', ' ')
        skipped = child.get('skipped') or []
        if skipped:
            item['skipped'] = [{'id': entry['id'], 'reason': entry['reason']} for entry in skipped]
        if isinstance(details, dict):
            harness = details.get('harness') or {}
            if isinstance(harness, dict) and harness.get('message'):
                item['harness'] = str(harness['message']).replace('\n', ' ')
            failures = [{'status': check.get('status'), 'name': check.get('name'),
                         'message': str(check.get('message', '')).replace('\n', ' ')}
                        for check in subtests or []
                        if isinstance(check, dict) and check.get('status') not in (0, 'PASS')]
            if failures:
                item['failures'] = failures
        if row['status'] != 'PASS' and row.get('log'):
            item['log'] = row['log']
        results.append(item)
    out['results'] = results

    out['gaps'] = [{'id': gap['id'], 'reason': gap['reason']} for gap in report.get('gaps', [])]
    accepted = [{'id': issue['id'], 'until': issue['decision']['expires'],
                 'reason': issue['decision']['reason'], 'user_impact': issue['decision']['user_impact']}
                for issue in report.get('issues', []) if issue.get('accepted')]
    if accepted:
        out['accepted'] = accepted
    standards = {key: value for key, value in report.get('standards_checklist', {}).items()
                 if key in {'text', 'coverage'}}
    if standards:
        out['standards'] = standards
    return out


def render(report):
    return toon.encode(view(report)) + '\n'
