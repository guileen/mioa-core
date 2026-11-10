"""Run production URL blocklist ownership and matching regressions without V8."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'runtime/engine/src'


class UrlBlocklistTests(unittest.TestCase):
    def test_native_blocklist_contracts(self):
        zig = shutil.which('zig')
        self.assertIsNotNone(zig, 'URL blocklist regressions require the installed Zig compiler')
        with tempfile.TemporaryDirectory(prefix='mioa-url-blocklist-') as directory:
            root = Path(directory)
            for source in SOURCE.iterdir():
                (root / source.name).symlink_to(source, target_is_directory=source.is_dir())
            runner = root / 'url_blocklist_tests.zig'
            runner.write_text('test { _ = @import("network/UrlBlocklist.zig"); }\n')
            result = subprocess.run(
                [zig, 'test', str(runner), '--test-filter', 'UrlBlocklist:',
                 '--cache-dir', str(root / 'cache'), '-femit-bin=' + str(root / 'blocklist-tests')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stderr.strip().splitlines()[-1])


if __name__ == '__main__':
    unittest.main()
