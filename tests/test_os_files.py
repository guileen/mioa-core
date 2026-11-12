"""Real file-v1 provider I/O; no engine build, sandbox or Core cutover claim."""
import ctypes as c
import os
import fcntl
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OK, INVALID, UNSUPPORTED, DENIED, STALE, LIMIT = range(6)
READ, WRITE, ISOLATED = 1, 2, 4


class Handle(c.Structure):
    _fields_ = [('connection', c.c_uint64), ('object', c.c_uint64)]


class FileCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        library = Path(cls.build.name) / 'files.so'
        command = shlex.split(os.environ.get('CC', 'cc')) + [
            '-std=c11', '-Wall', '-Wextra', '-Werror', '-fPIC',
            '-dynamiclib' if sys.platform == 'darwin' else '-shared',
            str(ROOT / 'mioa-os/files.c'), '-o', str(library)]
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        cls.lib = c.CDLL(str(library))
        signatures = {
            'mg_os_files_create': [c.c_uint32, c.c_uint64, c.c_uint64,
                                   c.POINTER(c.c_void_p), c.POINTER(c.c_void_p)],
            'mg_file_negotiate_v1': [c.c_void_p, c.c_uint32, c.c_uint64, c.POINTER(c.c_uint64)],
            'mg_os_files_grant': [c.c_void_p, c.c_int, c.c_uint32, c.c_uint64,
                                  c.c_uint64, c.POINTER(Handle)],
            'mg_file_read_v1': [c.c_void_p, Handle, c.c_uint64, c.c_void_p,
                                c.c_uint64, c.POINTER(c.c_uint64)],
            'mg_file_write_v1': [c.c_void_p, Handle, c.c_uint64, c.c_void_p,
                                 c.c_uint64, c.POINTER(c.c_uint64)],
            'mg_file_close_v1': [c.c_void_p, Handle],
            'mg_os_files_revoke': [c.c_void_p, Handle],
            'mg_os_files_destroy': [c.c_void_p],
        }
        for name, args in signatures.items():
            function = getattr(cls.lib, name)
            function.argtypes, function.restype = args, c.c_int

    def setUp(self):
        self.owner, self.endpoint = c.c_void_p(), c.c_void_p()
        self.assertEqual(self.lib.mg_os_files_create(1, 17, 0,
                         c.byref(self.owner), c.byref(self.endpoint)), OK)
        self.addCleanup(self.destroy)
        selected = c.c_uint64()
        self.assertEqual(self.lib.mg_file_negotiate_v1(self.endpoint, 1, 3,
                         c.byref(selected)), OK)
        self.file = tempfile.TemporaryFile()
        self.addCleanup(self.file.close)
        self.file.write(b'0123456789')
        self.file.flush()

    def destroy(self):
        self.assertEqual(self.lib.mg_os_files_destroy(self.owner), OK)

    def grant(self, rights=READ | WRITE, base=2, length=5):
        handle = Handle()
        self.assertEqual(self.lib.mg_os_files_grant(self.owner, self.file.fileno(),
                         rights, base, length, c.byref(handle)), OK)
        return handle

    def read(self, handle, offset, length):
        buffer, count = c.create_string_buffer(max(1, length)), c.c_uint64(99)
        status = self.lib.mg_file_read_v1(self.endpoint, handle, offset,
                                        buffer, length, c.byref(count))
        return status, count.value, buffer.raw[:min(count.value, length)]

    def write(self, handle, offset, data):
        count = c.c_uint64(99)
        status = self.lib.mg_file_write_v1(self.endpoint, handle, offset,
                                         data, len(data), c.byref(count))
        return status, count.value

    def test_positional_range_and_independent_host_oracle(self):
        handle = self.grant()
        os.lseek(self.file.fileno(), 7, os.SEEK_SET)
        self.assertEqual(self.read(handle, 1, 3), (OK, 3, b'345'))
        self.assertEqual(self.write(handle, 1, b'XY'), (OK, 2))
        self.assertEqual(os.lseek(self.file.fileno(), 0, os.SEEK_CUR), 7)
        self.assertEqual(os.pread(self.file.fileno(), 10, 0), b'012XY56789')

    def test_host_enabling_append_does_not_escape_granted_range(self):
        handle = self.grant()
        fd = self.file.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_APPEND)
        self.assertTrue(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND)
        self.assertEqual(self.write(handle, 1, b'XY'), (OK, 2))
        self.assertEqual(os.pread(fd, 20, 0), b'012XY56789')

    def test_range_and_transfer_limits_do_not_write(self):
        handle = self.grant()
        for offset, data in [(5, b'x'), (4, b'xx'), (2**64 - 1, b'x')]:
            with self.subTest(offset=offset, length=len(data)):
                self.assertEqual(self.write(handle, offset, data), (INVALID, 99))
        self.assertEqual(self.write(handle, 0, b'x' * (1048576 + 1)), (LIMIT, 99))
        self.assertEqual(os.pread(self.file.fileno(), 10, 0), b'0123456789')

    def test_short_eof_and_zero_length_boundary(self):
        handle = self.grant(base=8, length=5)
        self.assertEqual(self.read(handle, 0, 5), (OK, 2, b'89'))
        self.assertEqual(self.read(handle, 2, 3), (OK, 0, b''))
        self.assertEqual(self.write(handle, 5, b''), (OK, 0))
        self.assertEqual(self.read(handle, 6, 0)[:2], (INVALID, 99))

    def test_rights_revoke_close_and_slot_reuse(self):
        handle = self.grant(READ)
        self.assertEqual(self.write(handle, 0, b'x'), (DENIED, 99))
        self.assertEqual(self.lib.mg_os_files_revoke(self.owner, handle), OK)
        self.assertEqual(self.read(handle, 0, 1)[:2], (DENIED, 99))
        self.assertEqual(self.lib.mg_file_close_v1(self.endpoint, handle), OK)
        replacement = self.grant()
        self.assertNotEqual(replacement.object, handle.object)
        self.assertEqual(self.read(handle, 0, 1)[:2], (STALE, 99))
        self.assertEqual(self.lib.mg_file_close_v1(self.endpoint, handle), STALE)
        self.assertEqual(self.read(replacement, 0, 1), (OK, 1, b'2'))
        self.assertEqual(os.pread(self.file.fileno(), 10, 0), b'0123456789')

    def test_grant_owns_duplicate_not_host_descriptor(self):
        handle = self.grant()
        self.file.close()
        self.assertEqual(self.read(handle, 0, 5), (OK, 5, b'23456'))
        self.assertEqual(self.lib.mg_file_close_v1(self.endpoint, handle), OK)
        self.assertEqual(self.read(handle, 0, 1)[:2], (STALE, 99))

    def test_exhaustion_and_foreign_connection_preserve_resources(self):
        handle = self.grant()
        output = Handle(91, 92)
        self.assertEqual(self.lib.mg_os_files_grant(self.owner, self.file.fileno(),
                         READ, 0, 1, c.byref(output)), LIMIT)
        self.assertEqual((output.connection, output.object), (91, 92))
        foreign = Handle(handle.connection + 1, handle.object)
        self.assertEqual(self.read(foreign, 0, 1)[:2], (STALE, 99))
        self.assertEqual(self.lib.mg_file_close_v1(self.endpoint, foreign), STALE)
        self.assertEqual(self.read(handle, 0, 1), (OK, 1, b'2'))

    def test_required_isolation_is_refused_before_owner_creation(self):
        owner, endpoint = c.c_void_p(91), c.c_void_p(92)
        self.assertEqual(self.lib.mg_os_files_create(1, 18, ISOLATED,
                         c.byref(owner), c.byref(endpoint)), UNSUPPORTED)
        self.assertEqual((owner.value, endpoint.value), (91, 92))
        selected = c.c_uint64(93)
        self.assertEqual(self.lib.mg_file_negotiate_v1(self.endpoint, 1, ISOLATED,
                         c.byref(selected)), UNSUPPORTED)
        self.assertEqual(selected.value, 93)
        self.assertEqual(self.lib.mg_file_negotiate_v1(self.endpoint, 2, 0,
                         c.byref(selected)), UNSUPPORTED)
        self.assertEqual(self.read(self.grant(), 0, 1), (OK, 1, b'2'))

    def test_source_descriptor_rights_and_append_cannot_be_widened(self):
        with tempfile.TemporaryDirectory() as directory:
            name = Path(directory) / 'source'
            name.write_bytes(b'original')
            for flags, expected in [(os.O_RDONLY, DENIED),
                                    (os.O_RDWR | os.O_APPEND, UNSUPPORTED)]:
                fd = os.open(name, flags)
                try:
                    output = Handle(91, 92)
                    self.assertEqual(self.lib.mg_os_files_grant(self.owner, fd,
                                     WRITE, 0, 8, c.byref(output)), expected)
                    self.assertEqual((output.connection, output.object), (91, 92))
                finally:
                    os.close(fd)
            self.assertEqual(name.read_bytes(), b'original')
        # Rejected grants did not occupy the only slot.
        self.assertEqual(self.read(self.grant(), 0, 1), (OK, 1, b'2'))


if __name__ == '__main__':
    unittest.main()
