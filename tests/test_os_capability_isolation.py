"""Verify fine-grained compile-time and link-time dependency on individual mioa-os capabilities.

Contract:
Core requires mioa-os. Even when mioa-os is present as a whole, removing or
omitting any individual capability (e.g. environment, terminal, files) MUST
cause a deterministic compile/link failure specifically naming the missing
capability's exported symbols, proving no fallback or leak to host OS.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path


CORE_ROOT = Path(__file__).resolve().parent.parent


class CapabilityIsolationTests(unittest.TestCase):
    def test_full_mioa_os_environment_builds_and_passes(self):
        """Full mioa-os present: environment test compiles and passes."""
        cmd = [
            "zig", "test",
            str(CORE_ROOT / "runtime/engine/src/sys/environment.zig"),
            str(CORE_ROOT / "mioa-os/environment.c"),
            "-lc",
        ]
        proc = subprocess.run(cmd, cwd=CORE_ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Expected clean pass, got: {proc.stderr}")
        self.assertIn("All 1 tests passed", proc.stderr + proc.stdout)

    def test_omit_environment_capability_fails_link_with_specific_symbol(self):
        """mioa-os present, but environment.c omitted: link must fail naming _mioa_environment_has_v1."""
        cmd = [
            "zig", "test",
            str(CORE_ROOT / "runtime/engine/src/sys/environment.zig"),
            "-lc",
        ]
        proc = subprocess.run(cmd, cwd=CORE_ROOT, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, "Build must fail when environment capability is missing")
        combined = proc.stderr + proc.stdout
        self.assertIn("_mioa_environment_has_v1", combined)
        self.assertIn("undefined symbol", combined.lower())

    def test_omit_terminal_capability_fails_link_with_specific_symbol(self):
        """Core component requiring terminal: omitting terminal.c must fail naming _mioa_terminal_stderr_columns_v1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test_terminal_need.zig"
            test_file.write_text(
                'extern "c" fn mioa_terminal_stderr_columns_v1() u32;\n'
                'test "requires terminal capability" {\n'
                '    _ = mioa_terminal_stderr_columns_v1();\n'
                '}\n'
            )
            # 1. With terminal.c: passes
            cmd_pass = [
                "zig", "test", str(test_file),
                str(CORE_ROOT / "mioa-os/terminal.c"),
                "-lc",
            ]
            res_pass = subprocess.run(cmd_pass, cwd=CORE_ROOT, capture_output=True, text=True)
            self.assertEqual(res_pass.returncode, 0, f"Expected pass with terminal.c: {res_pass.stderr}")

            # 2. Without terminal.c (omitted capability): fails specifically
            cmd_fail = ["zig", "test", str(test_file), "-lc"]
            res_fail = subprocess.run(cmd_fail, cwd=CORE_ROOT, capture_output=True, text=True)
            self.assertNotEqual(res_fail.returncode, 0, "Build must fail without terminal capability")
            combined = res_fail.stderr + res_fail.stdout
            self.assertIn("_mioa_terminal_stderr_columns_v1", combined)
            self.assertIn("undefined symbol", combined.lower())

    def test_omit_raw_frame_files_capability_fails_link_with_specific_symbol(self):
        """Core component requiring files: omitting files.c must fail naming _mg_os_raw_frame_write."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test_files_need.zig"
            test_file.write_text(
                'extern "c" fn mg_os_raw_frame_write([*c]const u8, [*c]const u8, usize) c_int;\n'
                'test "requires files capability" {\n'
                '    _ = mg_os_raw_frame_write("test", "data", 4);\n'
                '}\n'
            )
            # 1. With files.c: passes
            cmd_pass = [
                "zig", "test", str(test_file),
                str(CORE_ROOT / "mioa-os/files.c"),
                "-lc",
            ]
            res_pass = subprocess.run(cmd_pass, cwd=CORE_ROOT, capture_output=True, text=True)
            self.assertEqual(res_pass.returncode, 0, f"Expected pass with files.c: {res_pass.stderr}")

            # 2. Without files.c (omitted capability): fails specifically
            cmd_fail = ["zig", "test", str(test_file), "-lc"]
            res_fail = subprocess.run(cmd_fail, cwd=CORE_ROOT, capture_output=True, text=True)
            self.assertNotEqual(res_fail.returncode, 0, "Build must fail without files capability")
            combined = res_fail.stderr + res_fail.stdout
            self.assertIn("_mg_os_raw_frame_write", combined)
            self.assertIn("undefined symbol", combined.lower())


if __name__ == "__main__":
    unittest.main()
