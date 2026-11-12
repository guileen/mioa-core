"""Consumer-visible source-boundary failures; no Core/native runtime launch."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import ci
import leak_direct_os_api as guard


class SourceBoundaryTests(unittest.TestCase):
    def test_python_aliases_dynamic_imports_and_file_builtins_are_rejected(self):
        source = ('import os as host\nfrom pathlib import Path as P\n'
                  'from builtins import open as file_open\n'
                  'file_open("x")\n__import__("socket")\n'
                  'import importlib as loader\nloader.import_module(name)\n')
        rows = guard.python_findings(source)
        self.assertIn((1, 'python-import', 'os'), rows)
        self.assertIn((2, 'python-import', 'pathlib.Path'), rows)
        self.assertIn((4, 'python-os-api', 'builtins.open'), rows)
        self.assertIn((5, 'python-import', 'socket'), rows)
        self.assertIn((7, 'dynamic-import', 'nonliteral module'), rows)

    def test_core_cannot_import_colocated_os_implementation(self):
        self.assertIn((1, 'python-import', 'mioa_os.files'),
                      guard.python_findings('import mioa_os.files\n'))
        self.assertIn((1, 'native-import', '../../mioa-os/files.h'),
                      guard.native_findings('#include "../../mioa-os/files.h"\n', '.c'))
        self.assertEqual(guard.native_findings(
            '#include "graphics_file_v1.h"\n', '.c'), set())

    def test_pure_computation_public_api_and_documentation_are_allowed(self):
        self.assertEqual(guard.python_findings(
            'import json\nfrom urllib.parse import urlsplit\nimport graphics_api as api\n'
            'note = "import os; open(\\\"x\\\")"\n'
            'result = api.storage.read(api.capability)\n'), set())
        native = '// std.posix.open("x");\nconst note = "IOSurfaceLock std.fs";\n'
        native += 'const api = @import("graphics_api");\n'
        native += 'const val = std.c.getenv("NAME");\n'
        native += 'const env = std.c.environ;\n'
        self.assertEqual(guard.native_findings(native, '.zig'), set())
        self.assertEqual(guard.native_findings('#include "graphics_render_v1.h"\n', '.c'), set())
    def test_platform_references_are_found_even_in_conditional_branches(self):
        cases = [
            ('.zig', 'const host = @import("std");\nhost.posix.read(fd, b);', 'native-os-api'),
            ('.zig', '@import("std").posix.read(fd, b);', 'native-os-api'),
            ('.zig', 'const p = @import("graphics_renderer");', 'native-import'),
            ('.c', '#ifdef __APPLE__\n#include <CoreGraphics/CoreGraphics.h>\n#endif', 'native-import'),
            ('.c', '#include <sys/socket.h>\n', 'native-import'),
            ('.c', '#include "../../mioa-graphics/resource.h"\n', 'native-import'),
            ('.c', 'void f(void) { mmap(0, n, 0, 0, -1, 0); }', 'native-os-api'),
            ('.rs', 'use std::{fs as files, net};', 'native-os-api'),
        ]
        for suffix, source, rule in cases:
            with self.subTest(source=source):
                self.assertTrue(any(row[1] == rule for row in guard.native_findings(source, suffix)))

    def fixture(self, root):
        for relative in guard.ROOTS:
            (root / relative).mkdir(parents=True)
        (root / guard.ROOTS[0] / 'pure.py').write_text('value = 1\n')
        (root / guard.ROOTS[1] / 'pure.zig').write_text('const value = 1;\n')

    def test_scan_inspects_sibling_modules_without_executing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            sentinel = root / 'must-not-exist'
            (root / guard.ROOTS[0] / 'helper.py').write_text(
                f'from pathlib import Path\nPath({str(sentinel)!r}).write_text("executed")\n')
            report = guard.scan(root)
            self.assertEqual(report['status'], 'FAIL')
            self.assertEqual(report['findings'][0]['path'], 'mioa-core/mioa_core/helper.py')
            self.assertFalse(sentinel.exists())
            self.assertEqual(len(report['sources']), report['files_checked'])

    def test_missing_empty_malformed_and_symlinked_sources_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(guard.scan(root)['status'], 'ERROR')
            self.fixture(root)
            bad = root / guard.ROOTS[0] / 'bad.py'
            bad.write_text('def invalid(\n')
            self.assertEqual(guard.scan(root)['status'], 'ERROR')
            bad.unlink()
            (root / guard.ROOTS[0] / 'alias.py').symlink_to(root / guard.ROOTS[1] / 'pure.zig')
            self.assertEqual(guard.scan(root)['status'], 'ERROR')
            (root / guard.ROOTS[0] / 'alias.py').unlink()
            (root / guard.ROOTS[0] / 'pure.py').unlink()
            self.assertEqual(guard.scan(root)['status'], 'ERROR')

    def test_cli_and_ci_reject_a_real_leak_and_retained_report_is_inspectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / guard.ROOTS[0] / 'pure.py').write_text('import socket\n')
            output = root / 'result.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(guard.main(['--root', str(root), '--report', str(output)]), 1)
            self.assertEqual(json.loads(output.read_text())['status'], 'FAIL')
            previous = ci.CORE
            ci.CORE = root
            try:
                row = {'id': 'source/leak-direct-os-api', 'kind': 'source', 'modules': ['harness']}
                result = ci.execute(row, root, None, 10)
            finally:
                ci.CORE = previous
            self.assertEqual(result['status'], 'FAIL')
            self.assertEqual(result['result']['findings'][0]['reference'], 'socket')
            self.assertEqual(ci.assess([result], [], 'deploy', [])[0], 'BLOCKED')

    def test_compatible_source_passes_without_a_native_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(guard.main(['--root', str(root)]), 0)


    def test_path_values_units_and_local_modules_are_not_host_capabilities(self):
        self.assertEqual(guard.python_findings(
            'from pathlib import PurePosixPath\nvalue = PurePosixPath("/guest") / "data"\n'), set())
        self.assertEqual(guard.native_findings(
            'const crypto = @import("sys/libcrypto.zig");\nconst scale = std.time.ns_per_s;\n', '.zig'), set())
        self.assertTrue(guard.native_findings('@cInclude("sys/socket.h");', '.zig'))
        self.assertTrue(guard.native_findings('std.time.nanoTimestamp();', '.zig'))


    def test_inventory_resolves_aliases_without_claiming_bare_c_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / guard.ROOTS[0] / 'pure.py').write_text(
                'import secrets as rng\nrng.token_bytes(4)\n')
            (root / guard.ROOTS[1] / 'pure.zig').write_text(
                'const host = @import("std");\nconst p = host.posix;\n'
                'p.read(fd, bytes);\nfn close() void {}\n')
            report = guard.scan(root)
            rows = report['inventory']['occurrences']
            self.assertTrue(any(r['reference'] == 'secrets.token_bytes' and
                                r['capability'] == 'random' and r['kind'] == 'call' for r in rows))
            self.assertTrue(any(r['reference'] == 'std.posix.read' and
                                r['confidence'] == 'candidate' for r in rows))
            self.assertTrue(any(r['reference'] == 'close' and
                                r['confidence'] == 'review' for r in rows))
            self.assertEqual(report['status'], 'FAIL')

    def test_inventory_does_not_promote_comments_literals_or_pure_computation(self):
        source = ('// IOSurfaceLock(surface);\nconst s = "mach_msg(x)";\n'
                  'std.fmt.allocPrint(a, s, args);\nstd.mem.replace(u8, a, b, c);\n')
        rows = guard.collect(source, '.zig', guard.native_findings(source, '.zig'),
                                       guard.LEX, guard.dotted)
        self.assertEqual(rows, [])

    def test_filtered_console_preserves_complete_json_and_gate_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / guard.ROOTS[0] / 'pure.py').write_text(
                'import secrets\nimport socket\nsecrets.token_bytes(4)\nsocket.socket()\n')
            output = root / 'report.json'
            console = io.StringIO()
            with contextlib.redirect_stdout(console):
                result = guard.main(['--root', str(root), '--report', str(output),
                                     '--capability', 'random', '--details'])
            self.assertEqual(result, 1)
            self.assertIn('[ ] random', console.getvalue())
            self.assertNotIn('[ ] network', console.getvalue())
            report = json.loads(output.read_text())
            self.assertIn('network', {r['capability'] for r in report['inventory']['groups']})
            self.assertEqual(report['status'], 'FAIL')

if __name__ == '__main__':
    unittest.main()
