"""Deterministic service-side event accounting (no native qualification)."""
import socket
import sqlite3
from unittest.mock import patch
import tempfile
from pathlib import Path
import unittest

from mioa_core.profile_service import _Storage, ProfileRequestRejected
from mioa_core.local_storage_stream import StreamStorage
from mioa_os import LocalSecureRandom
from mioa_os.profile_resource_grant import ResourceGrants
from mioa_core import native_spec, profile
from mioa_core.profile_service import _serve
from mioa_os import LocalArtifactContent, LocalHostFiles, LocalProfileLeases, LocalUnixChannels, LocalUnixMessages


class ServiceStartupTests(unittest.TestCase):
    def test_selector_failure_closes_adopted_grants(self):
        with tempfile.TemporaryDirectory() as root:
            previous = profile.BASE
            profile.BASE = Path(root)
            grant, peer = socket.socketpair()
            control, control_peer = socket.socketpair()
            grants = ResourceGrants()
            grants.bind(1, grant)
            peer.settimeout(0.2)
            try:
                binary = Path(root) / "runtime"
                binary.write_bytes(b"runtime")
                binary.chmod(0o700)
                font = Path(root) / "font"
                font.write_bytes(b"font")
                config = native_spec.create_native_spec(
                    "test", runtime="mioa", seed="ab" * 32, binary=binary,
                    storage_path=profile.profile_dir("test") / "data",
                    asset_paths={"fonts": [str(font)]}, artifact_content=LocalArtifactContent(),
                    random=LocalSecureRandom())
                profile.save_config("test", config)
                channels = LocalUnixChannels()
                with patch.object(LocalUnixChannels, "pair",
                                  side_effect=OSError("selector unavailable")):
                    with self.assertRaisesRegex(OSError, "selector unavailable"):
                        _serve(control, [], "test", Path(root), messages=LocalUnixMessages(),
                               leases=LocalProfileLeases(), random=LocalSecureRandom(),
                               grants=grants, artifact_content=LocalArtifactContent(),
                               channels=channels, files=LocalHostFiles(),
                               ready=lambda _: None)
                self.assertEqual(peer.recv(1), b"")
            finally:
                profile.BASE = previous
                grants.close()
                peer.close()
                control.close()
                control_peer.close()


class CookieEventsTests(unittest.TestCase):
    def test_error_reply_fans_out_and_overflow_is_sticky(self):
        class Owner:
            def request_reply(self, operation, payload):
                return {"ok": False, "error": "rejected", "events": [{
                    "kind": "changed", "name": "a", "value": "b", "domain": "example.test",
                    "path": "/", "secure": False, "http_only": False, "same_site": "lax"}]}

        with tempfile.TemporaryDirectory() as root:
            storage = _Storage(Path(root), "test", {}, random=LocalSecureRandom(), grants=ResourceGrants(), files=LocalHostFiles())
            storage.cookie_client = Owner()
            self.assertEqual(storage.dispatch("events.subscribe", {}, 1), {"next_sequence": 1})
            self.assertEqual(storage.dispatch("events.subscribe", {}, 2), {"next_sequence": 1})
            with self.assertRaises(ProfileRequestRejected):
                storage.dispatch("cookies.set", {}, 1)
            first = storage.dispatch("events.poll", {}, 1)
            self.assertEqual(first["events"][0]["sequence"], 1)
            self.assertFalse(first["lost"])
            for _ in range(256):
                with self.assertRaises(ProfileRequestRejected):
                    storage.dispatch("cookies.set", {}, 1)
            lost = storage.dispatch("events.poll", {}, 2)
            self.assertTrue(lost["lost"])
            self.assertEqual(lost["events"], [])
            with self.assertRaises(ProfileRequestRejected):
                storage.dispatch("cookies.snapshot", {}, 2)
            self.assertTrue(storage.dispatch("events.poll", {}, 2)["lost"])

    def test_idb_grants_are_principal_bound_and_share_sequence(self):
        class Owner:
            def request_reply(self, operation, payload):
                self.payload = payload
                return {"ok": True, "result": None, "events": [{
                    "type": "idb_gate", "connection_id": 2, "origin": "https://example.test",
                    "ticket": "7", "state": "granted"}]}

        with tempfile.TemporaryDirectory() as root:
            storage = _Storage(Path(root), "test", {}, random=LocalSecureRandom(), grants=ResourceGrants(), files=LocalHostFiles())
            owner = Owner()
            storage.idb_client = owner
            storage.dispatch("events.subscribe", {}, 1)
            storage.dispatch("events.subscribe", {}, 2)
            storage.dispatch("idb.cancel", {"origin": "https://example.test", "ticket": "6"}, 1)
            self.assertEqual(owner.payload["connection_id"], 1)
            self.assertEqual(storage.dispatch("events.poll", {}, 1)["events"], [])
            event = storage.dispatch("events.poll", {}, 2)["events"][0]
            self.assertEqual((event["sequence"], event["type"], event["ticket"]), (1, "idb_gate", "7"))
            self.assertNotIn("connection_id", event)
            with self.assertRaises(ProfileRequestRejected):
                storage.dispatch("idb.cancel", {"connection_id": 2}, 1)

    def test_random_failure_releases_storage_spool(self):
        class FailedRandom:
            def bytes(self, count):
                raise OSError("random unavailable")

        class Grants:
            def open_spool(self, principal):
                self.file = tempfile.TemporaryFile()
                return self

            def close(self):
                self.file.close()

        grants = Grants()
        storage = StreamStorage(None, grants, FailedRandom())
        with self.assertRaises(OSError):
            storage.dispatch("local.mutation.begin", {
                "origin": "https://example.test", "kind": "set",
                "key_length": 1, "value_length": 1,
            }, 1)
        self.assertTrue(grants.file.closed)
        self.assertEqual(storage.streams, {})


class StorageCloseTests(unittest.TestCase):
    def test_stream_failure_still_closes_grants_and_owner_endpoint(self):
        with tempfile.TemporaryDirectory() as root:
            grants = ResourceGrants()
            grant, grant_peer = socket.socketpair()
            endpoint, endpoint_peer = socket.socketpair()
            grant_peer.settimeout(0.2)
            endpoint_peer.settimeout(0.2)
            grants.bind(1, grant)
            storage = _Storage(Path(root), "test", {}, random=LocalSecureRandom(), grants=grants, files=LocalHostFiles())
            database = storage.local("https://example.test")
            storage.idb_endpoint = endpoint
            try:
                with patch.object(storage.stream, "close", side_effect=OSError("stream cleanup")):
                    with self.assertRaisesRegex(OSError, "stream cleanup"):
                        storage.close()
                with self.assertRaises(sqlite3.ProgrammingError):
                    database.execute("select 1")
                self.assertEqual(grant_peer.recv(1), b"")
                self.assertEqual(endpoint_peer.recv(1), b"")
            finally:
                grant.close()
                grant_peer.close()
                endpoint.close()
                endpoint_peer.close()


if __name__ == "__main__":
    unittest.main()
