"""Host-owned synchronous process capability regressions."""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProcessCapabilityTests(unittest.TestCase):
    def test_zig_provider_contract(self):
        command = shlex.split(os.environ.get('ZIG', 'zig')) + [
            'test', '--dep', 'lightpanda',
            f'-Mroot={ROOT / "mioa-os/process.zig"}',
            f'-Mlightpanda={ROOT / "tests/process_api_lightpanda.zig"}',
            '-lc']
        environment = os.environ.copy()
        environment['MIOA_PROCESS_API_TEST'] = 'inherited'
        result = subprocess.run(command, cwd=ROOT, env=environment,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr)[-3000:])
        self.assertIn('MIOA_PROCESS_CLEAN_EXIT_FLUSHED', result.stdout)


if __name__ == '__main__':
    unittest.main()
