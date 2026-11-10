"""Run production streaming Base64 regressions without engine dependencies."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'runtime/engine/src/Base64Writer.zig'


class Base64WriterTests(unittest.TestCase):
    def test_native_streaming_encoding(self):
        zig = shutil.which('zig')
        self.assertIsNotNone(zig, 'Streaming Base64 regressions require the installed Zig compiler')
        with tempfile.TemporaryDirectory(prefix='mioa-base64-writer-') as directory:
            root = Path(directory)
            source = root / SOURCE.name
            source.symlink_to(SOURCE)
            result = subprocess.run(
                [zig, 'test', str(source), '--test-filter', 'Base64Writer:',
                 '--cache-dir', str(root / 'cache'), '-femit-bin=' + str(root / 'writer-tests')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stderr.strip().splitlines()[-1])


if __name__ == '__main__':
    unittest.main()
