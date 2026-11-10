"""Exercise production String ownership with the native checking allocator."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'runtime/engine/src/string.zig'


class StringOwnershipTests(unittest.TestCase):
    def test_native_string_ownership(self):
        zig = shutil.which('zig')
        self.assertIsNotNone(zig, 'String ownership regressions require the installed Zig compiler')
        with tempfile.TemporaryDirectory(prefix='mioa-string-ownership-') as directory:
            root = Path(directory)
            for dependency in SOURCE.parent.iterdir():
                (root / dependency.name).symlink_to(dependency, target_is_directory=dependency.is_dir())
            source = root / SOURCE.name
            result = subprocess.run(
                [zig, 'test', str(source), '--test-filter', 'String: ownership',
                 '--cache-dir', str(root / 'cache'), '-femit-bin=' + str(root / 'string-tests')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stderr.strip().splitlines()[-1])


if __name__ == '__main__':
    unittest.main()
