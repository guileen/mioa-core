"""App-data directory policy and creation through the public OS capability."""
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
OK, INVALID, UNAVAILABLE, NO_MEMORY, IO = range(5)
CREATE_DIRECTORY = 1


class AppDataPath(c.Structure):
    _fields_ = [('path', c.c_void_p), ('length', c.c_size_t)]


class AppDataCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        library = Path(cls.build.name) / ('app_data.dylib' if sys.platform == 'darwin' else 'app_data.so')
        command = shlex.split(os.environ.get('CC', 'cc')) + [
            '-std=c11', '-Wall', '-Wextra', '-Werror', '-fPIC',
            '-dynamiclib' if sys.platform == 'darwin' else '-shared',
            '-I', str(ROOT / 'mioa-os/api/system'),
            str(ROOT / 'mioa-os/app_data.c'), '-o', str(library)]
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        cls.lib = c.CDLL(str(library))
        cls.lib.mioa_app_data_directory_v1.argtypes = [c.c_char_p, c.c_uint32, c.POINTER(AppDataPath)]
        cls.lib.mioa_app_data_directory_v1.restype = c.c_int
        cls.lib.mioa_app_data_path_release_v1.argtypes = [c.POINTER(AppDataPath)]
        cls.lib.mioa_app_data_path_release_v1.restype = None

    def directory(self, name, options=0):
        out = AppDataPath(None, 0)
        status = self.lib.mioa_app_data_directory_v1(name.encode(), options, c.byref(out))
        path = c.string_at(out.path, out.length).decode() if out.path else None
        return status, out, path

    def test_host_path_policy_uses_native_app_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, xdg, local = root / 'home', root / 'xdg', root / 'local'
            home.mkdir()
            xdg.mkdir()
            local.mkdir()
            with patch.dict(os.environ, {
                'HOME': str(home), 'XDG_DATA_HOME': str(xdg), 'LOCALAPPDATA': str(local),
            }, clear=True):
                status, out, path = self.directory('mioa-test')
                self.assertEqual(status, OK)
                try:
                    if sys.platform == 'darwin':
                        expected = home / 'Library' / 'Application Support' / 'mioa-test'
                    elif sys.platform.startswith('win'):
                        expected = local / 'mioa-test'
                    else:
                        expected = xdg / 'mioa-test'
                    self.assertEqual(Path(path), expected)
                    self.assertFalse(expected.exists())
                finally:
                    self.lib.mioa_app_data_path_release_v1(c.byref(out))

    def test_invalid_xdg_falls_back_to_platform_data_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, local = root / 'home', root / 'local'
            home.mkdir()
            local.mkdir()
            with patch.dict(os.environ, {
                'HOME': str(home), 'XDG_DATA_HOME': 'relative/path', 'LOCALAPPDATA': str(local),
            }, clear=True):
                status, out, path = self.directory('mioa-test')
                self.assertEqual(status, OK)
                try:
                    if sys.platform == 'darwin':
                        expected = home / 'Library' / 'Application Support' / 'mioa-test'
                    elif sys.platform.startswith('win'):
                        expected = local / 'mioa-test'
                    else:
                        expected = home / '.local' / 'share' / 'mioa-test'
                    self.assertEqual(Path(path), expected)
                finally:
                    self.lib.mioa_app_data_path_release_v1(c.byref(out))


    def test_create_option_makes_directory_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            values = {'LOCALAPPDATA': str(base), 'HOME': str(base), 'XDG_DATA_HOME': str(base / 'data')}
            with patch.dict(os.environ, values, clear=True):
                status, out, path = self.directory('mioa-test', CREATE_DIRECTORY)
                self.assertEqual(status, OK)
                try:
                    created = Path(path)
                    self.assertTrue(created.is_dir())
                    if os.name == 'posix':
                        self.assertEqual(created.stat().st_mode & 0o077, 0)
                finally:
                    self.lib.mioa_app_data_path_release_v1(c.byref(out))
                status, out, again = self.directory('mioa-test', CREATE_DIRECTORY)
                self.assertEqual(status, OK)
                try:
                    self.assertEqual(again, path)
                finally:
                    self.lib.mioa_app_data_path_release_v1(c.byref(out))

    def test_missing_native_location_and_invalid_inputs_fail_without_output(self):
        with patch.dict(os.environ, {}, clear=True):
            status, out, path = self.directory('mioa-test')
            self.assertEqual(status, UNAVAILABLE)
            self.assertIsNone(path)
            self.assertEqual(out.length, 0)
        for name, options in (('', 0), ('.', 0), ('..', 0), ('a/b', 0), ('a\\b', 0), ('ok', 2)):
            with self.subTest(name=name, options=options), patch.dict(os.environ, {}, clear=True):
                status, out, path = self.directory(name, options)
                self.assertEqual(status, INVALID)
                self.assertIsNone(path)
                self.assertEqual(out.length, 0)

    def test_creation_failure_leaves_result_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / 'not-a-directory'
            blocker.write_text('file')
            if sys.platform.startswith('win'):
                values = {'LOCALAPPDATA': str(blocker)}
            elif sys.platform == 'darwin':
                values = {'HOME': str(blocker)}
            else:
                values = {'XDG_DATA_HOME': str(blocker)}
            with patch.dict(os.environ, values, clear=True):
                out = AppDataPath(None, 0)
                status = self.lib.mioa_app_data_directory_v1(b'mioa-test', CREATE_DIRECTORY, c.byref(out))
                self.assertEqual(status, IO)
                self.assertIsNone(out.path)
                self.assertEqual(out.length, 0)

    def test_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'HOME': temporary, 'LOCALAPPDATA': temporary}, clear=True):
                status, out, _ = self.directory('mioa-test')
                self.assertEqual(status, OK)
                pointer = c.byref(out)
                self.lib.mioa_app_data_path_release_v1(pointer)
                self.lib.mioa_app_data_path_release_v1(pointer)
                self.assertIsNone(out.path)
                self.assertEqual(out.length, 0)


if __name__ == '__main__':
    unittest.main()
