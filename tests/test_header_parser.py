"""Run the engine's HTTP/MIME parser regressions without V8 or host providers."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'runtime/engine/src'


class HeaderParserTests(unittest.TestCase):
    def test_native_parser_contracts(self):
        zig = shutil.which('zig')
        self.assertIsNotNone(zig, 'HTTP parser regressions require the installed Zig compiler')
        with tempfile.TemporaryDirectory(prefix='mioa-header-parser-') as directory:
            root = Path(directory)
            # Keep imports rooted at src; compile unchanged production and test helpers.
            for source in SOURCE.iterdir():
                (root / source.name).symlink_to(source, target_is_directory=source.is_dir())
            runner = root / 'header_parser_tests.zig'
            runner.write_text('test { _ = @import("network/header_parser.zig"); }\n')
            result = subprocess.run(
                [zig, 'test', str(runner), '--test-filter', 'header_parser:',
                 '--cache-dir', str(root / 'cache'), '-femit-bin=' + str(root / 'parser-tests')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stderr.strip().splitlines()[-1])


if __name__ == '__main__':
    unittest.main()
