"""Run production CORS entry ownership regressions with allocation failures."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'runtime/engine/src'


class CorsEntryTests(unittest.TestCase):
    def test_native_merge_ownership(self):
        zig = shutil.which('zig')
        self.assertIsNotNone(zig, 'CORS entry regressions require the installed Zig compiler')
        with tempfile.TemporaryDirectory(prefix='mioa-cors-entry-') as directory:
            root = Path(directory)
            for source in SOURCE.iterdir():
                (root / source.name).symlink_to(source, target_is_directory=source.is_dir())
            runner = root / 'cors_entry_tests.zig'
            runner.write_text('test { _ = @import("network/CorsStore.zig"); }\n')
            result = subprocess.run(
                [zig, 'test', str(runner), '--test-filter', 'CorsStore.Entry:',
                 '--cache-dir', str(root / 'cache'), '-femit-bin=' + str(root / 'cors-entry-tests')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stderr.strip().splitlines()[-1])


if __name__ == '__main__':
    unittest.main()
