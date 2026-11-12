"""Executable checklist boundaries: coverage, scoped evidence and stale reports."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ci
import standards


class StandardsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'tests').mkdir()
        source = self.root / 'tests/input.py'
        source.write_text('current input\n')
        self.source = {'tests/input.py': ci.sha(source)}
        runtime = self.root / 'runtime'
        runtime.mkdir()
        runtime_source = runtime / 'engine.zig'
        runtime_source.write_text('current runtime\n')
        source_rows = [{'path': 'runtime/engine.zig', 'sha256': ci.sha(runtime_source)}]
        for name in ci.OS_BUILD_SOURCES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'fixture source: {name}\n')
            source_rows.append({'path': name, 'sha256': ci.sha(path)})
        self.binary = self.root / 'engine'
        self.binary.write_bytes(b'current binary fixture')
        self.receipt = self.root / 'receipt.json'
        self.receipt.write_text(json.dumps({
            'status': 'completed', 'binary': {'sha256': ci.sha(self.binary)},
            'sources': {'files': source_rows},
        }))
        self.checks = {
            key: {'id': key, 'kind': 'web', 'name': key.removeprefix('web/'), 'modules': ['dom']}
            for key in standards.RELATED_SPECS
        }
        self.checks['unit/test_ci'] = {'id': 'unit/test_ci', 'kind': 'unit', 'name': 'test_ci', 'modules': ['harness']}
        for patcher in (patch.object(standards, 'known_checks', return_value=self.checks),
                        patch.object(ci, 'inventory', return_value=self.source),
                        patch.object(ci, 'CORE', self.root)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.catalog = {'schema': 1, 'source': {
            'url': 'https://w3c.github.io/browser-specs/index.json',
            'retrieved_utc': '2026-11-08T00:00:00Z', 'sha256': 'a' * 64},
            'specs': [self.spec('html', 'dom'), self.spec('dom', 'dom'),
                      self.spec('css-flexbox-1', 'layout'), self.spec('non-browser-spec', 'other')]}
        self.decisions = {'schema': 1, 'specs': {}}
        self.priorities = {'schema': 1, 'defaults': {
            'browser': {'priority': 'P2', 'reason': 'Target-task evidence pending'},
            'other': {'priority': 'P3', 'reason': 'Peripheral scope'}},
            'specs': {'html': {'priority': 'P0', 'reason': 'Core navigation and page tasks'}}}

    def spec(self, identifier, module):
        return {'id': identifier, 'title': 'Official ' + identifier, 'url': 'https://example.org/' + identifier,
                'description': 'Official publication metadata for ' + identifier,
                'categories': ['browser'] if module != 'other' else [], 'standing': 'good',
                'wpt_paths': [identifier + '/'] if module != 'other' else [], 'module': module}

    def support(self, identifier='html', required=None):
        self.decisions['specs'][identifier] = {
            'status': '支持', 'scope': 'Local input-focus fixture: focus transfers between the two tested controls only',
            'reason': 'The declared assertion is exercised by the exact input-focus web regression',
            'required_checks': required if required is not None else ['web/input-focus']}
        return self.decisions

    def decline(self, identifier='html'):
        self.decisions['specs'][identifier] = {
            'status': '放弃兼容', 'reason': 'No target task requires this behavior',
            'user_impact': 'Pages needing this behavior are explicitly unsupported'}
        return self.decisions

    def report(self, statuses=None, *, bound=False):
        statuses = statuses if statuses is not None else {'web/input-focus': 'PASS'}
        plan = [dict(self.checks[key]) for key in statuses]
        rows = []
        for key, status in statuses.items():
            row = dict(self.checks[key], status=status)
            if status == 'PASS':
                row['returncode'] = 0
                row['result'] = {'status': 'PASS'}
                if row['kind'] == 'web':
                    row['result']['results'] = [{'id': row['name'], 'status': 'PASS'}]
                else:
                    row['result']['tests_run'] = 1
            rows.append(row)
        result = {'schema': 1, 'source': dict(self.source), 'plan': plan, 'results': rows,
                  'status': 'FAIL' if any(s in standards.FAILURES for s in statuses.values()) else
                            'INCOMPLETE' if any(s != 'PASS' for s in statuses.values()) else 'PASS',
                  'binary': {'path': str(self.binary), 'sha256': ci.sha(self.binary),
                             'source_binding': 'VERIFIED' if bound else 'NOT_VERIFIED'}}
        if bound:
            result['build'] = ci.bind_binary(self.binary, self.receipt)
        return result

    def ledger(self, report=None):
        return standards.build_checklist(self.catalog, self.decisions, report, root=self.root)

    def item(self, ledger, identifier='html'):
        return next(row for row in ledger['specs'] if row['id'] == identifier)

    def test_all_catalog_entries_including_non_browser_get_actionable_todo(self):
        ledger = self.ledger()
        self.assertEqual(ledger['catalog_count'], len(self.catalog['specs']))
        self.assertEqual(ledger['selected_count'], len(self.catalog['specs']))
        self.assertEqual([r['id'] for r in ledger['specs']], [r['id'] for r in self.catalog['specs']])
        self.assertEqual(ledger['status_counts']['TODO'], len(self.catalog['specs']))
        markdown = standards.render_markdown(ledger)
        for row in ledger['specs']:
            self.assertEqual(row['status'], 'TODO')
            self.assertIn(row['title'], row['gap'])
            self.assertIn(row['title'], row['next_action'])
            self.assertIn(row['url'], row['next_action'])
            self.assertIn(row['module'], row['next_action'])
            self.assertIn(f'(`{row["id"]}`)', markdown)

    def test_real_pinned_catalog_has_no_silent_omissions(self):
        catalog = standards.load_json(standards.CATALOG)
        ledger = standards.build_checklist(catalog, {'schema': 1, 'specs': {}})
        self.assertEqual(len(catalog['specs']), 831)
        self.assertEqual(ledger['catalog_count'], 831)
        self.assertEqual({s['id'] for s in ledger['specs']}, {s['id'] for s in catalog['specs']})
        self.assertEqual(ledger['status_counts'], {'支持': 0, 'TODO': 831, '放弃兼容': 0, '测试失败': 0})

    def test_partial_pass_never_promotes_whole_standard(self):
        for bound in (False, True):
            with self.subTest(bound=bound):
                row = self.item(self.ledger(self.report(bound=bound)))
                self.assertEqual(row['status'], 'TODO')
                self.assertEqual(row['evidence'][0]['status'], 'PASS')

    def test_scoped_support_is_reachable_with_related_pass_and_current_build(self):
        self.support()
        ledger = self.ledger(self.report(bound=True))
        row = self.item(ledger)
        self.assertEqual(row['status'], '支持')
        self.assertEqual(row['required_checks'], ['web/input-focus'])
        markdown = standards.render_markdown(ledger)
        self.assertIn(row['scope'], markdown)
        self.assertEqual(self.item(ledger, 'dom')['status'], 'TODO')

    def test_unbound_pass_cannot_support_and_is_labelled(self):
        self.support()
        row = self.item(self.ledger(self.report()))
        self.assertEqual(row['status'], 'TODO')
        self.assertEqual(row['evidence'][0]['source_binding'], 'UNVERIFIED')

    def test_unrelated_or_mock_unit_checks_cannot_prove_support(self):
        for required in (['unit/test_ci'], ['web/flex-solid-paint'], ['web/unknown'], []):
            with self.subTest(required=required):
                self.support(required=required)
                with self.assertRaises(ValueError):
                    self.ledger(self.report({'unit/test_ci': 'PASS'}, bound=True))

    def test_unexercised_security_and_protocol_scopes_cannot_be_promoted(self):
        cases = (
            ('rfc6797', 'web/platform-compat-media'),
            ('sri-2', 'web/platform-compat-media'),
            ('document-policy', 'web/platform-compat-media'),
            ('rfc6455', 'web/websocket-interface'),
            ('rfc7932', 'web/compression-streams'),
            ('clear-site-data', 'web/cookie-store-crud'),
            ('service-workers', 'web/advanced-apis-runtime'),
            ('GIF', 'web/advanced-apis-runtime'),
            ('accname-1.2', 'web/a11y-aria-mapping'),
            ('css-cascade-3', 'web/css-effects-styling'),
            ('css-cascade-4', 'web/css-effects-styling'),
        )
        self.catalog['specs'].extend(self.spec(identifier, 'network') for identifier, _ in cases)
        for identifier, check in cases:
            with self.subTest(spec=identifier):
                report = self.report({check: 'PASS'}, bound=True)
                self.decisions = {'schema': 1, 'specs': {}}
                row = self.item(self.ledger(report), identifier)
                self.assertEqual(row['status'], 'TODO')
                self.assertEqual(row['evidence'], [])
                self.support(identifier, [check])
                with self.assertRaises(ValueError):
                    self.ledger(report)

    def test_missing_not_run_incomplete_and_unsupported_checks_remain_todo(self):
        self.support()
        for statuses in ({}, {'web/input-focus': 'NOT_RUN'}, {'web/input-focus': 'INCOMPLETE'},
                         {'web/input-focus': 'UNSUPPORTED'}):
            with self.subTest(statuses=statuses):
                self.assertEqual(self.item(self.ledger(self.report(statuses, bound=True)))['status'], 'TODO')
        self.assertEqual(self.item(self.ledger())['status'], 'TODO')

    def test_every_actual_failure_overrides_support_and_decline(self):
        for status in standards.FAILURES:
            for decision in (self.support, self.decline):
                with self.subTest(status=status, decision=decision.__name__):
                    decision()
                    row = self.item(self.ledger(self.report({'web/input-focus': status}, bound=True)))
                    self.assertEqual(row['status'], '测试失败')
                    self.assertEqual(row['evidence'][0]['status'], status)
                    self.assertIsNotNone(row['decision'])
                    self.assertIn('web/input-focus', row['next_action'])

    def test_failure_in_other_related_check_also_overrides_declared_pass(self):
        self.support()
        row = self.item(self.ledger(self.report({'web/input-focus': 'PASS', 'web/canvas-pixels': 'FAIL'}, bound=True)))
        self.assertEqual(row['status'], '测试失败')

    def test_unbound_failure_is_observation_not_current_source_attribution(self):
        row = self.item(self.ledger(self.report({'web/input-focus': 'CRASH'})))
        self.assertEqual(row['status'], '测试失败')
        self.assertEqual(row['evidence'][0]['source_binding'], 'UNVERIFIED')

    def test_decline_requires_explicit_reason_and_user_impact(self):
        self.decline()
        row = self.item(self.ledger())
        self.assertEqual(row['status'], '放弃兼容')
        for field in ('reason', 'user_impact'):
            saved = self.decisions['specs']['html'][field]
            self.decisions['specs']['html'][field] = ' '
            with self.assertRaises(ValueError):
                self.ledger()
            self.decisions['specs']['html'][field] = saved

    def test_unknown_decision_spec_and_schema_reject(self):
        self.decline('unknown-spec')
        with self.assertRaises(ValueError):
            self.ledger()
        self.decisions = {'schema': 2, 'specs': {}}
        with self.assertRaises(ValueError):
            self.ledger()

    def test_report_unknown_schema_check_and_status_reject(self):
        for change in ('schema', 'id', 'status'):
            with self.subTest(change=change):
                report = self.report()
                if change == 'schema':
                    report['schema'] = 2
                else:
                    report['results'][0][change] = 'UNKNOWN'
                with self.assertRaises(ValueError):
                    self.ledger(report)

    def test_missing_source_binary_or_child_evidence_rejects(self):
        for field in ('source', 'binary', 'result'):
            with self.subTest(field=field):
                report = self.report()
                del (report['results'][0] if field == 'result' else report)[field]
                with self.assertRaises(ValueError):
                    self.ledger(report)

    def test_stale_source_hash_and_added_inputs_reject(self):
        report = self.report()
        (self.root / 'tests/input.py').write_text('changed\n')
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.ledger(report)
        (self.root / 'tests/input.py').write_text('current input\n')
        with patch.object(ci, 'inventory', return_value=dict(self.source, added='b' * 64)):
            with self.assertRaisesRegex(ValueError, 'inventory'):
                self.ledger(report)

    def test_stale_binary_receipt_and_runtime_binding_reject(self):
        for changed in ('binary', 'receipt', 'runtime'):
            with self.subTest(changed=changed):
                report = self.report(bound=True)
                path = {'binary': self.binary, 'receipt': self.receipt,
                        'runtime': self.root / 'runtime/engine.zig'}[changed]
                saved = path.read_bytes()
                path.write_bytes(saved + b' ')
                try:
                    with self.assertRaises(ValueError):
                        self.ledger(report)
                finally:
                    path.write_bytes(saved)

    def test_claimed_verified_without_receipt_rejects(self):
        report = self.report(bound=True)
        del report['build']['receipt']
        with self.assertRaises(ValueError):
            self.ledger(report)

    def test_missing_results_duplicate_ids_and_identity_conflicts_reject(self):
        for change in ('missing', 'duplicate', 'identity'):
            with self.subTest(change=change):
                report = self.report()
                if change == 'missing':
                    report['results'] = []
                elif change == 'duplicate':
                    report['results'].append(deepcopy(report['results'][0]))
                else:
                    report['results'][0]['name'] = 'another-case'
                with self.assertRaises(ValueError):
                    self.ledger(report)

    def test_contradictory_pass_evidence_rejects(self):
        for change in ('exit', 'child', 'child_case', 'overall'):
            with self.subTest(change=change):
                report = self.report()
                row = report['results'][0]
                if change == 'exit':
                    row['returncode'] = 1
                elif change == 'child':
                    row['result']['status'] = 'FAIL'
                elif change == 'child_case':
                    row['result']['results'][0]['status'] = 'FAIL'
                else:
                    row['status'] = 'NOT_RUN'
                with self.assertRaises(ValueError):
                    self.ledger(report)

    def test_aborted_report_keeps_not_run_todo(self):
        report = self.report({'web/input-focus': 'NOT_RUN'})
        report.pop('binary')
        report['status'] = 'ERROR'
        self.assertEqual(self.item(self.ledger(report))['status'], 'TODO')

    def test_filters_are_explicit_and_preserve_full_catalog_count(self):
        ledger = standards.select_checklist(self.ledger(), ['html', 'dom'], ['dom'])
        self.assertEqual(ledger['selected_count'], 2)
        self.assertEqual(ledger['catalog_count'], 4)
        self.assertEqual(sum(ledger['status_counts'].values()), 2)
        for specs, modules in ((['unknown'], []), ([], ['unknown'])):
            with self.assertRaises(ValueError):
                standards.select_checklist(self.ledger(), specs, modules)

    def test_duplicate_catalog_ids_and_json_keys_reject(self):
        self.catalog['specs'].append(deepcopy(self.catalog['specs'][0]))
        with self.assertRaises(ValueError):
            self.ledger()
        path = self.root / 'duplicate.json'
        path.write_text('{"schema":1,"schema":2}')
        with self.assertRaises(ValueError):
            standards.load_json(path)

    def test_toon_report_is_consumable_but_modified_evidence_is_rejected(self):
        import evidence_view
        report = self.report()
        readable = self.root / 'report.toon'
        readable.write_text(evidence_view.render(report))
        report['readable_report'] = {'path': readable.name, 'sha256': ci.sha(readable)}
        (self.root / 'report.json').write_text(json.dumps(report))
        loaded = standards.load_report(readable)
        self.assertEqual(self.item(self.ledger(loaded))['status'], 'TODO')
        original = report['status']
        report['status'] = 'FAIL' if original != 'FAIL' else 'PASS'
        (self.root / 'report.json').write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            standards.load_report(readable)
        report['status'] = original
        (self.root / 'report.json').write_text(json.dumps(report))
        readable.write_text('PASS everything\\n')
        with self.assertRaises(ValueError):
            standards.load_report(readable)

    def test_wpt_numeric_counts_do_not_misread_local_assertion_statuses(self):
        child = {'kind': 'assertion', 'details': {'subtests': [{'name': 'local', 'status': 'PASS'}]}}
        row = {'result': {'results': [child]}}
        self.assertIsNone(standards.subtest_counts(row))
        child.update(kind='wpt', details={'subtests': [{'name': 'a', 'status': 0}, {'name': 'b', 'status': 1}]})
        self.assertEqual(standards.subtest_counts(row), {'total': 2, 'passed': 1})
        child['details']['subtests'][0]['status'] = 'PASS'
        self.assertIsNone(standards.subtest_counts(row))

    def test_coverage_counts_registration_and_scope_separately_from_results(self):
        inventory = standards.coverage_inventory(self.ledger())
        self.assertEqual(inventory['selected_count'], 4)
        self.assertEqual(inventory['tests_registered'], 3)
        self.assertEqual(inventory['scope_declared'], 0)
        self.decline('html')
        inventory = standards.coverage_inventory(self.ledger())
        self.assertEqual(inventory['tests_registered'], 3)
        self.assertEqual(inventory['scope_declared'], 1)
        missing = next(row for row in inventory['specs'] if row['id'] == 'non-browser-spec')
        self.assertEqual(missing['checks'], [])
        self.assertFalse(missing['scope_declared'])

    def test_priority_override_filter_and_todo_counts(self):
        ledger = standards.build_checklist(self.catalog, self.decisions, priorities=self.priorities)
        self.assertEqual(self.item(ledger)['priority'], 'P0')
        self.assertEqual(ledger['todo_priorities'], {'P0': 1, 'P1': 0, 'P2': 2, 'P3': 1})
        selected = standards.select_checklist(ledger, priorities=['P0'])
        self.assertEqual([row['id'] for row in selected['specs']], ['html'])
        self.assertEqual(selected['catalog_count'], 4)
        self.assertEqual(selected['todo_priorities']['P0'], 1)
        for invalid in ('P9', '', None):
            self.priorities['specs']['html']['priority'] = invalid
            with self.assertRaises(ValueError):
                standards.build_checklist(self.catalog, self.decisions, priorities=self.priorities)

    def test_cli_defaults_to_complete_markdown_without_network_or_runner(self):
        with patch.object(standards, 'CATALOG', self.root / 'catalog.json'), \
             patch.object(standards, 'DECISIONS', self.root / 'decisions.json'), \
             patch.object(standards, 'PRIORITIES', self.root / 'priorities.json'), \
             patch.object(standards, 'known_checks', side_effect=AssertionError('must not run discovery')):
            standards.CATALOG.write_text(json.dumps(self.catalog))
            standards.DECISIONS.write_text(json.dumps(self.decisions))
            standards.PRIORITIES.write_text(json.dumps(self.priorities))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(standards.main([]), 0)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(standards.main(['--format', 'json', '--spec', 'html']), 0)
            self.assertEqual(json.loads(stdout.getvalue())['selected_count'], 1)


if __name__ == '__main__':
    unittest.main()
