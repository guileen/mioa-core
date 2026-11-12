"""Host lease boundary regressions using real locks and processes."""
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mioa_core import profile
from mioa_os import LocalProfileLeases


class ProfileLeaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.leases = LocalProfileLeases()

    def test_profile_requires_explicit_host(self):
        with self.assertRaises(TypeError):
            profile.exclusive("example")

    def test_rejects_symlink_lease_directory(self):
        outside = self.home / "outside"
        outside.mkdir()
        (self.home / ".leases").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            with self.leases.acquire(self.home, "example"):
                self.fail("unsafe directory must not grant a lease")

    def test_legacy_busy_rolls_back_store_lock_and_capability_is_opaque(self):
        legacy = self.home / "profiles" / "example" / ".lock"
        legacy.parent.mkdir(parents=True)
        with legacy.open("w+") as blocker:
            fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.leases.acquire(self.home, "example") as busy:
                self.assertIsNone(busy)
                # Busy must have rolled back the store lock before returning.
                legacy.unlink()
                with self.leases.acquire(self.home, "example") as lease:
                    self.assertIsNotNone(lease)
                    self.assertTrue(lease.authorizes(self.home, "example", "engine"))
                    self.assertFalse(lease.authorizes(self.home, "other", "engine"))
                    self.assertFalse(hasattr(lease, "fileno"))
        self.assertFalse(lease.authorizes(self.home, "example", "engine"))

    def test_close_attempts_remaining_lock_after_error(self):
        legacy = self.home / "profiles" / "example" / ".lock"
        legacy.parent.mkdir(parents=True)
        with self.leases.acquire(self.home, "example") as lease:
            store, legacy_handle = lease._handles

            class FailedClose:
                def close(self):
                    raise OSError("injected close failure")

            lease._handles[-1] = FailedClose()
            self.addCleanup(legacy_handle.close)
            self.addCleanup(store.close)
            with self.assertRaisesRegex(OSError, "injected close failure"):
                lease.close()
            self.assertFalse(lease.authorizes(self.home, "example", "engine"))
            with next((self.home / ".leases").glob("*.engine")).open("a+") as competitor:
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_busy_rollback_closes_other_locks_without_retrying_failed_close(self):
        legacy = self.home / "profiles" / "example" / ".lock"
        legacy.parent.mkdir(parents=True)
        real_fdopen = os.fdopen
        opened = []
        attempts = []

        class FailedClose:
            def __init__(self, handle):
                self.handle = handle

            def fileno(self):
                return self.handle.fileno()

            def close(self):
                attempts.append("close")
                raise OSError("injected rollback failure")

        def fdopen(*args, **kwargs):
            handle = real_fdopen(*args, **kwargs)
            opened.append(handle)
            self.addCleanup(handle.close)
            return FailedClose(handle) if len(opened) == 2 else handle

        with legacy.open("w+") as blocker:
            fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("mioa_os.leases.os.fdopen", side_effect=fdopen):
                with self.assertRaisesRegex(OSError, "injected rollback failure"):
                    with self.leases.acquire(self.home, "example"):
                        self.fail("busy lease must not be granted")
            with next((self.home / ".leases").glob("*.engine")).open("a+") as competitor:
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(attempts, ["close"])

    def test_escrow_copies_keep_both_locks_until_last_copy_closes(self):
        legacy = self.home / "profiles" / "example" / ".lock"
        legacy.parent.mkdir(parents=True)
        legacy.touch()
        sender, receiver = socket.socketpair()
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        with self.leases.acquire(self.home, "example") as original:
            self.leases.send(sender, b"ok", original, max_bytes=64)
            body, copies = self.leases.receive(receiver, max_bytes=64)
            self.assertEqual(body, b"ok")
            self.assertEqual(len(copies), 2)
            for copy in copies:
                self.addCleanup(copy.close)
                self.assertFalse(hasattr(copy, "fileno"))
        with self.leases.acquire(self.home, "example") as busy:
            self.assertIsNone(busy)
        for copy in copies:
            copy.close()
            copy.close()
        with self.leases.acquire(self.home, "example") as acquired:
            self.assertIsNotNone(acquired)

    def test_child_inheritance_survives_owner_death(self):
        program = r'''
import asyncio, os, sys
from mioa_os import LocalChildProcesses, LocalProfileLeases
async def main():
    with LocalProfileLeases().acquire(sys.argv[1], "example") as lease:
        child = await LocalChildProcesses().start([sys.executable, "-c",
            "import os,time; print(os.getpid(),flush=True); time.sleep(30)"],
            environment=dict(os.environ), profile_lease=lease)
        print((await child.stdout.readline()).decode().strip(), flush=True)
        os._exit(0)
asyncio.run(main())
'''
        owner = subprocess.Popen([sys.executable, "-c", program, str(self.home)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = owner.stdout.readline()
        self.assertTrue(line, owner.stderr.read())
        child_pid = int(line)
        pending = [True]
        self.addCleanup(lambda: self._kill_if_alive(child_pid) if pending[0] else None)
        self.assertEqual(owner.wait(timeout=10), 0)
        owner.stdout.close()
        owner.stderr.close()
        with self.leases.acquire(self.home, "example") as busy:
            self.assertIsNone(busy)
        os.kill(child_pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while True:
            with self.leases.acquire(self.home, "example") as acquired:
                if acquired is not None:
                    break
            if time.monotonic() >= deadline:
                self.fail("child exit did not release inherited lease")
            time.sleep(0.05)
        pending[0] = False

    @staticmethod
    def _kill_if_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
