"""Host-owned joinable thread capability regressions."""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ThreadCapabilityTests(unittest.TestCase):
    def test_zig_provider_contract(self):
        command = shlex.split(os.environ.get('ZIG', 'zig')) + [
            'test', str(ROOT / 'mioa-os/thread.zig'), '-lc']
        result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr)[-3000:])


if __name__ == '__main__':
    unittest.main()
