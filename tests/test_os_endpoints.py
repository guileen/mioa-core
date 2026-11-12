"""Real Unix sockets and filesystem oracles, not sandbox qualification."""
import os
from pathlib import Path
import socket
import stat
import unittest

from mioa_os import LocalUnixEndpoints


class UnixEndpointTests(unittest.TestCase):
    def endpoint(self):
        lease = LocalUnixEndpoints().create()
        self.addCleanup(lease.dispose)
        return lease

    def test_unique_private_short_paths_support_real_socket_exchange(self):
        first, second = self.endpoint(), self.endpoint()
        self.assertNotEqual(first.path, second.path)
        for lease in (first, second):
            self.assertEqual(lease.directory.parent, Path("/tmp"))
            self.assertEqual(stat.S_IMODE(lease.directory.stat().st_mode), 0o700)
            self.assertLess(len(os.fsencode(lease.path)), 104)
        with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as client:
            listener.bind(str(first.path))
            listener.listen(1)
            client.connect(str(first.path))
            connection, _ = listener.accept()
            with connection:
                client.sendall(b"endpoint-alive")
                self.assertEqual(connection.recv(32), b"endpoint-alive")
        first.dispose()
        self.assertFalse(first.directory.exists())
        self.assertTrue(second.directory.exists())

    def test_unexpected_directory_contents_are_preserved_and_cleanup_can_retry(self):
        lease = self.endpoint()
        unexpected = lease.directory / "keep-user-data"
        unexpected.write_bytes(b"preserve")
        try:
            with self.assertRaises(OSError):
                lease.dispose()
            self.assertEqual(unexpected.read_bytes(), b"preserve")
            self.assertTrue(lease.directory.exists())
        finally:
            unexpected.unlink()
        lease.dispose()
        self.assertFalse(lease.directory.exists())

    def test_regular_file_or_symlink_at_socket_name_is_never_deleted(self):
        lease = self.endpoint()
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                target = lease.directory / "user-data"
                target.write_bytes(b"preserve")
                if symlink:
                    lease.path.symlink_to(target)
                else:
                    lease.path.write_bytes(b"not a socket")
                try:
                    with self.assertRaises(OSError):
                        lease.dispose()
                    self.assertEqual(target.read_bytes(), b"preserve")
                    self.assertTrue(lease.path.exists())
                finally:
                    lease.path.unlink()
                    target.unlink()

    def test_completed_lease_cannot_delete_replacement_using_same_pathname(self):
        lease = self.endpoint()
        lease.dispose()
        lease.directory.mkdir(mode=0o700)
        try:
            with socket.socket(socket.AF_UNIX) as replacement:
                replacement.bind(str(lease.path))
                lease.dispose()
                self.assertTrue(stat.S_ISSOCK(lease.path.stat().st_mode))
        finally:
            lease.path.unlink()
            lease.directory.rmdir()


if __name__ == "__main__":
    unittest.main()
