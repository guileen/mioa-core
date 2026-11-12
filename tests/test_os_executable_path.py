"""Current executable path snapshots through the public OS capability."""
import ctypes as c
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OK, INVALID, UNSUPPORTED, NO_MEMORY, IO = range(5)


class ExecutablePathValue(c.Structure):
    _fields_ = [('path', c.c_void_p), ('length', c.c_size_t)]


class ExecutablePathCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
        library = Path(cls.build.name) / ('executable_path' + suffix)
        command = shlex.split(os.environ.get('CC', 'cc')) + [
            '-std=c11', '-Wall', '-Wextra', '-Werror', '-fPIC',
            '-dynamiclib' if sys.platform == 'darwin' else '-shared',
            '-I', str(ROOT / 'mioa-os/api/system'),
            str(ROOT / 'mioa-os/executable_path.c'), '-o', str(library)]
        if sys.platform == 'win32':
            command.append('-lkernel32')
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        cls.lib = c.CDLL(str(library))
        cls.lib.mioa_executable_path_get_v1.argtypes = [c.POINTER(ExecutablePathValue)]
        cls.lib.mioa_executable_path_get_v1.restype = c.c_int
        cls.lib.mioa_executable_path_value_release_v1.argtypes = [c.POINTER(ExecutablePathValue)]
        cls.lib.mioa_executable_path_value_release_v1.restype = None

    def test_path_names_the_running_openable_executable(self):
        value = ExecutablePathValue(None, 0)
        self.assertEqual(self.lib.mioa_executable_path_get_v1(c.byref(value)), OK)
        try:
            path = c.string_at(value.path, value.length).decode('utf-8')
            self.assertGreater(value.length, 0)
            self.assertTrue(os.path.isabs(path), path)
            self.assertTrue(os.path.samefile(path, sys.executable), (path, sys.executable))
        finally:
            self.lib.mioa_executable_path_value_release_v1(c.byref(value))
        self.assertFalse(value.path)
        self.assertEqual(value.length, 0)

    def test_release_is_idempotent(self):
        value = ExecutablePathValue(None, 0)
        self.assertEqual(self.lib.mioa_executable_path_get_v1(c.byref(value)), OK)
        pointer = c.byref(value)
        self.lib.mioa_executable_path_value_release_v1(pointer)
        self.lib.mioa_executable_path_value_release_v1(pointer)
        self.assertFalse(value.path)
        self.assertEqual(value.length, 0)

    def test_invalid_output_is_rejected_unchanged(self):
        value = ExecutablePathValue(1, 7)
        self.assertEqual(self.lib.mioa_executable_path_get_v1(c.byref(value)), INVALID)
        self.assertEqual(value.path, 1)
        self.assertEqual(value.length, 7)
        self.assertEqual(self.lib.mioa_executable_path_get_v1(None), INVALID)


if __name__ == '__main__':
    unittest.main()
