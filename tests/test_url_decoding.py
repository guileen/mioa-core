"""Run production URL and data URL decoding regressions without URL FFI or V8."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'runtime/engine/src'


class UrlDecodingTests(unittest.TestCase):
    def _run_native(self, module, test_filter):
        zig = shutil.which('zig')
        self.assertIsNotNone(zig, 'URL decoding regressions require the installed Zig compiler')
        with tempfile.TemporaryDirectory(prefix='mioa-url-decoding-') as directory:
            root = Path(directory)
            for source in SOURCE.iterdir():
                (root / source.name).symlink_to(source, target_is_directory=source.is_dir())
            runner = root / 'url_decoding_tests.zig'
            runner.write_text(f'test {{ _ = @import("{module}"); }}\n')
            result = subprocess.run(
                [zig, 'test', str(runner), '--test-filter', test_filter,
                 '--cache-dir', str(root / 'cache'), '-femit-bin=' + str(root / 'decoder-tests')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stderr.strip().splitlines()[-1])

    def test_native_percent_decoding(self):
        self._run_native('browser/URL.zig', 'URL: unescape')

    def test_native_data_url_decoding(self):
        self._run_native('browser/data_url.zig', 'data_url:')

    def test_native_shared_base64_decoding(self):
        self._run_native('browser/webapi/encoding/base64.zig', 'base64:')


if __name__ == '__main__':
    unittest.main()
