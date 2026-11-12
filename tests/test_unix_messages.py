"""Kernel descriptor/peer oracles for bounded framed receive."""
import array
import json
import os
import socket
import struct
import tempfile
import unittest
from unittest.mock import patch
import sys

from mioa_os import LocalManagedPeerIdentity, LocalUnixMessages


class UnixMessageTests(unittest.TestCase):
    def setUp(self):
        self.messages = LocalUnixMessages()
        self.client, self.host = self.pair()

    def pair(self, kind=socket.SOCK_STREAM):
        pair = socket.socketpair(type=kind)
        for endpoint in pair:
            endpoint.settimeout(2)
            self.addCleanup(endpoint.close)
        return pair

    def send(self, data, fds=()):
        sent = self.host.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                         array.array("i", fds))] if fds else [])
        self.host.sendall(data[sent:])

    def receive(self, limit=4):
        return self.messages.receive(self.client, max_bytes=64, max_sockets=limit)

    def test_peer_audit_token_distinguishes_supervisor_and_cdp(self):
        class Channel:
            family = socket.AF_UNIX
            type = socket.SOCK_STREAM
            socket_type = socket.SOCK_STREAM
            def __init__(self, token):
                self.token = token
            def getsockopt(self, level, option, *args):
                if level == socket.SOL_SOCKET and option == socket.SO_TYPE:
                    return self.socket_type
                self.assert_token_call(level, option, args)
                return self.token
            def assert_token_call(self, level, option, args):
                if (level, option, args) != (0, 0x006, (32,)):
                    raise AssertionError("unexpected socket option")

        uid, pid = os.geteuid(), 100
        def token(uid=uid, pid=pid, version=1):
            return struct.pack("=8I", 0, uid, 0, 0, 0, pid, 0, version)

        authority = LocalManagedPeerIdentity()
        def message(payload):
            _, handle = authority._unwrap(json.dumps({"fd_transfer_id": 1, "payload": payload}).encode())
            return handle
        _, peer = authority.child_enrollment(message({"op": "enroll", "launch_id": 4, "pid": pid}))
        authority.child_ready(message({"op": "ready", "launch_id": 4, "pid": pid, "uid": uid}), peer)
        self.assertTrue(LocalManagedPeerIdentity().verify_supervisor(Channel(token(version=0))))
        self.assertFalse(authority.verify_child(Channel(token(version=0)), peer))
        self.assertTrue(authority.verify_child(Channel(token()), peer))
        self.assertFalse(LocalManagedPeerIdentity().verify_supervisor(Channel(token(uid=uid + 1))))
        self.assertFalse(authority.verify_child(Channel(token(pid=pid + 1)), peer))
        self.assertFalse(LocalManagedPeerIdentity().verify_supervisor(Channel(b"short")))
        channel = Channel(token())
        channel.family = socket.AF_INET
        self.assertFalse(LocalManagedPeerIdentity().verify_supervisor(channel))
        channel.family, channel.type = socket.AF_UNIX, socket.SOCK_STREAM
        channel.socket_type = socket.SOCK_DGRAM
        self.assertFalse(authority.verify_child(channel, peer))
        datagram, _ = self.pair(socket.SOCK_DGRAM)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=os.dup(datagram.fileno())) as mislabeled:
            self.assertFalse(authority.verify_child(mislabeled, peer))
        with self.assertRaises(ValueError):
            LocalManagedPeerIdentity().verify_child(Channel(token()), peer)

    def test_identity_frames_are_read_only_from_bound_supervisor(self):
        authority = LocalManagedPeerIdentity()
        with self.assertRaises(ValueError):
            authority.receive_frame(self.client)
        with patch.object(authority, "_verify", return_value=True):
            self.assertTrue(authority.verify_supervisor(self.client))
        with self.assertRaises(ValueError):
            authority.receive_frame(self.host)
        source, peer = self.pair()
        body = json.dumps({"fd_transfer_id": 7, "payload": {
            "op": "enroll", "launch_id": 9, "pid": 121}}).encode()
        self.send(struct.pack("!I", len(body)) + body, [source.fileno()])
        receipt, message, received = authority.receive_frame(self.client)
        try:
            self.assertEqual(receipt, 7)
            result, enrolled = authority.child_enrollment(message)
            self.assertEqual(result, {"op": "enroll", "launch_id": 9})
            self.assertFalse(hasattr(enrolled, "pid"))
            with self.assertRaises(ValueError):
                authority.child_enrollment(message)
        finally:
            for endpoint in received:
                endpoint.close()

    @unittest.skipUnless(sys.platform == "darwin", "Darwin supervisor peer token")
    def test_identity_rejects_duplicate_fields_and_closes_received_sockets(self):
        authority = LocalManagedPeerIdentity()
        self.assertTrue(authority.verify_supervisor(self.client))
        bodies = (
            b'{"fd_transfer_id":1,"fd_transfer_id":2,"payload":{"op":"enroll","launch_id":9,"pid":121}}',
            b'{"fd_transfer_id":1,"payload":{"op":"enroll","launch_id":9,"pid":120,"pid":121}}',
        )
        for body in bodies:
            with self.subTest(body=body):
                source, peer = self.pair()
                self.send(struct.pack("!I", len(body)) + body, [source.fileno()])
                try:
                    with self.assertRaises(ValueError):
                        _, _, received = authority.receive_frame(self.client)
                        for endpoint in received:
                            endpoint.close()
                finally:
                    source.close()
                self.assertEqual(peer.recv(1), b"")
        body = b'{"op":"reject","op":"reject","code":"denied","domain":0}'
        self.send(struct.pack("!I", len(body)) + body)
        with self.assertRaises(ValueError):
            authority.receive_frame(self.client)

    def test_enrollment_correlates_ready_and_keeps_identity_opaque(self):
        authority = LocalManagedPeerIdentity()
        def message(payload):
            return authority._unwrap(json.dumps({"fd_transfer_id": 1, "payload": payload}).encode())[1]
        enrollment, peer = authority.child_enrollment(message({"op": "enroll", "launch_id": 9, "pid": 121}))
        self.assertEqual(enrollment, {"op": "enroll", "launch_id": 9})
        self.assertFalse(hasattr(peer, "pid"))
        with self.assertRaises(ValueError):
            authority.verify_child(self.client, peer)
        ready = {"op": "ready", "launch_id": 9, "pid": 121, "uid": os.geteuid()}
        for invalid in ({"launch_id": 10}, {"pid": 122}, {"uid": os.geteuid() + 1},
                        {"launch_id": True}, {"pid": 121.0}, {"uid": float(os.geteuid())}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                authority.child_ready(message(ready | invalid), peer)
        self.assertEqual(authority.child_ready(message(ready), peer), {"op": "ready", "launch_id": 9})
        with self.assertRaises(ValueError):
            LocalManagedPeerIdentity().child_ready(message(ready), peer)

    def test_profile_ready_is_correlated_inside_authority(self):
        authority = LocalManagedPeerIdentity()
        wire = json.dumps({"fd_transfer_id": 1, "payload": {
            "op": "profile_started", "launch_id": 5, "pid": 123, "application_domains": [0]}}).encode()
        _, message = authority._unwrap(wire)
        reply, peer = authority.profile_started(message)
        self.assertEqual(reply, {"op": "profile_started", "launch_id": 5, "application_domains": [0]})
        body = json.dumps({"ready": True, "pid": 124}).encode()
        self.host.sendall(struct.pack("!I", len(body)) + body)
        with self.assertRaises(ValueError):
            authority.profile_ready(self.client, peer)

    def test_profile_ready_rejects_noninteger_identity(self):
        authority = LocalManagedPeerIdentity()
        wire = json.dumps({"fd_transfer_id": 1, "payload": {
            "op": "profile_started", "launch_id": 5, "pid": 1, "application_domains": [0]}}).encode()
        _, message = authority._unwrap(wire)
        _, peer = authority.profile_started(message)
        for pid in (True, 1.0):
            with self.subTest(pid=pid):
                body = json.dumps({"ready": True, "pid": pid}).encode()
                self.host.sendall(struct.pack("!I", len(body)) + body)
                with self.assertRaises(ValueError):
                    authority.profile_ready(self.client, peer)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin audit token")
    def test_peer_audit_token_for_real_connected_socket(self):
        authority = LocalManagedPeerIdentity()
        def message(payload):
            return authority._unwrap(json.dumps({"fd_transfer_id": 1, "payload": payload}).encode())[1]
        _, peer = authority.child_enrollment(message({"op": "enroll", "launch_id": 4, "pid": os.getpid()}))
        authority.child_ready(message({"op": "ready", "launch_id": 4, "pid": os.getpid(), "uid": os.geteuid()}), peer)
        self.assertTrue(authority.verify_supervisor(self.client))
        self.assertTrue(authority.verify_child(self.client, peer))


    def test_send_sockets_completes_short_write_once_and_retains_donor(self):
        source, peer = self.pair()

        class ShortChannel:
            def sendmsg(self, chunks, ancillary):
                return self.channel.sendmsg([chunks[0][:2]], ancillary)

            def sendall(self, data):
                self.channel.sendall(data)

        channel = ShortChannel()
        channel.channel = self.host
        self.messages.send_sockets(channel, b"body", [source], max_bytes=64)
        body, received = self.receive()
        self.addCleanup(received[0].close)
        self.assertEqual(body, b"body")
        self.assertEqual(len(received), 1)
        self.assertGreaterEqual(source.fileno(), 0)
        peer.sendall(b"x")
        self.assertEqual(received[0].recv(1), b"x")

    def test_send_failure_keeps_donor_and_channel_owned(self):
        source, peer = self.pair()

        class FailingChannel:
            def sendmsg(self, chunks, ancillary):
                return self.channel.sendmsg([chunks[0][:2]], ancillary)

            def sendall(self, data):
                raise BrokenPipeError("short write completion failed")

        channel = FailingChannel()
        channel.channel = self.host
        with self.assertRaises(BrokenPipeError):
            self.messages.send_sockets(channel, b"body", [source], max_bytes=64)
        self.assertGreaterEqual(source.fileno(), 0)
        self.assertGreaterEqual(self.host.fileno(), 0)
        self.host.close()
        with self.assertRaises(EOFError):
            self.receive()
        peer.sendall(b"x")
        self.assertEqual(source.recv(1), b"x")

    def test_zero_send_fails_without_sending_frame_without_rights(self):
        source, _ = self.pair()

        class ZeroChannel:
            def sendmsg(self, chunks, ancillary):
                return 0

            def sendall(self, data):
                raise AssertionError("cannot send frame after rights were not sent")

        with self.assertRaises(EOFError):
            self.messages.send_sockets(ZeroChannel(), b"body", [source], max_bytes=64)
        self.assertGreaterEqual(source.fileno(), 0)

    def test_send_files_borrows_received_file_and_preserves_description(self):
        with tempfile.TemporaryFile() as source:
            source.write(b"abcdef")
            source.seek(2)
            self.messages.send_files(self.host, b"first", [source.fileno()], max_bytes=64)
            body, files = self.messages.receive_files(self.client, max_bytes=64, max_files=2)
            self.assertEqual(body, b"first")
            self.addCleanup(files[0].close)
            self.messages.send_files(self.host, b"second", files, max_bytes=64)
            body, copies = self.messages.receive_files(self.client, max_bytes=64, max_files=2)
            self.addCleanup(copies[0].close)
            self.assertEqual(body, b"second")
            self.assertEqual(os.read(copies[0].fileno(), 1), b"c")
            self.assertEqual(source.tell(), 3)
            self.assertGreaterEqual(files[0].fileno(), 0)

    def test_send_bounds_fail_before_writing_or_closing_donors(self):
        source, peer = self.pair()
        for body, endpoints, limit in ((b"", [source], 64), (b"ab", [source], 1),
                                       (b"x", [source] * 5, 64), (b"x", [source], 0)):
            with self.subTest(body=body, count=len(endpoints), limit=limit):
                with self.assertRaises(ValueError):
                    self.messages.send_sockets(self.host, body, endpoints, max_bytes=limit)
        with tempfile.TemporaryFile() as file:
            with self.assertRaises(ValueError):
                self.messages.send_files(self.host, b"x", [file.fileno()] * 3, max_bytes=64)
            self.assertGreaterEqual(file.fileno(), 0)
        self.assertGreaterEqual(source.fileno(), 0)
        peer.sendall(b"x")
        self.assertEqual(source.recv(1), b"x")
        self.host.sendall(b"z")
        self.assertEqual(self.client.recv(1), b"z")


    def test_owned_sockets_preserve_order_and_next_frame(self):
        first, peer1 = self.pair()
        second, peer2 = self.pair()
        self.send(struct.pack("!I", 3) + b"one", [first.fileno(), second.fileno()])
        self.send(struct.pack("!I", 3) + b"two")
        first.close()
        second.close()
        body, endpoints = self.receive()
        for endpoint in endpoints:
            self.addCleanup(endpoint.close)
            endpoint.settimeout(2)
            self.assertFalse(os.get_inheritable(endpoint.fileno()))
        self.assertEqual(body, b"one")
        peer1.sendall(b"A")
        peer2.sendall(b"B")
        self.assertEqual([endpoint.recv(1) for endpoint in endpoints], [b"A", b"B"])
        endpoints[0].sendall(b"C")
        self.assertEqual(peer1.recv(1), b"C")
        self.assertEqual(self.receive(), (b"two", []))

    def test_descriptors_on_later_header_and_body_fragments(self):
        for stage in ("header", "body"):
            with self.subTest(stage=stage):
                source, peer = self.pair()
                packet = struct.pack("!I", 4) + b"body"
                offset = 2 if stage == "header" else 6
                self.send(packet[:offset])
                self.send(packet[offset:], [source.fileno()])
                source.close()
                body, endpoints = self.receive()
                self.assertEqual(body, b"body")
                self.addCleanup(endpoints[0].close)
                endpoints[0].settimeout(2)
                peer.sendall(b"X")
                self.assertEqual(endpoints[0].recv(1), b"X")
                endpoints[0].close()
                self.assertEqual(peer.recv(1), b"")

    def test_partial_adoption_closes_wrapped_raw_and_invalid_pipe(self):
        first, peer1 = self.pair()
        last, peer2 = self.pair()
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)
        try:
            self.send(struct.pack("!I", 1) + b"x", [first.fileno(), reader, last.fileno()])
        finally:
            os.close(reader)
        first.close()
        last.close()
        with self.assertRaises((OSError, ValueError)):
            self.receive()
        self.assertEqual(peer1.recv(1), b"")
        self.assertEqual(peer2.recv(1), b"")
        with self.assertRaises(BrokenPipeError):
            os.write(writer, b"x")

    def test_rejects_datagram_and_unconnected_stream(self):
        datagram, peer = self.pair(socket.SOCK_DGRAM)
        unconnected = socket.socket(socket.AF_UNIX)
        self.addCleanup(unconnected.close)
        for source in (datagram, unconnected):
            with self.subTest(kind=source.type):
                self.send(struct.pack("!I", 1) + b"x", [source.fileno()])
                with self.assertRaises((ValueError, OSError)):
                    self.receive()
        # Borrowed channel is not closed by OS failure cleanup.
        self.host.sendall(b"z")
        self.assertEqual(self.client.recv(1), b"z")

    def test_oversized_ancillary_batches_close_every_received_reader(self):
        for count in (2, 16, 200):
            with self.subTest(count=count):
                reader, writer = os.pipe()
                try:
                    self.send(struct.pack("!I", 1) + b"x", [reader] * count)
                finally:
                    os.close(reader)
                try:
                    with self.assertRaises(ValueError):
                        self.receive(limit=1)
                    with self.assertRaises(BrokenPipeError):
                        os.write(writer, b"x")
                finally:
                    os.close(writer)

    def test_eof_and_invalid_lengths_release_rights(self):
        for data, error in ((b"\x00\x00", EOFError),
                            (struct.pack("!I", 3) + b"x", EOFError),
                            (struct.pack("!I", 0), ValueError),
                            (struct.pack("!I", 65), ValueError)):
            with self.subTest(data=data):
                client, host = self.pair()
                source, peer = self.pair()
                sent = host.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                             array.array("i", [source.fileno()]))])
                host.sendall(data[sent:])
                host.close()
                source.close()
                with self.assertRaises(error):
                    self.messages.receive(client, max_bytes=64, max_sockets=1)
                self.assertEqual(peer.recv(1), b"")

    def reader(self, client=None, limit=4):
        client = self.client if client is None else client
        client.settimeout(None)
        reader = self.messages.reader(client, max_bytes=64, max_sockets=limit)
        self.addCleanup(reader.close)
        return reader

    def test_incremental_fragments_reuse_and_no_read_ahead(self):
        reader = self.reader()
        self.assertIsNone(reader.read_available())
        source, peer = self.pair()
        packet = struct.pack("!I", 4) + b"body"
        self.send(packet[:2])
        self.assertIsNone(reader.read_available())
        self.send(packet[2:6], [source.fileno()])
        self.assertIsNone(reader.read_available())
        self.send(packet[6:])
        self.send(struct.pack("!I", 4) + b"next")
        body, endpoints = reader.read_available()
        self.assertEqual(body, b"body")
        self.addCleanup(endpoints[0].close)
        self.assertFalse(os.get_inheritable(endpoints[0].fileno()))
        source.close()
        peer.sendall(b"x")
        self.assertEqual(endpoints[0].recv(1), b"x")
        # The next frame is still on the borrowed channel, not hidden in a buffer.
        self.assertEqual(self.client.recv(4), struct.pack("!I", 4))
        self.assertEqual(self.client.recv(4), b"next")
        self.send(struct.pack("!I", 5) + b"again")
        self.assertEqual(reader.read_available(), (b"again", []))
        reader.close()
        peer.sendall(b"y")
        self.assertEqual(endpoints[0].recv(1), b"y")
        self.assertIsNone(self.client.gettimeout())
        self.assertTrue(os.get_blocking(self.client.fileno()))

    def test_incremental_close_releases_pending_rights_not_channel(self):
        for offset in (2, 6):
            with self.subTest(offset=offset):
                reader = self.reader()
                source, peer = self.pair()
                packet = struct.pack("!I", 4) + b"body"
                self.send(packet[:offset], [source.fileno()])
                self.assertIsNone(reader.read_available())
                source.close()
                reader.close()
                reader.close()
                self.assertEqual(peer.recv(1), b"")
                with self.assertRaises(ValueError):
                    reader.read_available()
                self.host.sendall(b"x")
                self.assertEqual(self.client.recv(1), b"x")

    def test_incremental_eof_and_later_excess_are_terminal(self):
        for excess in (False, True):
            with self.subTest(excess=excess):
                client, host = self.pair()
                reader = self.reader(client, limit=1)
                source, peer = self.pair()
                packet = struct.pack("!I", 4) + b"body"
                host.sendmsg([packet[:2]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                           array.array("i", [source.fileno()]))])
                self.assertIsNone(reader.read_available())
                source.close()
                extra, other_peer = self.pair()
                if excess:
                    host.sendmsg([packet[2:]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array("i", [extra.fileno()]))])
                    error = ValueError
                else:
                    host.shutdown(socket.SHUT_WR)
                    error = EOFError
                with self.assertRaises(error):
                    reader.read_available()
                extra.close()
                self.assertEqual(peer.recv(1), b"")
                self.assertEqual(other_peer.recv(1), b"")
                with self.assertRaises(ValueError):
                    reader.read_available()

    def test_incremental_rejects_timeout_without_consuming_frame(self):
        self.send(struct.pack("!I", 3) + b"one")
        with self.assertRaises(ValueError):
            self.messages.reader(self.client, max_bytes=64, max_sockets=1)
        self.assertEqual(self.receive(), (b"one", []))


if __name__ == "__main__":
    unittest.main()
