"""Owned environment snapshots through the public OS capability."""
import ctypes as c
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OK, MISSING, INVALID, NO_MEMORY = range(4)


class EnvironmentValue(c.Structure):
    _fields_ = [('value', c.c_void_p), ('length', c.c_size_t)]


class EnvironmentCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        library = Path(cls.build.name) / ('environment.dylib' if sys.platform == 'darwin' else 'environment.so')
        command = shlex.split(os.environ.get('CC', 'cc')) + [
            '-std=c11', '-Wall', '-Wextra', '-Werror', '-fPIC',
            '-dynamiclib' if sys.platform == 'darwin' else '-shared',
            '-I', str(ROOT / 'mioa-os/api/system'),
            str(ROOT / 'mioa-os/environment.c'), '-o', str(library)]
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        cls.lib = c.CDLL(str(library))
        cls.lib.mioa_environment_get_v1.argtypes = [c.c_char_p, c.POINTER(EnvironmentValue)]
        cls.lib.mioa_environment_get_v1.restype = c.c_int
        cls.lib.mioa_environment_value_release_v1.argtypes = [c.POINTER(EnvironmentValue)]
        cls.lib.mioa_environment_has_v1.argtypes = [c.c_char_p, c.POINTER(c.c_int)]
        cls.lib.mioa_environment_has_v1.restype = c.c_int
        cls.lib.mioa_environment_value_release_v1.restype = None

    def get(self, name):
        out = EnvironmentValue(None, 0)
        status = self.lib.mioa_environment_get_v1(name, c.byref(out))
        value = c.string_at(out.value, out.length).decode() if out.value else None
        return status, out, value

    def test_present_value_is_an_owned_snapshot(self):
        with patch.dict(os.environ, {'MIOA_TEST_ENV_VALUE': 'before'}):
            status, out, value = self.get(b'MIOA_TEST_ENV_VALUE')
            self.assertEqual(status, OK)
            self.assertEqual(value, 'before')
            self.assertEqual(c.string_at(out.value + out.length, 1), b'\0')
            try:
                os.environ['MIOA_TEST_ENV_VALUE'] = 'after'
                self.assertEqual(c.string_at(out.value, out.length).decode(), 'before')
            finally:
                self.lib.mioa_environment_value_release_v1(c.byref(out))
            self.assertIsNone(out.value)
            self.assertEqual(out.length, 0)

    def test_empty_value_is_present_and_release_is_idempotent(self):
        with patch.dict(os.environ, {'MIOA_TEST_ENV_EMPTY': ''}):
            status, out, value = self.get(b'MIOA_TEST_ENV_EMPTY')
            self.assertEqual(status, OK)
            self.assertEqual(value, '')
            self.assertIsNotNone(out.value)
            self.assertEqual(c.string_at(out.value, 1), b'\0')
            pointer = c.byref(out)
            self.lib.mioa_environment_value_release_v1(pointer)
            self.lib.mioa_environment_value_release_v1(pointer)
            self.assertIsNone(out.value)

    def test_missing_and_invalid_names_leave_output_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            status, out, value = self.get(b'MIOA_TEST_ENV_ABSENT')
            self.assertEqual(status, MISSING)
            self.assertIsNone(value)
            self.assertIsNone(out.value)
            self.assertEqual(out.length, 0)
        for name in (None, b'', b'1INVALID', b'BAD-NAME', b'BAD=NAME'):
            with self.subTest(name=name):
                out = EnvironmentValue(None, 0)
                status = self.lib.mioa_environment_get_v1(name, c.byref(out))
                self.assertEqual(status, INVALID)
                self.assertIsNone(out.value)
                self.assertEqual(out.length, 0)


    def test_presence_query_preserves_empty_and_reports_missing(self):
        for name, value in (('MIOA_TEST_ENV_PRESENT', 'value'), ('MIOA_TEST_ENV_EMPTY_PRESENT', '')):
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                present = c.c_int(-1)
                status = self.lib.mioa_environment_has_v1(name.encode(), c.byref(present))
                self.assertEqual(status, OK)
                self.assertEqual(present.value, 1)
        with patch.dict(os.environ, {}, clear=True):
            present = c.c_int(-1)
            status = self.lib.mioa_environment_has_v1(b'MIOA_TEST_ENV_ABSENT', c.byref(present))
            self.assertEqual(status, OK)
            self.assertEqual(present.value, 0)
        for name in (None, b'', b'1INVALID', b'BAD-NAME', b'BAD=NAME'):
            with self.subTest(name=name):
                present = c.c_int(7)
                status = self.lib.mioa_environment_has_v1(name, c.byref(present))
                self.assertEqual(status, INVALID)
                self.assertEqual(present.value, 7)

if __name__ == '__main__':
    unittest.main()
