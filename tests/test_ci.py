"""Gate boundaries and real subprocess failures, not copied command wiring."""
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import ci


class GateTests(unittest.TestCase):
    def decision(self, identifier):
        return {'id': identifier, 'reason': 'Deferred feature costs exceed current benefit',
                'user_impact': 'No video tasks are promised in this release',
                'expires': (date.today() + timedelta(days=1)).isoformat()}

    def test_gap_requires_explicit_current_decision_for_deployment(self):
        gap = {'id': 'gap/media', 'status': 'NOT_RUN', 'reason': 'No media corpus'}
        self.assertEqual(ci.assess([], [gap], 'deploy', [])[0], 'BLOCKED')
        status, issues = ci.assess([], [gap], 'deploy', [self.decision(gap['id'])])
        self.assertEqual(status, 'ACCEPTED_WITH_RISKS')
        self.assertTrue(issues[0]['accepted'])
        expired = self.decision(gap['id'])
        expired['expires'] = '2000-01-01'
        with self.assertRaises(ValueError):
            ci.assess([], [gap], 'deploy', [expired])

    def test_infrastructure_and_unit_failures_cannot_be_waived(self):
        for kind, status in [('source', 'FAIL'), ('unit', 'FAIL'), ('web', 'CRASH'), ('web', 'ERROR'), ('web', 'TIMEOUT'), ('web', 'NOT_RUN')]:
            with self.subTest(kind=kind, status=status):
                row = {'id': kind + '/broken', 'kind': kind, 'status': status}
                verdict, issues = ci.assess([row], [], 'deploy', [self.decision(row['id'])])
                self.assertEqual(verdict, 'BLOCKED')
                self.assertFalse(issues[0]['accepted'])

    def test_native_authority_failure_cannot_be_accepted_as_compatibility(self):
        row = {'id': 'web/native-frame-contract', 'kind': 'web', 'status': 'FAIL'}
        self.assertEqual(ci.assess([row], [], 'deploy', [self.decision(row['id'])])[0], 'BLOCKED')

    def test_web_semantic_failure_can_be_explicitly_accepted_without_becoming_pass(self):
        row = {'id': 'web/video', 'kind': 'web', 'status': 'FAIL'}
        status, issues = ci.assess([row], [], 'deploy', [self.decision(row['id'])])
        self.assertEqual(status, 'ACCEPTED_WITH_RISKS')
        self.assertEqual(issues[0]['status'], 'FAIL')
        with self.assertRaises(ValueError):
            ci.assess([], [], 'deploy', [self.decision(row['id'])])

    def test_diagnostic_missing_evidence_is_not_pass(self):
        row = {'id': 'web/input', 'status': 'NOT_RUN', 'kind': 'web'}
        self.assertEqual(ci.assess([row], [], 'diagnose', [])[0], 'INCOMPLETE')


class NextActionTests(unittest.TestCase):
    def report(self, level, rows):
        return {'level': level, 'results': rows, 'gaps': [], 'status': 'INCOMPLETE',
                'scope': 'test scope'}

    def test_empty_execution_cannot_promote(self):
        self.assertEqual(ci.next_action(self.report(1, []))['action'],
                         'repair-missing-execution-evidence')

    def test_source_failure_prevents_promotion_even_with_passing_units(self):
        report = self.report(2, [
            {'id': 'source/boundary', 'kind': 'source', 'status': 'FAIL'},
            {'id': 'unit/ok', 'kind': 'unit', 'status': 'PASS'}])
        self.assertEqual(ci.next_action(report)['action'], 'review-and-fix-os-boundary')

    def test_skipped_units_require_evidence_not_higher_level(self):
        report = self.report(1, [{'id': 'unit/skip', 'kind': 'unit', 'status': 'INCOMPLETE'}])
        action = ci.next_action(report)
        self.assertEqual(action['action'], 'resolve-first-failure-or-missing-evidence')
        self.assertEqual(action['check'], 'unit/skip')

    def test_runtime_prerequisite_is_distinct_from_product_failure(self):
        report = self.report(2, [{'id': 'web/case', 'kind': 'web', 'status': 'NOT_RUN'}])
        self.assertEqual(ci.next_action(report)['required'], ['BINARY'])

    def test_basic_success_requests_runtime_evidence_not_delivery_approval(self):
        report = self.report(1, [{'id': 'unit/ok', 'kind': 'unit', 'status': 'PASS'}])
        self.assertEqual(ci.next_action(report)['action'], 'run-runtime-level')


