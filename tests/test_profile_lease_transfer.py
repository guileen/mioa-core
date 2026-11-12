"""Real Profile escrow transfers; no browser or sandbox qualification."""
import array
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import unittest

from mioa_core.profile_service import ProfileServiceError, receive_leases
from mioa_os import LocalProfileLeases, LocalUnixMessages


class ProfileLeaseTransferTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.leases = LocalProfileLeases()
        self.messages = LocalUnixMessages()

    def channel(self):
        client, host = socket.socketpair()
        for endpoint in (client, host):
            endpoint.settimeout(2)
            self.addCleanup(endpoint.close)
        return client, host

    def locked_file(self, name="lease"):
        path = self.directory / name
        path.write_bytes(b"abcdef")
        source = path.open("r+b", buffering=0)
        self.addCleanup(source.close)
        fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return source, path

    def assert_unlocked(self, path):
        with path.open("r+b", buffering=0) as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def send(self, host, body, descriptors, *, offset=0):
        packet = struct.pack("!I", len(body)) + body
        if offset:
            host.sendall(packet[:offset])
        sent = host.sendmsg([packet[offset:]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                array.array("i", descriptors))] if descriptors else [])
        host.sendall(packet[offset + sent:])

    def receive(self, client):
        leases = receive_leases(client, 1, leases=self.leases)
        for lease in leases:
            self.addCleanup(lease.close)
        return leases

    def receive_files(self, client):
        body, files = self.messages.receive_files(client, max_bytes=1048576, max_files=2)
        self.assertEqual(json.loads(body), {"id": 1, "ok": True})
        for file in files:
            self.addCleanup(file.close)
        return files

    def test_opaque_escrow_copies_retain_locks_until_closed(self):
        client, host = self.channel()
        first, first_path = self.locked_file("first")
        second, second_path = self.locked_file("second")
        self.send(host, b'{"id":1,"ok":true}', [first.fileno(), second.fileno()])
        copies = self.receive(client)
        self.assertEqual(len(copies), 2)
        first.close()
        second.close()
        for path in (first_path, second_path):
            with path.open("r+b", buffering=0) as competitor:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        copies[0].close()
        self.assert_unlocked(first_path)
        with second_path.open("r+b", buffering=0) as competitor:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        copies[1].close()
        self.assert_unlocked(second_path)

    def test_lease_right_on_body_fragment_is_received(self):
        client, host = self.channel()
        source, _ = self.locked_file()
        data = json.dumps({"id": 1, "ok": True}).encode()
        self.send(host, data, [source.fileno()], offset=6)
        leases = self.receive_files(client)
        self.assertEqual(os.fstat(leases[0].fileno()).st_ino, os.fstat(source.fileno()).st_ino)

    def test_shared_lock_offset_flags_and_two_file_wire_order_survive_transfer(self):
        client, host = self.channel()
        first, path = self.locked_file("first")
        second, second_path = self.locked_file("second")
        os.lseek(first.fileno(), 3, os.SEEK_SET)
        flags = fcntl.fcntl(first, fcntl.F_GETFL)
        fcntl.fcntl(first, fcntl.F_SETFL, flags | os.O_APPEND)
        self.send(host, b'{"id":1,"ok":true}', [first.fileno(), second.fileno()])
        leases = self.receive_files(client)
        for lease, source in zip(leases, (first, second), strict=True):
            self.assertEqual(os.fstat(lease.fileno()).st_ino, os.fstat(source.fileno()).st_ino)
            self.assertFalse(os.get_inheritable(lease.fileno()))
        self.assertEqual(os.lseek(leases[0].fileno(), 0, os.SEEK_CUR), 3)
        self.assertEqual(fcntl.fcntl(leases[0].fileno(), fcntl.F_GETFL), flags | os.O_APPEND)
        # Closing one received copy must never explicitly unlock the donor.
        leases[1].close()
        with second_path.open("r+b", buffering=0) as competitor:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        second.close()
        self.assert_unlocked(second_path)
        # Escrow keeps the same open-description lock after its donor exits.
        first.close()
        with path.open("r+b", buffering=0) as competitor:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            leases[0].close()
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_rejected_identity_or_json_releases_received_lock_copy(self):
        for index, body in enumerate((b'{"id":2,"ok":true}', b'{"id":true,"ok":true}',
                                      b'{"id":1,"ok":false}', b'[]', b'{',
                                      b'{"id":1,"id":1,"ok":true}')):
            with self.subTest(body=body):
                client, host = self.channel()
                source, path = self.locked_file(str(index))
                self.send(host, body, [source.fileno()])
                source.close()
                with self.assertRaises((ProfileServiceError, ValueError)):
                    self.receive(client)
                self.assert_unlocked(path)

    def test_partial_adoption_rejects_pipe_and_releases_file_copy(self):
        client, host = self.channel()
        source, path = self.locked_file()
        reader, writer = os.pipe()
        try:
            self.send(host, b'{"id":1,"ok":true}', [reader, source.fileno()])
        finally:
            os.close(reader)
        source.close()
        try:
            with self.assertRaises(ValueError):
                self.receive(client)
            with self.assertRaises(BrokenPipeError):
                os.write(writer, b"x")
            self.assert_unlocked(path)
        finally:
            os.close(writer)

    def test_oversized_batch_eof_and_bad_length_release_received_lock_copies(self):
        for kind in ("oversized", "eof", "length"):
            with self.subTest(kind=kind):
                client, host = self.channel()
                source, path = self.locked_file(kind)
                packet = struct.pack("!I", 1048577 if kind == "length" else 2) + b"{"
                if kind == "oversized":
                    packet = struct.pack("!I", 2) + b"{}"
                rights = [source.fileno()] * (200 if kind == "oversized" else 1)
                sent = host.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array("i", rights))])
                host.sendall(packet[sent:])
                source.close()
                host.close()
                with self.assertRaises(EOFError if kind == "eof" else ValueError):
                    self.receive(client)
                self.assert_unlocked(path)

    def test_socket_and_empty_batch_are_not_file_leases(self):
        for attach in (True, False):
            with self.subTest(attach=attach):
                client, host = self.channel()
                source, peer = self.channel()
                self.send(host, b'{"id":1,"ok":true}', [source.fileno()] if attach else [])
                source.close()
                with self.assertRaises(ValueError if attach else ProfileServiceError):
                    self.receive(client)
                self.assertEqual(peer.recv(1), b"")

    def test_repeated_close_cannot_close_reused_descriptor(self):
        client, host = self.channel()
        source, path = self.locked_file()
        self.send(host, b'{"id":1,"ok":true}', [source.fileno()])
        lease, = self.receive_files(client)
        number = lease.fileno()
        lease.close()
        replacement = os.open(path, os.O_RDONLY)
        if replacement != number:
            os.dup2(replacement, number)
            os.close(replacement)
        try:
            lease.close()
            self.assertEqual(os.read(number, 1), b"a")
            with self.assertRaises(ValueError):
                lease.fileno()
        finally:
            os.close(number)


if __name__ == "__main__":
    unittest.main()
