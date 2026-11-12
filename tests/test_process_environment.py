"""Actual child inheritance and startup-failure redaction; no browser qualification."""
import asyncio
import json
import os
from pathlib import Path
import socket
import signal
import struct
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mioa_core import profile
from mioa_core.engine import Engine
from mioa_core.native import NativeError, NativeKeyboard, _Supervisor, _WebSocket, adopt_supervised_native, launch_native, start_supervised_profile
from mioa_core.native_spec import create_native_spec, resolve_proxy
from mioa_os import LocalArtifactContent, LocalBlockingCalls, LocalChildProcesses, LocalHostFiles, LocalManagedPeerIdentity, LocalProcessEnvironment, LocalProcessIdentity, LocalProfileLeases, LocalProxySecrets, LocalSecureRandom, LocalStreamConnections, LocalSupervisorChannel, LocalUnixEndpoints, LocalUnixMessages, profile_owner_command
from mioa_os_api import ProxySecretError


class ProcessEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_rejects_malformed_random_before_writing(self):
        random = SimpleNamespace(bytes=lambda count: b"\x00")
        left, right = socket.socketpair()
        right.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=left)
        ws = _WebSocket(reader, writer, random)
        try:
            with self.assertRaises(NativeError) as rejected:
                await ws.send(b"hello")
            self.assertEqual(rejected.exception.code, "capability_unavailable")
            with self.assertRaises(BlockingIOError):
                right.recv(1)
        finally:
            writer.close()
            await writer.wait_closed()
            right.close()

        left, right = socket.socketpair()
        right.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=left)
        try:
            with self.assertRaises(NativeError) as rejected:
                await _WebSocket._upgrade(reader, writer, "/", "localhost", random)
            self.assertEqual(rejected.exception.code, "capability_unavailable")
            self.assertEqual(right.recv(1), b"")
        finally:
            writer.close()
            await writer.wait_closed()
            right.close()

    def test_proxy_lookup_requires_host_authority_and_preserves_fallback(self):
        cfg = {"id": "proxy-fixture", "network": {}}
        key = profile._proxy_ref(cfg)[4:]
        cfg["network"]["proxy_ref"] = "env:" + key
        with tempfile.TemporaryDirectory() as temporary:
            authority = LocalProxySecrets(Path(temporary))
            authority.write(cfg["id"], "http://127.0.0.1:8081")
            with patch.dict(os.environ, {key: "http://user:pass@127.0.0.1:8080"}), \
                    patch.object(profile, "read_proxy_secret", side_effect=AssertionError("Core callback")):
                with self.assertRaises(NativeError) as missing:
                    resolve_proxy(cfg, proxy_secrets=None)
                self.assertEqual(missing.exception.code, "capability_unavailable")
                self.assertEqual(resolve_proxy(cfg, proxy_secrets=authority), os.environ[key])
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_proxy(cfg, proxy_secrets=authority), "http://127.0.0.1:8081")
            self.assertIsNone(resolve_proxy({"network": {"proxy_ref": None}}, proxy_secrets=None))

    def test_proxy_lookup_propagates_rejection_and_rejects_bad_values(self):
        cfg = {"network": {"proxy_ref": "env:MIOA_TEST_PROXY_LOOKUP"}}
        class Reject:
            def resolve(self, name, identity):
                raise PermissionError("denied")
        with self.assertRaises(PermissionError):
            resolve_proxy(cfg, proxy_secrets=Reject())
        with patch.dict(os.environ, {"MIOA_TEST_PROXY_LOOKUP": ""}):
            with self.assertRaises(NativeError) as missing:
                resolve_proxy(cfg, proxy_secrets=LocalProxySecrets(Path(tempfile.gettempdir())))
            self.assertEqual(missing.exception.code, "network_unavailable")
        with patch.dict(os.environ, {"MIOA_TEST_PROXY_LOOKUP": "not-a-proxy"}):
            with self.assertRaises(NativeError) as invalid:
                resolve_proxy(cfg, proxy_secrets=LocalProxySecrets(Path(tempfile.gettempdir())))
            self.assertEqual(invalid.exception.code, "invalid_config")

    def test_proxy_management_requires_authority_for_references(self):
        cfg = {"id": "fixture", "network": {"proxy_ref": None}}
        with patch.dict(os.environ, {"MIOA_TEST_MANAGED_REF": "http://127.0.0.1:8080"}):
            with self.assertRaises(NativeError) as missing:
                profile.configure_proxy(cfg, "env:MIOA_TEST_MANAGED_REF")
            self.assertEqual(missing.exception.code, "capability_unavailable")
            self.assertIsNone(cfg["network"]["proxy_ref"])
            profile.configure_proxy(cfg, "env:MIOA_TEST_MANAGED_REF", proxy_secrets=LocalProxySecrets(Path(tempfile.gettempdir())))
            self.assertEqual(cfg["network"]["proxy_ref"], "env:MIOA_TEST_MANAGED_REF")

    def test_proxy_archive_requires_authority_only_for_credentials(self):
        cfg = {"schema": 3, "name": "sample", "id": "fixture",
               "network": {"proxy_ref": "env:MIOA_TEST_ARCHIVE_PROXY"}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "profile"
            directory.mkdir()
            archive = root / "profile.tar.gz"
            with patch.dict(os.environ, {"MIOA_TEST_ARCHIVE_PROXY": "http://127.0.0.1:8080"}):
                profile._archive(directory, cfg, archive)
                with self.assertRaises(NativeError) as missing:
                    profile._archive(directory, cfg, archive, include_credentials=True)
                self.assertEqual(missing.exception.code, "capability_unavailable")
                profile._archive(directory, cfg, archive, include_credentials=True,
                                 proxy_secrets=LocalProxySecrets(root))
                with tarfile.open(archive, "r:gz") as packed:
                    credential = json.load(packed.extractfile("sample/.proxy-credentials.json"))
                self.assertEqual(credential, {"id": "fixture", "proxy": "http://127.0.0.1:8080"})

    def test_proxy_store_bound_root_and_snapshot_ignores_environment(self):
        cfg = {"schema": 3, "name": "sample", "id": "fixture", "network": {"proxy_ref": None}}
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
            root = Path(temporary)
            authority = LocalProxySecrets(root)
            profile.configure_proxy(cfg, "http://127.0.0.1:8081", proxy_secrets=authority)
            self.assertIsNone(LocalProxySecrets(other).read(cfg["id"]))
            key = cfg["network"]["proxy_ref"][4:]
            directory = root / "profile"
            directory.mkdir()
            (directory / "config.json").write_text(json.dumps(cfg))
            with patch.dict(os.environ, {key: "http://127.0.0.1:8082"}):
                archive = profile._archive(directory, cfg, root / "snapshot.tar.gz",
                                           include_credentials=True, bundle_fonts=False,
                                           snapshot_identity=cfg["id"], proxy_secrets=authority)
                with tarfile.open(archive, "r:gz") as packed:
                    credential = json.load(packed.extractfile("sample/.proxy-credentials.json"))
                self.assertEqual(credential["proxy"], "http://127.0.0.1:8081")
            profile.configure_proxy(cfg, "DIRECT", proxy_secrets=authority)
            self.assertIsNone(authority.read(cfg["id"]))
            profile.configure_proxy(cfg, "DIRECT")

    def test_proxy_store_rejects_unsafe_files_and_direct_needs_no_authority(self):
        cfg = {"id": "fixture", "network": {"proxy_ref": None}}
        profile.configure_proxy(cfg, "DIRECT")
        with tempfile.TemporaryDirectory() as temporary:
            authority = LocalProxySecrets(Path(temporary))
            profile.configure_proxy(cfg, "http://127.0.0.1:8081", proxy_secrets=authority)
            with self.assertRaises(profile.ProfileError):
                profile.configure_proxy(cfg, "DIRECT")
            path = Path(temporary) / ".secrets" / "proxies" / (cfg["network"]["proxy_ref"][4:] + ".json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            with self.assertRaises(ProxySecretError) as unsafe:
                authority.read(cfg["id"])
            self.assertEqual(unsafe.exception.code, "unsafe_secret_store")
            path.unlink()
            path.symlink_to("/dev/null")
            with self.assertRaises(ProxySecretError):
                authority.read(cfg["id"])

    def test_proxy_store_directory_sync_failure_does_not_publish(self):
        for failed_directory in ("root", "secrets"):
            with self.subTest(directory=failed_directory), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                authority = LocalProxySecrets(root)
                target = root if failed_directory == "root" else root / ".secrets"
                sync = authority._sync

                def failing_sync(directory):
                    if directory == target:
                        raise OSError("directory sync failed")
                    sync(directory)

                with patch.object(authority, "_sync", side_effect=failing_sync):
                    with self.assertRaisesRegex(OSError, "directory sync failed"):
                        authority.write("fixture", "http://127.0.0.1:8081")
                self.assertIsNone(authority.read("fixture"))
                authority.write("fixture", "http://127.0.0.1:8081")
                self.assertEqual(authority.read("fixture"), "http://127.0.0.1:8081")

    def test_proxy_delete_retry_requires_directory_sync(self):
        with tempfile.TemporaryDirectory() as temporary:
            authority = LocalProxySecrets(temporary)
            authority.delete("fixture")
            authority.write("fixture", "http://127.0.0.1:8081")
            with patch.object(authority, "_sync", side_effect=OSError("directory sync failed")):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    authority.delete("fixture")
                self.assertIsNone(authority.read("fixture"))
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    authority.delete("fixture")
            authority.delete("fixture")
            self.assertIsNone(authority.read("fixture"))

    async def test_proxy_store_rejects_fifo_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            authority = LocalProxySecrets(temporary)
            authority.write("fixture", "http://127.0.0.1:8081")
            path = authority._path("fixture")
            path.unlink()
            os.mkfifo(path, 0o600)
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c",
                "import sys\n"
                "from mioa_os import LocalProxySecrets\n"
                "from mioa_os_api import ProxySecretError\n"
                "try:\n"
                "    LocalProxySecrets(sys.argv[1]).read('fixture')\n"
                "except ProxySecretError as exc:\n"
                "    sys.exit(0 if exc.code == 'unsafe_secret_store' else 2)\n"
                "sys.exit(3)\n",
                temporary,
            )
            try:
                self.assertEqual(await asyncio.wait_for(child.wait(), 3), 0)
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()




    async def test_child_exec_snapshots_explicit_environment_and_closes_unrelated_descriptors(self):
        entered, release = asyncio.Event(), asyncio.Event()
        real_create = asyncio.create_subprocess_exec

        async def gated_create(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_create(*args, **kwargs)

        with tempfile.TemporaryFile() as unrelated:
            os.set_inheritable(unrelated.fileno(), True)
            identity = os.fstat(unrelated.fileno())
            code = (
                "import json,os,sys\n"
                "other,device,inode=map(int,sys.argv[1:])\n"
                "try:\n"
                " info=os.fstat(other)\n"
                " leaked=(info.st_dev,info.st_ino)==(device,inode)\n"
                "except OSError:\n"
                " leaked=False\n"
                "print(json.dumps({'environment':dict(os.environ),'leaked':leaked}))\n"
                "sys.exit(23)\n"
            )
            argv = [sys.executable, "-c", code, str(unrelated.fileno()),
                    str(identity.st_dev), str(identity.st_ino)]
            environment = {"MIOA_EXPLICIT": "snapshot", "LC_ALL": "C"}
            child = None
            with patch.dict(os.environ, {"MIOA_AMBIENT_ONLY": "must-not-leak"}), \
                    patch("mioa_os.processes.asyncio.create_subprocess_exec", gated_create):
                pending = asyncio.create_task(LocalChildProcesses().start(
                    argv, environment=environment))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    argv[:] = ["/not/the/snapshotted/executable"]
                    environment["MIOA_EXPLICIT"] = "mutated"
                    release.set()
                    child = await asyncio.wait_for(pending, 5)
                    stdout, stderr, status = await asyncio.wait_for(asyncio.gather(
                        child.stdout.read(), child.stderr.read(), child.wait()), 5)
                    observed = json.loads(stdout)
                    self.assertEqual(status, 23)
                    self.assertEqual(child.returncode, 23)
                    self.assertEqual(stderr, b"")
                    self.assertEqual(observed["environment"]["MIOA_EXPLICIT"], "snapshot")
                    self.assertNotIn("MIOA_AMBIENT_ONLY", observed["environment"])
                    self.assertFalse(observed["leaked"])
                    self.assertTrue(os.get_inheritable(unrelated.fileno()))
                finally:
                    release.set()
                    if not pending.done():
                        pending.cancel()
                    results = await asyncio.gather(pending, return_exceptions=True)
                    if child is None and not isinstance(results[0], BaseException):
                        child = results[0]
                    if child is not None and child.returncode is None:
                        child.kill()
                        await asyncio.gather(child.stdout.read(), child.stderr.read(), child.wait())

    async def test_child_streams_parent_eof_and_cancelled_wait_preserve_live_child(self):
        child = await LocalChildProcesses().start(
            [sys.executable, "-c",
             "import os,select,signal\n"
             "def report(*args):\n"
             " os.write(1,b'CLOSED\\n' if select.select([0],[],[],0)[0] else b'OPEN\\n')\n"
             "signal.signal(signal.SIGUSR1,report)\n"
             "os.write(2,('ERR %d\\n' % os.getpid()).encode())\n"
             "report()\n"
             "while True: signal.pause()\n"],
            environment={})
        try:
            self.assertEqual(await asyncio.wait_for(child.stdout.read(5), 5), b"OPEN\n")
            child_pid = int((await asyncio.wait_for(child.stderr.readline(), 5)).removeprefix(b"ERR "))
            self.assertFalse(hasattr(child, "pid"))
            waiter = asyncio.create_task(child.wait())
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertIsNone(child.returncode)
            os.kill(child_pid, 0)
            os.kill(child_pid, signal.SIGUSR1)
            self.assertEqual(await asyncio.wait_for(child.stdout.read(5), 5), b"OPEN\n")
            child.terminate()
            stdout, stderr, status = await asyncio.wait_for(asyncio.gather(
                child.stdout.read(), child.stderr.read(), child.wait()), 5)
            self.assertEqual((stdout, stderr), (b"", b""))
            self.assertEqual(status, -signal.SIGTERM)
            self.assertEqual(child.returncode, status)
            self.assertEqual(await child.wait(), status)
        finally:
            if child.returncode is None:
                child.kill()
            await asyncio.gather(child.stdout.read(), child.stderr.read(), child.wait())

    async def test_child_exec_failure_preserves_os_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            denied = Path(temporary) / "not-executable"
            denied.write_text("not executable")
            denied.chmod(0o600)
            for executable, failure in ((missing, FileNotFoundError), (denied, PermissionError)):
                with self.subTest(executable=executable):
                    with self.assertRaises(failure) as rejected:
                        await LocalChildProcesses().start([str(executable)], environment={})
                    self.assertEqual(rejected.exception.filename, str(executable))

    async def test_cancelled_start_reaps_real_flooding_child_before_repeated_cancel_returns(self):
        created, deliver = asyncio.Event(), asyncio.Event()
        real_create = asyncio.create_subprocess_exec
        processes = []

        async def delayed_delivery(*args, **kwargs):
            process = await real_create(*args, **kwargs)
            processes.append(process)
            created.set()
            await deliver.wait()
            return process

        async def wait_for_full_pipes(process):
            # Observe real reader backpressure, not elapsed time: without
            # concurrent drainage, asyncio's exit wait can remain stuck here.
            while not (len(process.stdout._buffer) > 2 * process.stdout._limit
                       and len(process.stderr._buffer) > 2 * process.stderr._limit):
                if process.returncode is not None:
                    self.fail("flooding child exited before cancellation")
                await asyncio.sleep(0)

        async def cancel_repeatedly(pending):
            while not pending.done():
                pending.cancel()
                await asyncio.sleep(0)

        pending = repeater = None
        with patch("mioa_os.processes.asyncio.create_subprocess_exec", delayed_delivery):
            try:
                pending = asyncio.create_task(LocalChildProcesses().start(
                    [sys.executable, "-c",
                     "import os,signal,threading\n"
                     "def flood(fd):\n"
                     " while True: os.write(fd,b'x'*65536)\n"
                     "threading.Thread(target=flood,args=(1,),daemon=True).start()\n"
                     "threading.Thread(target=flood,args=(2,),daemon=True).start()\n"
                     "signal.pause()\n"],
                    environment={}))
                await asyncio.wait_for(created.wait(), 5)
                process = processes[0]
                await asyncio.wait_for(wait_for_full_pipes(process), 5)
                pending.cancel()
                await asyncio.sleep(0)
                pending.cancel()
                await asyncio.sleep(0)
                self.assertFalse(pending.done(), "cancellation escaped before creation settled")
                repeater = asyncio.create_task(cancel_repeatedly(pending))
                deliver.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 5)
                self.assertGreaterEqual(pending.cancelling(), 3)
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertTrue(process.stdout.at_eof())
                self.assertTrue(process.stderr.at_eof())
                self.assertTrue(process.stdin.is_closing())
                with self.assertRaises(ChildProcessError):
                    os.waitpid(process.pid, os.WNOHANG)
            finally:
                if repeater is not None:
                    repeater.cancel()
                    await asyncio.gather(repeater, return_exceptions=True)
                deliver.set()
                if pending is not None:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())

    async def test_injected_peer_verification_rejects_and_closes_owned_channels(self):
        class PeerIdentity:
            def __init__(self, result):
                self.result = result
                self.calls = []

            def verify_supervisor(self, channel):
                self.calls.append((channel, None))
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

            def verify_child(self, channel, peer):
                self.calls.append((channel, peer))
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        for result in (False, OSError("peer unavailable")):
            for cdp in (False, True):
                with self.subTest(result=result, cdp=cdp):
                    client, host = socket.socketpair()
                    authority = PeerIdentity(result)
                    peer = object()
                    try:
                        with self.assertRaises(NativeError if result is False else OSError) as error:
                            if cdp:
                                await _WebSocket.connect_owned_unix(
                                    client, peer, authority, LocalSecureRandom(),
                                    connections=LocalStreamConnections())
                            else:
                                _Supervisor(client, endpoints=LocalUnixEndpoints(),
                                            blocking_calls=LocalBlockingCalls(), messages=LocalUnixMessages(),
                                            profile_command=profile_owner_command(), managed_identity=authority,
                                            identity=LocalProcessIdentity(), leases=LocalProfileLeases())
                        if result is False:
                            self.assertEqual(error.exception.code, "verification_failed")
                        else:
                            self.assertIs(error.exception, result)
                        self.assertEqual(authority.calls, [(client, peer if cdp else None)])
                        self.assertEqual(host.recv(1), b"")
                    finally:
                        client.close()
                        host.close()

    async def test_missing_identity_closes_transferred_supervisor_channel(self):
        for identity, managed in ((None, LocalManagedPeerIdentity()), (LocalProcessIdentity(), None)):
            client, host = socket.socketpair()
            with client, host:
                host.setblocking(False)
                with self.assertRaises(NativeError) as rejected:
                    _Supervisor(client, endpoints=LocalUnixEndpoints(), blocking_calls=LocalBlockingCalls(),
                                messages=LocalUnixMessages(), profile_command=profile_owner_command(),
                                identity=identity, managed_identity=managed, leases=LocalProfileLeases())
                self.assertEqual(rejected.exception.code, "capability_unavailable")
                self.assertEqual(host.recv(1), b"")

    async def test_identity_provider_failure_closes_transferred_supervisor_channel(self):
        class UnavailableIdentity(LocalManagedPeerIdentity):
            def verify_supervisor(self, channel):
                raise failure

        for failure in (PermissionError("identity denied"), asyncio.CancelledError()):
            with self.subTest(error=type(failure).__name__):
                client, host = socket.socketpair()
                with client, host:
                    host.setblocking(False)
                    with self.assertRaises(type(failure)) as rejected:
                        _Supervisor(client, endpoints=LocalUnixEndpoints(), blocking_calls=LocalBlockingCalls(),
                                    messages=LocalUnixMessages(), profile_command=profile_owner_command(),
                                    identity=LocalProcessIdentity(), managed_identity=UnavailableIdentity(), leases=LocalProfileLeases())
                    self.assertIs(rejected.exception, failure)
                    self.assertEqual(host.recv(1), b"")

    async def test_supervisor_channel_is_consumed_once_and_closed_on_handshake_failure(self):
        client, host = socket.socketpair()
        host.setblocking(False)
        service = LocalSupervisorChannel()
        pending = None
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"MIOA_SUPERVISOR_FD": str(client.detach())}):
            try:
                pending = asyncio.create_task(start_supervised_profile(
                    "test", Path(temporary), supervisor_channel=service,
                    endpoints=LocalUnixEndpoints(), blocking_calls=LocalBlockingCalls(),
                    messages=LocalUnixMessages(), profile_command=profile_owner_command(),
                    identity=LocalProcessIdentity(), managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases()))
                loop = asyncio.get_running_loop()

                async def receive_exact(count):
                    data = b""
                    while len(data) < count:
                        chunk = await asyncio.wait_for(loop.sock_recv(host, count - len(data)), 2)
                        if not chunk:
                            raise EOFError("supervisor closed early")
                        data += chunk
                    return data

                length, = struct.unpack("!I", await receive_exact(4))
                request = json.loads(await receive_exact(length))
                self.assertEqual(request["op"], "profile_start")
                self.assertNotIn("MIOA_SUPERVISOR_FD", os.environ)
                os.environ["MIOA_SUPERVISOR_FD"] = "replacement"
                with self.assertRaises(ValueError):
                    service.take()
                self.assertEqual(os.environ["MIOA_SUPERVISOR_FD"], "replacement")
                await loop.sock_sendall(host, struct.pack("!I", 2) + b"{}")
                with self.assertRaises(NativeError) as rejected:
                    await asyncio.wait_for(pending, 2)
                self.assertEqual(rejected.exception.code, "protocol_error")
                self.assertEqual(await asyncio.wait_for(loop.sock_recv(host, 1), 2), b"")
            finally:
                host.close()
                if pending is not None:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    async def test_missing_supervisor_channel_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(NativeError) as rejected:
                await start_supervised_profile(
                    "test", Path("/tmp"), supervisor_channel=LocalSupervisorChannel(),
                    endpoints=LocalUnixEndpoints(), blocking_calls=LocalBlockingCalls(),
                    messages=LocalUnixMessages(), profile_command=profile_owner_command(),
                    identity=LocalProcessIdentity(), managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
            self.assertEqual(rejected.exception.code, "verification_failed")

    async def test_invalid_supervisor_descriptor_is_closed(self):
        reader, writer = os.pipe()
        try:
            with patch.dict(os.environ, {"MIOA_SUPERVISOR_FD": str(reader)}):
                with self.assertRaises(NativeError) as rejected:
                    await start_supervised_profile(
                        "test", Path("/tmp"), supervisor_channel=LocalSupervisorChannel(),
                        endpoints=LocalUnixEndpoints(), blocking_calls=LocalBlockingCalls(),
                        messages=LocalUnixMessages(), profile_command=profile_owner_command(), leases=LocalProfileLeases())
                self.assertEqual(rejected.exception.code, "verification_failed")
                with self.assertRaises(BrokenPipeError):
                    os.write(writer, b"x")
        finally:
            os.close(writer)


    async def test_supervised_proxy_rejected_without_resolving_credentials(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(profile, "BASE", Path(temporary)):
            home = Path(temporary)
            binary = home / "runtime"
            binary.write_bytes(b"runtime fixture")
            binary.chmod(0o700)
            font = home / "font"
            font.write_bytes(b"font fixture")
            cfg = create_native_spec("managed-proxy", runtime="mioa", binary=binary,
                storage_path=profile.profile_dir("managed-proxy") / "data", seed="ab" * 32,
                asset_paths={"fonts": [str(font)]}, network={"proxy_ref": "env:MIOA_TEST_MANAGED_PROXY"},
                artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
            supervisor = SimpleNamespace(application_domains=(0,), _reserved_domains=set())
            for environment in ({}, {"MIOA_TEST_MANAGED_PROXY": "not-a-proxy"}):
                with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                        patch.object(profile, "read_proxy_secret", side_effect=AssertionError("secret read")):
                    with self.assertRaises(NativeError) as rejected:
                        await adopt_supervised_native("managed-proxy", cfg, supervisor, 1, None,
                                                      random=LocalSecureRandom(), artifact_content=LocalArtifactContent(),
                                                      connections=LocalStreamConnections(), files=LocalHostFiles())
                    self.assertEqual(rejected.exception.code, "capability_unavailable")
                    self.assertFalse(profile.profile_dir("managed-proxy").exists())

    async def test_child_gets_independent_snapshot_with_remove_then_overlay(self):
        service = LocalProcessEnvironment()
        with patch.dict(os.environ, {"KEEP": "original", "REMOVE": "private", "OVERRIDE": "ambient"}, clear=True):
            overlay = {"OVERRIDE": "explicit", "ADDED": "child"}
            environment = service.derive(remove=("REMOVE", "OVERRIDE", "ABSENT"), overlay=overlay)
            sibling = service.derive(remove=(), overlay={})
            os.environ["KEEP"] = "rotated"
            environment["ONLY_THIS_CHILD"] = "yes"
            process = await LocalChildProcesses().start(
                [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
                environment=environment,
            )
            try:
                stdout, stderr, _ = await asyncio.wait_for(asyncio.gather(
                    process.stdout.read(), process.stderr.read(), process.wait()), 5)
                self.assertEqual(stderr, b"")
                self.assertEqual(process.returncode, 0)
                child = json.loads(stdout)
                self.assertEqual({key: child[key] for key in environment}, environment)
                self.assertNotIn("REMOVE", child)
                self.assertEqual(child["KEEP"], "original")
                self.assertEqual(child["OVERRIDE"], "explicit")
                self.assertEqual(child["ADDED"], "child")
                self.assertNotIn("ONLY_THIS_CHILD", sibling)
                self.assertEqual(sibling["REMOVE"], "private")
                self.assertEqual(dict(os.environ), {"KEEP": "rotated", "REMOVE": "private", "OVERRIDE": "ambient"})
                self.assertEqual(overlay, {"OVERRIDE": "explicit", "ADDED": "child"})
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_missing_host_service_rejects_before_profile_or_child_creation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(profile, "BASE", Path(temporary)):
            engine = Engine("no-environment", headless=True, initial_url=None, verify=False,
                            child_processes=LocalChildProcesses())
            with self.assertRaises(NativeError) as rejected:
                await engine._launch({"runtime": {"id": "mioa"}})
            self.assertEqual(rejected.exception.code, "capability_unavailable")
            self.assertIsNone(engine.native)
            self.assertFalse(engine.workdir.exists())

    async def test_missing_child_process_authority_rejects_before_config_or_profile_creation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(profile, "BASE", Path(temporary)):
            engine = Engine("no-processes", headless=True, initial_url=None, verify=False,
                            process_environment=LocalProcessEnvironment(),
                            process_identity=LocalProcessIdentity())
            with patch.object(profile, "save_config", side_effect=AssertionError("config creation")):
                with self.assertRaises(NativeError) as rejected:
                    await engine._launch({"runtime": {"id": "mioa"}})
            self.assertEqual(rejected.exception.code, "capability_unavailable")
            self.assertIsNone(engine.native)
            self.assertFalse(engine.workdir.exists())

    async def test_launch_rejects_unowned_profile_before_creating_data(self):
        for kind in ("missing", "foreign", "closed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(profile, "BASE", Path(temporary)):
                home = Path(temporary)
                font = home / "fixture.ttf"
                font.write_bytes(b"font fixture")
                data = profile.profile_dir("launch-lease") / "data"
                cfg = create_native_spec("launch-lease", runtime="mioa", binary=sys.executable,
                    storage_path=data, asset_paths={"fonts": [str(font)]},
                    artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
                profile.save_config(cfg["name"], cfg)
                with profile.exclusive("other", leases=LocalProfileLeases()) as foreign:
                    self.assertIsNotNone(foreign)
                    with profile.exclusive(cfg["name"], leases=LocalProfileLeases()) as closed:
                        self.assertIsNotNone(closed)
                    lease = {"missing": None, "foreign": foreign, "closed": closed}[kind]
                    with self.assertRaises(NativeError) as rejected:
                        await launch_native(cfg["name"], cfg, True,
                            process_environment=LocalProcessEnvironment(),
                            child_processes=LocalChildProcesses(),
                            identity=LocalProcessIdentity(), profile_lease=lease,
                            random=LocalSecureRandom(), artifact_content=LocalArtifactContent(),
                            connections=LocalStreamConnections(), files=LocalHostFiles())
                    self.assertEqual(rejected.exception.code, "busy")
                    self.assertFalse(data.exists(), "unowned launch created Profile data")

    async def test_launch_filters_ambient_authority_and_redacts_original_secret_on_child_failure(self):
        key = "MIOA_TEST_START_PROXY"
        launched = "http://started%2Duser:started%2Dpassword@127.0.0.1:8123"
        reserved = (
            "OBSCURA_ENVIRONMENT", "OBSCURA_ENVIRONMENT_GL_BACKEND", "OBSCURA_PROXY",
            "LIGHTPANDA_ENVIRONMENT", "LIGHTPANDA_HTTP_PROXY", "MIOA_ENVIRONMENT", "MIOA_HTTP_PROXY",
            "MIOA_COOKIE_OWNER_FD", "MIOA_COOKIE_OWNER_DB", "MIOA_PROFILE_SERVICE_FD",
            "MIOA_PROFILE_EVENTS_FD", "MIOA_IDB_OWNER_FD", "MIOA_IDB_OWNER_DIR",
            "MIOA_TRUSTED_RASTER_HOST_PID", "MIOA_TRUSTED_RASTER_HOST_UID",
            "MIOA_TRUSTED_RASTER_HOST_VERSION", "MIOA_TRUSTED_RASTER_SOCKET", "MIOA_SUPERVISOR_FD",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        )
        for transition in ("direct", "rotate", "remove"):
            with self.subTest(transition=transition), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(profile, "BASE", Path(temporary)), \
                    patch.dict(os.environ, {**dict.fromkeys(reserved, "ambient-authority"), key: launched,
                                            "ANTI_TRACE_PARENT_EOF": "0"}):
                home = Path(temporary)
                observed = home / "observed.json"
                seed = "ab" * 32
                child = home / "failing-runtime"
                child.write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, sys\nfrom pathlib import Path\nfrom urllib.parse import unquote\n"
                    f"Path({str(observed)!r}).write_text(json.dumps(dict(os.environ)))\n"
                    "print('CHILD-STARTUP-FAILURE', file=sys.stderr)\n"
                    f"print(unquote(os.environ.get({key!r}, '')), file=sys.stderr)\n"
                    f"print(os.environ.get({key!r}, ''), file=sys.stderr)\n"
                    f"print({seed!r}, file=sys.stderr)\nsys.exit(17)\n"
                )
                child.chmod(0o700)
                asset = home / "font-fixture"
                asset.write_bytes(b"font hash fixture; not rendered")
                cfg = create_native_spec("launch-environment", runtime="obscura", binary=child,
                    storage_path=profile.profile_dir("launch-environment") / "data", seed=seed,
                    asset_paths={"fonts": [str(asset)]},
                    network={"proxy_ref": None if transition == "direct" else f"env:{key}"},
                    artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
                profile.save_config(cfg["name"], cfg)
                if transition == "direct":
                    os.environ.pop(key)

                class ChangingParent(LocalProcessEnvironment):
                    def derive(self, **kwargs):
                        environment = super().derive(**kwargs)
                        if transition == "rotate":
                            os.environ[key] = "http://replacement:rotated@127.0.0.1:8124"
                        elif transition == "remove":
                            os.environ.pop(key)
                        return environment

                with profile.exclusive(cfg["name"], leases=LocalProfileLeases()) as owned:
                    self.assertTrue(owned, "test engine lease unavailable")
                    with self.assertRaises(NativeError) as failed:
                        await launch_native(cfg["name"], cfg, True, process_environment=ChangingParent(),
                                            child_processes=LocalChildProcesses(), identity=LocalProcessIdentity(),
                                            profile_lease=owned, proxy_secrets=LocalProxySecrets(home),
                                            random=LocalSecureRandom(), artifact_content=LocalArtifactContent(),
                                            connections=LocalStreamConnections(), files=LocalHostFiles())
                self.assertEqual(failed.exception.code, "invalid_config")
                diagnostic = str(failed.exception)
                self.assertIn("CHILD-STARTUP-FAILURE", diagnostic)
                self.assertTrue(all(secret not in diagnostic for secret in (
                    launched, "started%2Duser", "started%2Dpassword", "started-user", "started-password", seed,
                )), "startup diagnostics disclosed a launched credential")
                environment = json.loads(observed.read_text())
                expected_absent = set(reserved) - ({"OBSCURA_PROXY"} if transition != "direct" else set())
                self.assertFalse(expected_absent.intersection(environment))
                self.assertEqual(environment["ANTI_TRACE_PARENT_EOF"], "1")
                self.assertEqual(os.environ["ANTI_TRACE_PARENT_EOF"], "0")
                self.assertTrue(all(os.environ[name] == "ambient-authority" for name in reserved))
                if transition == "direct":
                    self.assertNotIn(key, environment)
                else:
                    self.assertEqual(environment[key], launched)
                    self.assertEqual(environment["OBSCURA_PROXY"], launched)
                    self.assertIn("[private]", diagnostic)
    def test_process_identity_platform_and_monotonic_clock(self):
        identity = LocalProcessIdentity()
        self.assertIn(identity.platform, {"Darwin", "Linux", "Windows"})
        t1 = identity.monotonic_time()
        self.assertIsInstance(t1, float)
        self.assertGreaterEqual(identity.monotonic_time(), t1)

    def test_native_keyboard_cross_platform_shortcuts(self):
        page = SimpleNamespace(context=SimpleNamespace(session=None))
        darwin_kb = NativeKeyboard(page, platform="Darwin")
        key, code, virtual = darwin_kb._key("ControlOrMeta")
        self.assertEqual(key, "Meta")
        self.assertEqual(code, "MetaLeft")

        linux_kb = NativeKeyboard(page, platform="Linux")
        key, code, virtual = linux_kb._key("ControlOrMeta")
        self.assertEqual(key, "Control")
        self.assertEqual(code, "ControlLeft")


if __name__ == "__main__":
    unittest.main()
