import socket
import struct
import threading
import unittest
from unittest.mock import patch

from mioa_core.local_storage_stream import StreamStorage
from mioa_os import LocalSecureRandom
from mioa_os.profile_resource_grant import ResourceGrants


_REQ = struct.Struct('!4sIQQQQQ')
_RESP = struct.Struct('!4sIQQQ')


class ProfileResourceGrantTests(unittest.TestCase):
    def test_spool_grants_are_bound_and_released_after_close(self):
        client, server = socket.socketpair()
        requests = []
        disposed = threading.Event()

        def host():
            pending = bytearray()
            while True:
                part = server.recv(_REQ.size - len(pending))
                if not part:
                    break
                pending.extend(part)
                if len(pending) != _REQ.size:
                    continue
                magic, op, seq, token, host_bytes, handles, disk = _REQ.unpack(pending)
                pending.clear()
                requests.append((op, token, host_bytes, handles, disk))
                if op == 2:
                    self.assertTrue(disposed.is_set())
                server.sendall(_RESP.pack(magic, 0, seq, seq if op == 1 else token, 0))
            server.close()

        thread = threading.Thread(target=host)
        thread.start()
        grants = ResourceGrants()
        grants.bind(7, client)
        with self.assertRaises(ValueError):
            grants.open_spool(8)
        spool = grants.open_spool(7)
        spool.reserve(1)
        spool.write(b'x')
        spool.seek(0)
        self.assertEqual(spool.read(1), b'x')
        spool.discard_file()
        disposed.set()
        spool.close()
        grants.unbind(7)
        thread.join()
        self.assertEqual(requests[0][0:4], (1, 0, 1024 * 1024, 1))
        self.assertGreaterEqual(requests[1][4], 1)
        self.assertEqual([r[0] for r in requests], [1, 1, 2, 2])

    def _check_cleanup_after_grant_channel_loss(self, *, disconnect):
        client, server = socket.socketpair()

        def host():
            try:
                for _ in range(2):
                    request = bytearray()
                    while len(request) < _REQ.size:
                        part = server.recv(_REQ.size - len(request))
                        if not part:
                            return
                        request.extend(part)
                    magic, op, seq, *_ = _REQ.unpack(request)
                    if op != 1:
                        return
                    server.sendall(_RESP.pack(magic, 0, seq, seq, 0))
                server.recv(_REQ.size)
            finally:
                server.close()

        thread = threading.Thread(target=host)
        thread.start()
        grants = ResourceGrants()
        grants.bind(1, client)
        storage = StreamStorage(None, grants, LocalSecureRandom())
        payload = {"origin": "https://example.test", "kind": "set",
                   "key_length": 0, "value_length": 0}
        try:
            first = storage.dispatch("local.mutation.begin", payload, 1)["token"]
            second = storage.dispatch("local.mutation.begin", payload, 1)["token"]
            spools = [storage.streams[token]["grant"] for token in (first, second)]
            with self.assertRaises(OSError):
                storage.disconnect(1) if disconnect else storage.close()
            self.assertTrue(all(spool._file.closed for spool in spools))
            self.assertEqual(storage.streams, {})
        finally:
            grants.close()
            thread.join(timeout=2)
            for stream in storage.streams.values():
                stream["grant"].discard_file()

    def test_disconnect_closes_all_spools_after_grant_channel_loss(self):
        self._check_cleanup_after_grant_channel_loss(disconnect=True)

    def test_close_closes_all_spools_after_grant_channel_loss(self):
        self._check_cleanup_after_grant_channel_loss(disconnect=False)

    def test_close_attempts_other_channels_when_one_fails(self):
        first, first_peer = socket.socketpair()
        second, second_peer = socket.socketpair()
        second_peer.settimeout(0.2)
        grants = ResourceGrants()
        try:
            grants.bind(1, first)
            grants.bind(2, second)
            with patch.object(grants._channels[1], "close", side_effect=OSError("close failed")):
                with self.assertRaisesRegex(OSError, "close failed"):
                    grants.close()
            self.assertEqual(second_peer.recv(1), b"")
        finally:
            first.close()
            second.close()
            first_peer.close()
            second_peer.close()

    def test_eof_is_unknown_and_never_retried(self):
        client, server = socket.socketpair()
        grants = ResourceGrants()
        grants.bind(1, client)
        server.close()
        with self.assertRaises(OSError):
            grants.open_spool(1)
        with self.assertRaises(OSError):
            grants.open_spool(1)
        grants.close()

    def test_partial_reply_cannot_extend_request_deadline(self):
        client, server = socket.socketpair()
        stop = threading.Event()

        def host():
            try:
                pending = bytearray()
                while len(pending) < _REQ.size:
                    part = server.recv(_REQ.size - len(pending))
                    if not part:
                        return
                    pending.extend(part)
                magic, _, seq, *_ = _REQ.unpack(pending)
                for byte in _RESP.pack(magic, 0, seq, 1, 0):
                    server.sendall(bytes([byte]))
                    if stop.wait(0.04):
                        return
            except OSError:
                pass

        thread = threading.Thread(target=host)
        thread.start()
        grants = ResourceGrants()
        try:
            with patch('mioa_os.profile_resource_grant._TIMEOUT', 0.2):
                grants.bind(1, client)
                with self.assertRaises(OSError):
                    grants.open_spool(1)
                with self.assertRaisesRegex(OSError, 'unavailable'):
                    grants.open_spool(1)
        finally:
            stop.set()
            grants.close()
            server.close()
            thread.join()