class IdentityTests(unittest.TestCase):
    def test_build_binding_rejects_changed_and_unrecorded_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runtime = root / 'runtime'
            runtime.mkdir()
            source = runtime / 'engine.zig'
            source.write_text('original')
            os_rows = []
            for name in ci.OS_BUILD_SOURCES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'fixture source: {name}\n')
                os_rows.append({'path': name, 'sha256': ci.sha(path)})
            binary = root / 'binary'
            binary.write_bytes(b'compiled fixture')
            receipt = root / 'receipt.json'
            ci.write_json(receipt, {
                'status': 'completed',
                'binary': {'sha256': ci.sha(binary)},
                'sources': {'files': [
                    {'path': 'runtime/engine.zig', 'sha256': ci.sha(source)},
                    *os_rows,
                ]},
            })
            previous = ci.CORE
            ci.CORE = root
            try:
                self.assertEqual(ci.bind_binary(binary, receipt)['binary_sha256'], ci.sha(binary))
                added = runtime / 'new.zig'
                added.write_text('new input')
                with self.assertRaises(ValueError):
                    ci.bind_binary(binary, receipt)
                added.unlink()
                os_source = root / next(iter(ci.OS_BUILD_SOURCES))
                os_source.write_text('changed OS input')
                with self.assertRaises(ValueError):
                    ci.bind_binary(binary, receipt)
                source.write_text('changed')
                with self.assertRaises(ValueError):
                    ci.bind_binary(binary, receipt)
            finally:
                ci.CORE = previous


class ProcessTests(unittest.TestCase):
    def test_real_unit_failure_skip_and_empty_suite_are_not_pass(self):
        sources = {
            'failing': 'import unittest\nclass Test(unittest.TestCase):\n def test_behavior(self): self.assertEqual(1, 2)\n',
            'skipped': 'import unittest\nclass Test(unittest.TestCase):\n @unittest.skip("unavailable platform")\n def test_behavior(self): self.fail()\n',
            'empty': 'import unittest\n',
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, source in sources.items():
                (root / (name + '.py')).write_text(source)
                row = {'id': 'unit/' + name, 'kind': 'unit', 'root': temporary, 'name': name, 'modules': ['harness']}
                result = ci.execute(row, root, None, 10)
                self.assertEqual(result['status'], 'INCOMPLETE' if name == 'skipped' else 'FAIL' if name == 'failing' else 'ERROR')

    def test_keep_going_executes_repairs_without_waiving_source_failure(self):
        for keep_going in (False, True):
            with self.subTest(keep_going=keep_going), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "mioa-core/mioa_core"
                source.mkdir(parents=True)
                (source / "leak.py").write_text("import os\n")
                native = root / "runtime/engine/src"
                native.mkdir(parents=True)
                (native / "pure.zig").write_text("const value = 1;\n")
                marker = root / "repair-ran"
                (root / "repair_probe.py").write_text(
                    "import unittest\nfrom pathlib import Path\n"
                    "class Repair(unittest.TestCase):\n"
                    f" def test_repair(self): Path({str(marker)!r}).write_text('observed')\n")
                rows = [
                    {"id": "source/leak-direct-os-api", "kind": "source", "name": "leak-direct-os-api", "modules": ["harness"]},
                    {"id": "unit/repair_probe", "kind": "unit", "name": "repair_probe", "root": str(root), "modules": ["harness"]},
                ]
                output = root / ".local/evidence"
                args = ["--module", "harness", "--output", str(output), "--format", "json"]
                with (patch.object(ci, "CORE", root), patch.object(ci, "plan", return_value=rows),
                      patch.object(ci, "inventory", side_effect=lambda: {"leak.py": ci.sha(source / "leak.py")}),
                      redirect_stdout(StringIO())):
                    code = ci.main(args + (["--keep-going"] if keep_going else []))
                report = json.loads((output / "report.json").read_text())
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "FAIL")
                self.assertEqual(report["results"][0]["status"], "FAIL")
                self.assertEqual(report["results"][1]["status"], "PASS" if keep_going else "NOT_RUN")
                self.assertEqual(marker.exists(), keep_going)
                self.assertEqual(report["next_action"]["action"], "review-and-fix-os-boundary")

    def test_preflight_failures_retain_structured_repair_evidence(self):
        cases = (["--level", "3"], ["--level", "2", "--module", "harness"])
        for args in cases:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "evidence"
                if args[1] == "2":
                    args = args + ["--binary", str(Path(temporary) / "absent")]
                summary = StringIO()
                with redirect_stdout(summary):
                    code = ci.main(args + ["--output", str(output), "--format", "json"])
                report = json.loads((output / "report.json").read_text())
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "ERROR")
                self.assertTrue(all(row["status"] == "NOT_RUN" for row in report["results"]))
                self.assertEqual(report["next_action"]["action"], "repair-preflight-or-evidence")
                self.assertEqual(json.loads(summary.getvalue())["status"], "ERROR")
                self.assertTrue((output / "report.toon").is_file())

    def test_real_process_timeout_and_missing_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = ci.launch([sys.executable, '-c', 'import time; time.sleep(30)'], root / 'timeout.log', 0.05)
            self.assertEqual(result['status'], 'TIMEOUT')
            result = ci.launch([str(root / 'absent')], root / 'missing.log', 1)
            self.assertEqual(result['status'], 'ERROR')

    def test_unreported_web_execution_is_not_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = {'id': 'web/input-focus', 'name': 'input-focus', 'kind': 'web', 'modules': ['input']}
            result = ci.execute(row, root, root / 'missing-browser', 10)
            self.assertEqual(result['status'], 'ERROR')


if __name__ == '__main__':
    unittest.main()
