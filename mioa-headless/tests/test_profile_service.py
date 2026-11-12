"""Exercise the private Profile owner without using a user's profile."""
import array
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import struct
import tempfile
import threading
import unittest

from mioa_core import native_spec, profile
from mioa_core.native import NativeError, _Supervisor
from mioa_core.profile_service import (ProfileClient, ProfileServiceError, _receive, await_owner_ready,
    confirm_lease_escrow, enroll_domain, enroll_owner, receive_leases)
from mioa_os import (LocalArtifactContent, LocalBlockingCalls, LocalManagedPeerIdentity, LocalProcessIdentity, LocalProfileLeases, LocalSecureRandom,
                     LocalUnixEndpoints, LocalUnixMessages, profile_owner_command)

def start_profile_service(name, home, *, command):
    control, child = socket.socketpair()
    process = None
    try:
        process = subprocess.Popen([*command, str(child.fileno()), name, str(home.resolve())],
                                   pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL)
        child.close()
        control.settimeout(30)
        response = _receive(control)
        control.settimeout(None)
        if response.get("ready") is not True or response.get("pid") != process.pid:
            raise ProfileServiceError(response.get("error", "profile service did not start"))
        return process, control
    except BaseException:
        control.close()
        child.close()
        if process is not None:
            process.terminate()
            process.wait()
        raise


class StorageClient(ProfileClient):
    """Use the actual principal-bound stream RPC, including commit and close."""
    def local_mutation(self, origin, kind, key, value=""):
        key, value = key.encode(), value.encode()
        token = self.request("local.mutation.begin", origin=origin, kind=kind,
                             key_length=len(key), value_length=len(value))["token"]
        data = key + value
        for offset in range(0, len(data), 192 * 1024):
            self.request("local.mutation.chunk", origin=origin, token=token, offset=offset,
                         data=base64.b64encode(data[offset:offset + 192 * 1024]).decode())
        self.request("local.mutation.commit", origin=origin, token=token)

    def local_snapshot(self, origin):
        snapshot = self.request("local.snapshot.begin", origin=origin)
        token, rows = snapshot["token"], []
        try:
            for _ in range(snapshot["count"]):
                record = self.request("local.snapshot.next", origin=origin, token=token)
                size = record["key_length"] + record["value_length"]
                data = bytearray()
                while len(data) < size:
                    chunk = self.request("local.snapshot.read", origin=origin, token=token, offset=len(data))
                    data.extend(base64.b64decode(chunk["data"], validate=True))
                rows.append([data[:record["key_length"]].decode(), data[record["key_length"]:].decode()])
            return rows
        finally:
            self.request("local.snapshot.end", origin=origin, token=token)



class ProfileServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = profile.BASE
        profile.BASE = Path(self.temp.name)
        data = profile.profile_dir("test") / "data"
        binary = Path(self.temp.name) / "runtime"
        binary.write_bytes(b"test runtime")
        binary.chmod(0o700)
        font = Path(self.temp.name) / "font"
        font.write_bytes(b"test font")
        cfg = native_spec.create_native_spec("test", runtime="mioa", seed="ab" * 32,
            binary=binary, storage_path=data, asset_paths={"fonts": [str(font)]},
            artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
        profile.save_config("test", cfg)
        self.process, self.control = start_profile_service("test", profile.BASE, command=profile_owner_command())
        self.escrow_leases = receive_leases(self.control, 1, leases=LocalProfileLeases())
        confirm_lease_escrow(self.control, 2)
        self.next_control_id = 3
        self.auxiliary = []
        self.grant_threads = []
        self.grant_errors = []
        self.cookie_processes = []
        self.a, self.b = self.enroll_storage_domain(), self.enroll_storage_domain()

    def control_id(self):
        result = self.next_control_id
        self.next_control_id += 1
        return result

    def enroll_storage_domain(self):
        pairs = [socket.socketpair() for _ in range(4)]
        domain, events, idb, grants = pairs
        self.auxiliary.extend((events[0], idb[0]))
        request = struct.Struct("!4sIQQQQQ")
        reply = struct.Struct("!4sIQQQ")

        def grant_host():
            active = set()
            previous = 0
            try:
                while True:
                    data = bytearray()
                    while len(data) < request.size:
                        part = grants[0].recv(request.size - len(data))
                        if not part:
                            self.assertFalse(data)
                            self.assertFalse(active)
                            return
                        data.extend(part)
                    magic, operation, sequence, token, host, handles, disk = request.unpack(data)
                    self.assertEqual(magic, b"PGR1")
                    self.assertGreater(sequence, previous)
                    previous = sequence
                    if operation == 1:
                        self.assertEqual(token, 0)
                        self.assertLess(len(active), 32)
                        self.assertLessEqual(host, 1024 * 1024)
                        self.assertLessEqual(handles, 1)
                        self.assertLessEqual(disk, 16 * 1024 * 1024)
                        active.add(sequence)
                        result = sequence
                    else:
                        self.assertEqual(operation, 2)
                        self.assertIn(token, active)
                        active.remove(token)
                        result = token
                    grants[0].sendall(reply.pack(magic, 0, sequence, result, 0))
            except BaseException as exc:
                self.grant_errors.append(exc)
            finally:
                grants[0].close()

        worker = threading.Thread(target=grant_host)
        worker.start()
        self.grant_threads.append(worker)
        try:
            enroll_domain(self.control, domain[1], self.control_id(), events[1], idb[1], grants[1],
                          messages=LocalUnixMessages())
        finally:
            for _, owner in pairs:
                owner.close()
        return StorageClient(domain[0])

    def close_auxiliary(self):
        for endpoint in self.auxiliary:
            endpoint.close()
        self.auxiliary.clear()

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.control.close()
        self.close_auxiliary()
        self.process.wait(timeout=10)
        for owner in self.cookie_processes:
            try:
                _, stderr = owner.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.communicate()
                self.fail("native cookie owner did not exit after service closure")
            self.assertEqual(owner.returncode, 0, stderr.decode(errors="replace"))
        for lease in self.escrow_leases:
            lease.close()
        profile.BASE = self.previous
        self.temp.cleanup()
        for worker in self.grant_threads:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.grant_errors, [])

    def test_lease_shared_origin_and_durable_storage(self):
        with profile.exclusive("test", leases=LocalProfileLeases()) as acquired:
            self.assertFalse(acquired)
        self.a.local_mutation("https://example.test", "set", "one", "1")
        self.assertEqual(self.b.local_snapshot("https://example.test"), [["one", "1"]])
        self.assertEqual(self.b.local_snapshot("https://other.test"), [])
        self.b.local_mutation("https://example.test", "set", "two", "2")
        self.a.local_mutation("https://example.test", "remove", "one")
        self.assertEqual(self.b.local_snapshot("https://example.test"), [["two", "2"]])
        db = profile.profile_dir("test") / "data" / ("local-" + hashlib.sha256(b"https://example.test").hexdigest() + ".sqlite")
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute("select key,value from entries").fetchall(), [("two", "2")])


    def test_isolated_processes_share_one_service(self):
        setup = ("import json,socket,sys; sys.path.insert(0,sys.argv[2]);"
                 " from test_profile_service import StorageClient;"
                 " c=StorageClient(socket.socket(fileno=int(sys.argv[1])));")
        writer = setup + " c.local_mutation('https://example.test','set','k','v')"
        reader = setup + " print(json.dumps(c.local_snapshot('https://example.test')))"
        tests = str(Path(__file__).resolve().parent)
        a_fd = self.a.endpoint.fileno()
        b_fd = self.b.endpoint.fileno()
        subprocess.run([sys.executable, "-c", writer, str(a_fd), tests], pass_fds=(a_fd,), check=True)
        result = subprocess.run([sys.executable, "-c", reader, str(b_fd), tests], pass_fds=(b_fd,),
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), [["k", "v"]])
    def test_duplicate_request_id_closes_only_that_domain(self):
        from mioa_core.profile_service import _receive, _send
        endpoint = self.a.endpoint
        self.a.local_mutation("https://example.test", "set", "k", "v")
        request = {"version": 1, "id": self.a.next_id, "operation": "session.ping", "payload": {}}
        _send(endpoint, request)
        self.assertTrue(_receive(endpoint)["ok"])
        _send(endpoint, request)
        self.assertEqual(endpoint.recv(1), b"")
        self.assertEqual(self.b.local_snapshot("https://example.test"), [["k", "v"]])

    def test_unrecognized_principal_field_is_not_authority(self):
        from mioa_core.profile_service import _send
        endpoint = self.a.endpoint
        _send(endpoint, {"version": 1, "id": 1, "operation": "local.snapshot",
                         "payload": {"origin": "https://example.test"}, "principal": "other"})
        self.assertEqual(endpoint.recv(1), b"")

    def test_supervisor_enrolls_connected_domain_endpoint(self):
        client = self.enroll_storage_domain()
        try:
            client.local_mutation("https://example.test", "set", "via", "rights")
            self.assertEqual(self.b.local_snapshot("https://example.test"), [["via", "rights"]])
        finally:
            client.close()

    def test_fragmented_enrollment_keeps_new_and_existing_domains_usable(self):
        from mioa_core.profile_service import _receive
        origin = "https://example.test"
        self.a.local_mutation(origin, "set", "existing", "preserved")
        for offset in (2, 6):
            with self.subTest(rights_offset=offset):
                runtime, service = socket.socketpair()
                with runtime, service:
                    runtime.settimeout(2)
                    request_id = self.control_id()
                    body = json.dumps({"version": 1, "id": request_id, "operation": "enroll"}).encode()
                    packet = struct.pack("!I", len(body)) + body
                    self.control.sendall(packet[:offset])
                    sent = self.control.sendmsg([packet[offset:]],
                        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", service.fileno()))])
                    self.control.sendall(packet[offset + sent:])
                    self.assertEqual(_receive(self.control), {"id": request_id, "ok": True})
                    service.close()
                    client = ProfileClient(runtime)
                    try:
                        self.assertEqual(client.request("session.ping"), {"protocol": 1})
                        self.assertEqual(self.b.local_snapshot(origin), [["existing", "preserved"]])
                    finally:
                        client.close()

    def test_incomplete_control_frame_does_not_block_existing_domain_work(self):
        from mioa_core.profile_service import _receive
        origin = "https://example.test"
        self.a.endpoint.settimeout(2)
        self.b.endpoint.settimeout(2)
        self.a.local_mutation(origin, "set", "ongoing-task", "preserved")
        for offset in (2, 6):
            runtime, service = socket.socketpair()
            with runtime, service:
                runtime.settimeout(2)
                request_id = self.control_id()
                body = json.dumps({"version": 1, "id": request_id, "operation": "enroll"}).encode()
                packet = struct.pack("!I", len(body)) + body
                sent = self.control.sendmsg([packet[:offset]],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", service.fileno()))])
                self.control.sendall(packet[sent:offset])
                # No more control bytes until unrelated real work has completed.
                self.assertEqual(self.b.request("session.ping"), {"protocol": 1})
                self.assertEqual(self.a.local_snapshot(origin), [["ongoing-task", "preserved"]])
                self.control.sendall(packet[offset:])
                self.assertEqual(_receive(self.control), {"id": request_id, "ok": True})
                service.close()
                client = ProfileClient(runtime)
                try:
                    self.assertEqual(client.request("session.ping"), {"protocol": 1})
                finally:
                    client.close()

    def assert_control_loss_drains(self):
        with self.assertRaisesRegex(ProfileServiceError, "supervisor lost"):
            self.b.request("session.ping")
        self.assertIsNone(self.process.poll())
        # Prove the owner, not merely external escrow, retains its lease.
        for lease in self.escrow_leases:
            lease.close()
        self.escrow_leases.clear()
        with profile.exclusive("test", leases=LocalProfileLeases()) as acquired:
            self.assertFalse(acquired)
        self.a.close()
        self.b.close()
        self.close_auxiliary()
        self.assertEqual(self.process.wait(timeout=10), 0)
        with profile.exclusive("test", leases=LocalProfileLeases()) as acquired:
            self.assertTrue(acquired)

    def test_incomplete_control_eof_closes_pending_socket_and_retains_lease(self):
        runtime, service = socket.socketpair()
        with runtime, service:
            runtime.settimeout(2)
            body = json.dumps({"version": 1, "id": self.control_id(), "operation": "enroll"}).encode()
            packet = struct.pack("!I", len(body)) + body
            sent = self.control.sendmsg([packet[:6]],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", service.fileno()))])
            self.control.sendall(packet[sent:6])
            self.control.shutdown(socket.SHUT_WR)
            self.assertEqual(self.control.recv(1), b"")
            service.close()
            self.assertEqual(runtime.recv(1), b"")
            self.assert_control_loss_drains()

    def test_invalid_control_json_closes_socket_before_draining(self):
        runtime, service = socket.socketpair()
        with runtime, service:
            runtime.settimeout(2)
            packet = struct.pack("!I", 1) + b"{"
            sent = self.control.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                  struct.pack("=i", service.fileno()))])
            self.control.sendall(packet[sent:])
            self.control.settimeout(2)
            self.assertEqual(self.control.recv(1), b"")
            service.close()
            self.assertEqual(runtime.recv(1), b"")
            self.assert_control_loss_drains()

    def test_partial_control_adoption_closes_entire_batch_before_draining(self):
        first, peer1 = socket.socketpair()
        last, peer2 = socket.socketpair()
        reader, writer = os.pipe()
        try:
            with first, last, peer1, peer2:
                peer1.settimeout(2)
                peer2.settimeout(2)
                body = json.dumps({"version": 1, "id": self.control_id(), "operation": "enroll"}).encode()
                packet = struct.pack("!I", len(body)) + body
                rights = struct.pack("=3i", first.fileno(), reader, last.fileno())
                sent = self.control.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
                self.control.sendall(packet[sent:])
                self.control.settimeout(2)
                self.assertEqual(self.control.recv(1), b"")
                first.close()
                last.close()
                os.close(reader)
                reader = -1
                self.assertEqual(peer1.recv(1), b"")
                self.assertEqual(peer2.recv(1), b"")
                with self.assertRaises(BrokenPipeError):
                    os.write(writer, b"x")
                self.assert_control_loss_drains()
        finally:
            if reader >= 0:
                os.close(reader)
            os.close(writer)

    def test_replayed_control_identity_closes_unregistered_socket_without_ack(self):
        runtime, service = socket.socketpair()
        with runtime, service:
            runtime.settimeout(2)
            body = json.dumps({"version": 1, "id": self.next_control_id - 1, "operation": "enroll"}).encode()
            packet = struct.pack("!I", len(body)) + body
            sent = self.control.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                  struct.pack("=i", service.fileno()))])
            self.control.sendall(packet[sent:])
            self.control.settimeout(2)
            self.assertEqual(self.control.recv(1), b"")
            service.close()
            self.assertEqual(runtime.recv(1), b"")
            self.assert_control_loss_drains()

    def supervisor_ready(self, change=None, *, authority_error=None):
        from mioa_core.profile_service import _receive

        class RetainedMessages(LocalUnixMessages):
            def receive(self, *args, **kwargs):
                body, endpoints = super().receive(*args, **kwargs)
                received.extend(endpoints)
                return body, endpoints

        class FailedIdentity(LocalManagedPeerIdentity):
            def child_ready(self, message, identity):
                raise authority_error

        received, errors = [], []
        client, host = socket.socketpair()
        host.settimeout(3)
        supervisor = _Supervisor(client, endpoints=LocalUnixEndpoints(),
            blocking_calls=LocalBlockingCalls(), messages=RetainedMessages(),
            profile_command=profile_owner_command(), identity=LocalProcessIdentity(),
            managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        supervisor.owners_enrolled = True
        supervisor.profile_request_id = self.control_id()
        pairs = [socket.socketpair() for _ in range(5)]
        launch_id, pid, uid = 1, os.getpid(), os.geteuid()

        def transfer(value, sources, transfer_id):
            data = json.dumps({"fd_transfer_id": transfer_id, "payload": value}).encode()
            packet = struct.pack("!I", len(data)) + data
            sent = host.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                array.array("i", [source.fileno() for source in sources]))])
            host.sendall(packet[sent:])
            self.assertEqual(_receive(host), {"op": "fd_received", "fd_transfer_id": transfer_id})
            for source in sources:
                source.close()

        def serve():
            try:
                self.assertEqual(_receive(host), {"op": "spawn", "domain": 0})
                transfer({"op": "enroll", "launch_id": launch_id, "pid": pid},
                         [pair[1] for pair in pairs[:4]], 1)
                self.assertEqual(_receive(host), {"op": "enrolled", "launch_id": launch_id})
                if authority_error is not None:
                    supervisor.managed_identity.child_ready = FailedIdentity().child_ready
                ready = {"op": "ready", "launch_id": launch_id, "pid": pid, "uid": uid}
                if isinstance(change, dict):
                    ready.update(change)
                transfer(ready, [pairs[4][1]], 2)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve)
        worker.start()
        try:
            if change is None and authority_error is None:
                actual_launch, enrolled_peer, channel = asyncio.run(
                    supervisor.spawn({"op": "spawn", "domain": 0}, self.control))
                self.assertEqual(actual_launch, launch_id)
                self.assertNotIsInstance(enrolled_peer, int)
                pairs[4][0].sendall(b"ready")
                self.assertEqual(channel.recv(5), b"ready")
                channel.close()
            else:
                error_type = type(authority_error) if authority_error is not None else NativeError
                with self.assertRaises(error_type) as rejected:
                    asyncio.run(supervisor.spawn({"op": "spawn", "domain": 0}, self.control))
                if authority_error is not None:
                    self.assertIs(rejected.exception, authority_error)
                else:
                    self.assertEqual(rejected.exception.code, "verification_failed")
                self.assertEqual(client.fileno(), -1)
                pairs[4][0].settimeout(1)
                self.assertEqual(pairs[4][0].recv(1), b"", "unclaimed ready socket was not closed")
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(self.a.request("session.ping"), {"protocol": 1})
            self.assertEqual(self.b.request("session.ping"), {"protocol": 1})
        finally:
            client.close()
            host.close()
            for pair in pairs:
                for endpoint in pair:
                    endpoint.close()
            for endpoint in received:
                endpoint.close()
            worker.join(timeout=5)

    @unittest.skipUnless(sys.platform == "darwin", "supervisor peer audit token")
    def test_supervisor_ready_requires_exact_integer_identities(self):
        for change in ({"launch_id": True}, {"launch_id": 1.0},
                       {"pid": float(os.getpid())}, {"uid": float(os.geteuid())}):
            with self.subTest(change=change):
                self.supervisor_ready(change)

    @unittest.skipUnless(sys.platform == "darwin", "supervisor peer audit token")
    def test_supervisor_ready_authority_failure_closes_unclaimed_socket(self):
        for error in (PermissionError("identity revoked"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.supervisor_ready(authority_error=error)

    @unittest.skipUnless(sys.platform == "darwin", "supervisor peer audit token")
    def test_supervisor_exact_ready_delivers_usable_socket(self):
        self.supervisor_ready()

    def supervisor_owner_confirmation(self, malformed_kind=None):
        from mioa_core.profile_service import _receive, _send
        client, host = socket.socketpair()
        host.settimeout(3)
        supervisor = _Supervisor(client, endpoints=LocalUnixEndpoints(),
            blocking_calls=LocalBlockingCalls(), messages=LocalUnixMessages(),
            profile_command=profile_owner_command(), identity=LocalProcessIdentity(),
            managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        supervisor.profile_request_id = self.control_id()
        for _ in range(4):
            self.control_id()
        pairs = [socket.socketpair() for _ in range(2)]
        errors, confirmed = [], []

        def serve():
            try:
                self.assertEqual(_receive(host), {"op": "spawn", "domain": 0})
                for transfer_id, (kind, pair) in enumerate(zip(("cookie", "idb"), pairs), 1):
                    launch_id = transfer_id
                    payload = {"op": "enroll_owner", "kind": kind,
                               "launch_id": launch_id, "pid": os.getpid()}
                    data = json.dumps({"fd_transfer_id": transfer_id, "payload": payload}).encode()
                    packet = struct.pack("!I", len(data)) + data
                    sent = host.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                  struct.pack("=i", pair[1].fileno()))])
                    host.sendall(packet[sent:])
                    self.assertEqual(_receive(host),
                                     {"op": "fd_received", "fd_transfer_id": transfer_id})
                    pair[1].close()
                    self.assertEqual(_receive(host),
                                     {"op": "enrolled_owner", "kind": kind, "launch_id": launch_id})
                    _send(pair[0], {"ready": True})
                    identity = (True if kind == "cookie" else float(launch_id)) if kind == malformed_kind else launch_id
                    _send(host, {"op": "owner_started", "kind": kind, "launch_id": identity})
                    try:
                        reply = _receive(host)
                    except EOFError:
                        if kind == malformed_kind:
                            return
                        raise
                    self.assertEqual(reply, {"op": "owner_ready", "kind": kind, "launch_id": launch_id})
                    confirmed.append(kind)
                _send(host, {"op": "spawn_rejected", "code": "busy", "domain": 0})
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve)
        worker.start()
        try:
            with self.assertRaises(NativeError) as rejected:
                asyncio.run(supervisor.spawn({"op": "spawn", "domain": 0}, self.control))
            self.assertEqual(rejected.exception.code, "protocol_error" if malformed_kind else "busy")
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            if malformed_kind:
                self.assertEqual(confirmed, [] if malformed_kind == "cookie" else ["cookie"])
                self.assertFalse(supervisor.owners_enrolled)
                self.assertEqual(client.fileno(), -1)
            else:
                self.assertEqual(confirmed, ["cookie", "idb"])
                self.assertTrue(supervisor.owners_enrolled)
                self.assertGreaterEqual(client.fileno(), 0)
            self.assertEqual(self.a.request("session.ping"), {"protocol": 1})
            self.assertEqual(self.b.request("session.ping"), {"protocol": 1})
        finally:
            client.close()
            host.close()
            for pair in pairs:
                for endpoint in pair:
                    endpoint.close()
            worker.join(timeout=5)

    @unittest.skipUnless(sys.platform == "darwin", "supervisor peer audit token")
    def test_supervisor_cookie_boolean_identity_cannot_confirm_ready(self):
        self.supervisor_owner_confirmation("cookie")

    @unittest.skipUnless(sys.platform == "darwin", "supervisor peer audit token")
    def test_supervisor_idb_float_identity_cannot_confirm_ready(self):
        self.supervisor_owner_confirmation("idb")

    @unittest.skipUnless(sys.platform == "darwin", "supervisor peer audit token")
    def test_supervisor_exact_owner_identities_confirm_both_ready(self):
        self.supervisor_owner_confirmation()

    def test_owner_enrollment_requires_real_ready_frame(self):
        from mioa_core.profile_service import _send
        host, owner = socket.socketpair()
        try:
            enroll_owner(self.control, owner, self.control_id(), "cookie", messages=LocalUnixMessages())
            owner.close()
            _send(host, {"ready": True})
            await_owner_ready(self.control, self.control_id(), "cookie")
        finally:
            host.close()
            owner.close()

    def test_separate_event_endpoint_receives_ordered_owner_event(self):
        from mioa_core.profile_service import _receive, _send
        host, host_peer = socket.socketpair()
        runtime, runtime_peer = socket.socketpair()
        events, events_peer = socket.socketpair()
        events.settimeout(5)
        try:
            enroll_owner(self.control, host_peer, self.control_id(), "cookie", messages=LocalUnixMessages())
            host_peer.close()
            _send(host, {"ready": True})
            await_owner_ready(self.control, self.control_id(), "cookie")
            enroll_domain(self.control, runtime_peer, self.control_id(), events_peer, messages=LocalUnixMessages())
            runtime_peer.close()
            events_peer.close()
            client = ProfileClient(runtime)
            self.assertEqual(client.request("events.subscribe"), {"next_sequence": 1})

            def respond():
                request = _receive(host)
                _send(host, {"id": request["id"], "ok": True, "result": None,
                             "events": [{"kind": "changed", "name": "n", "value": "v",
                                         "domain": "example.test", "path": "/", "secure": False,
                                         "http_only": False, "same_site": "lax"}]})

            worker = threading.Thread(target=respond)
            worker.start()
            self.a.request("cookies.set", url="https://example.test", header="n=v", source="http")
            event = _receive(events)
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual((event["sequence"], event["type"], event["name"]), (1, "cookie", "n"))
            client.close()
        finally:
            for endpoint in (host, host_peer, runtime, runtime_peer, events, events_peer):
                endpoint.close()

    def test_cookie_owner_failure_does_not_break_other_domain(self):
        with self.assertRaises(ProfileServiceError):
            self.a.request("cookies.snapshot")
        self.assertEqual(self.b.local_snapshot("https://example.test"), [])

    def enroll_native_cookie_owner(self):
        """Run the actual rebuilt native owner, never an RPC forwarding mock."""
        binary = os.environ["MIOA_COOKIE_TEST_BINARY"]
        host, child = socket.socketpair()
        env = {**os.environ, "MIOA_COOKIE_OWNER_FD": str(child.fileno()),
               "MIOA_COOKIE_OWNER_DB": str(profile.profile_dir("test") / "data" / "cookies.sqlite")}
        try:
            owner = subprocess.Popen([binary], env=env, pass_fds=(child.fileno(),),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.cookie_processes.append(owner)
            child.close()
            enroll_owner(self.control, host, self.control_id(), "cookie", messages=LocalUnixMessages())
            await_owner_ready(self.control, self.control_id(), "cookie")
        finally:
            host.close()
            child.close()
        return owner

    @unittest.skipUnless(os.environ.get("MIOA_COOKIE_TEST_BINARY"), "requires rebuilt native cookie owner")
    def test_native_cookie_utf8_boundaries_and_shared_events(self):
        from mioa_core.profile_service import _receive, ProfileRequestRejected
        self.enroll_native_cookie_owner()
        events = self.auxiliary[2]  # Domain b's separate, real push event socket.
        events.settimeout(10)
        self.b.request("events.subscribe")
        url = "https://example.test/"
        names = ["a" * 4095, "é" * 2047 + "u", "😀" * 1023 + "utf", "value-max",
                 "b" * 4096, "界" * 1365 + "u", "😀" * 1024]
        for sequence, name in enumerate(names, 1):
            value = "v" * (4096 - len(name.encode()))
            self.a.request("cookies.store_set", url=url, site=url,
                           init={"name": name, "value": value}, is_delete=False)
            event = _receive(events)
            self.assertEqual((event["sequence"], event["name"], event["value"]),
                             (sequence, name, value))
            if not name.isascii():
                self.assertGreater(len(json.dumps(event).encode()), 8192)
            rows = self.b.request("cookies.store_get", url=url, site=url, name=name, first_only=True)
            self.assertEqual([(row["name"], row["value"]) for row in rows], [(name, value)])
            self.assertEqual(self.b.request("cookies.store_get", url="https://other.test/",
                                           site="https://other.test/", name=name, first_only=True), [])

        # Both overlong names and value growth of an existing cookie reject
        # before mutation. Unicode is measured in UTF-8 bytes, not JS characters.
        for name, value in [("a" * 4096, "v"), ("é" * 2048, "v"), (names[0], "vv")]:
            with self.assertRaises(ProfileRequestRejected):
                self.a.request("cookies.store_set", url=url, site=url,
                               init={"name": name, "value": value}, is_delete=False)
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name="a" * 4096, first_only=True), [])
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name="é" * 2048, first_only=True), [])
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name=names[0], first_only=True)[0]["value"], "v")
        for is_http in (True, False):
            self.assertFalse(self.a.request("cookies.set", url=url, origin_url=url,
                                           header="h" * 4096 + "=v; Path=/", is_http=is_http))
        self.a.request("cookies.store_set", url=url, site=url,
                       init={"name": "after-rejection", "value": "ok"}, is_delete=False)
        final = _receive(events)
        self.assertEqual((final["sequence"], final["name"]), (len(names) + 1, "after-rejection"))
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name="after-rejection", first_only=True)[0]["value"], "ok")

    @unittest.skipUnless(os.environ.get("MIOA_COOKIE_TEST_BINARY"), "requires rebuilt native cookie owner")
    def test_native_cookie_maximum_batch_events_are_independent_frames(self):
        from mioa_core.profile_service import ProfileRequestRejected
        self.enroll_native_cookie_owner()
        url = "https://example.test/"
        # Each request fits 1 MiB; clearing the full legal jar emits over 4 MiB
        # of events and must not poison the one shared native cookie owner.
        for start in range(0, 1024, 128):
            cookies = [{"name": f"bulk-{index:04d}", "value": "v" * 4087, "url": url}
                       for index in range(start, start + 128)]
            self.assertEqual(self.a.request("cookies.cdp_set", cookies=cookies), 128)
        self.a.request("cookies.clear")
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name=None, first_only=False), [])
        # Reject the batch before its first entry can create a cookie.
        with self.assertRaises(ProfileRequestRejected):
            self.a.request("cookies.cdp_set", cookies=[
                {"name": f"rejected-{index}", "value": "v", "url": url} for index in range(1025)])
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name=None, first_only=False), [])
        self.a.request("cookies.store_set", url=url, site=url,
                       init={"name": "survived", "value": "yes"}, is_delete=False)
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name="survived", first_only=True)[0]["value"], "yes")

    @unittest.skipUnless(os.environ.get("MIOA_COOKIE_TEST_BINARY"), "requires rebuilt native cookie owner")
    def test_native_cookie_unicode_bulk_subscription_survives_ascii_expansion(self):
        from mioa_core.profile_service import _receive
        self.enroll_native_cookie_owner()
        url = "https://example.test/"
        names = [f"{index:03d}" + "é" * 2046 for index in range(256)]
        for start in range(0, len(names), 32):
            self.assertEqual(self.a.request("cookies.cdp_set", cookies=[
                {"name": name, "value": "v", "url": url} for name in names[start:start + 32]]), 32)
        events = self.auxiliary[2]
        events.settimeout(10)
        self.b.request("events.subscribe")
        self.a.request("cookies.clear")
        # Exactly the existing per-domain queue budget, and >1 MiB both as
        # native UTF-8 aggregate events and after ensure_ascii expansion.
        total_utf8 = total_ascii = 0
        for sequence, name in enumerate(names, 1):
            event = _receive(events)
            self.assertEqual((event["sequence"], event["kind"], event["name"]),
                             (sequence, "deleted", name))
            total_utf8 += len(json.dumps(event, ensure_ascii=False).encode())
            total_ascii += len(json.dumps(event, ensure_ascii=True).encode())
        self.assertGreater(total_utf8, 1024 * 1024)
        self.assertGreater(total_ascii, 1024 * 1024)
        self.a.request("cookies.store_set", url=url, site=url,
                       init={"name": "after-bulk", "value": "ok"}, is_delete=False)
        self.assertEqual(_receive(events)["sequence"], 257)
        self.assertEqual(self.b.request("cookies.store_get", url=url, site=url,
                                       name="after-bulk", first_only=True)[0]["value"], "ok")

    @unittest.skipUnless(os.environ.get("MIOA_COOKIE_TEST_BINARY"), "requires rebuilt native cookie owner")
    def test_native_cookie_real_owner_loss_is_sticky_without_replay(self):
        owner = self.enroll_native_cookie_owner()
        url = "https://example.test/"
        self.a.request("cookies.store_set", url=url, site=url,
                       init={"name": "before-loss", "value": "known"}, is_delete=False)
        owner.terminate()
        owner.communicate(timeout=10)
        self.cookie_processes.remove(owner)  # Deliberately reaped, not a clean-EOF owner.
        for client in (self.a, self.b):
            with self.assertRaises(ProfileServiceError):
                client.request("cookies.store_set", url=url, site=url,
                               init={"name": "must-not-replay", "value": "unknown"}, is_delete=False)
        with sqlite3.connect(profile.profile_dir("test") / "data" / "cookies.sqlite") as db:
            self.assertEqual(db.execute("select name,value from cookies").fetchall(),
                             [("before-loss", "known")])
        self.a.local_mutation(url, "set", "profile-still-alive", "yes")
        self.assertEqual(self.b.local_snapshot(url), [["profile-still-alive", "yes"]])

    def test_control_eof_drains_domains_before_releasing_lease(self):
        self.control.close()
        with self.assertRaises(ProfileServiceError):
            self.a.local_snapshot("https://example.test")
        with profile.exclusive("test", leases=LocalProfileLeases()) as acquired:
            self.assertFalse(acquired)
        self.a.close()
        self.b.close()
        self.close_auxiliary()
        self.process.wait(timeout=10)
        # Closing the service does not unlock a shared flock description held
        # by the supervisor's escrow copy.
        with profile.exclusive("test", leases=LocalProfileLeases()) as acquired:
            self.assertFalse(acquired)
        for lease in self.escrow_leases:
            lease.close()
        self.escrow_leases.clear()
        with profile.exclusive("test", leases=LocalProfileLeases()) as acquired:
            self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
