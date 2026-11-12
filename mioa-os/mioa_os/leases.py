"""Trusted-local Profile lock and descriptor-transfer implementation.

This is host authority, not a sandbox. Shared locks are never explicitly
unlocked: inherited and escrowed open-file descriptions remain authoritative
until their last owner closes them.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import time

from .messages import LocalUnixMessages


def _close_handles(handles):
    failure = None
    while handles:
        handle = handles.pop()
        try:
            handle.close()
        except BaseException as error:
            if failure is None:
                failure = error
            else:
                failure.add_note(f"additional lease close failure: {error!r}")
    if failure is not None:
        raise failure


class _Lease:
    def __init__(self, handles, home, name, kind):
        self._handles = handles
        self._scope = (str(Path(home).resolve()), name, kind)
    def authorizes(self, home, name, kind):
        return bool(self._handles) and self._scope == (str(Path(home).resolve()), name, kind)


    def close(self):
        handles, self._handles = self._handles, []
        _close_handles(handles)


class _LeaseCopy:
    def __init__(self, received):
        self._received = received

    def close(self):
        self._received.close()


class LocalProfileLeases:
    """Explicit Host authority for compatible store and legacy profile locks."""

    @contextmanager
    def acquire(self, home, name, kind="engine", timeout=0.0):
        if kind not in {"engine", "spawn", "catalog"}:
            raise ValueError("unknown profile lease kind")
        if not name or name != "".join(c for c in name if c.isalnum() or c in "-_").strip("-_"):
            raise ValueError("invalid profile name")
        home = Path(home)
        directory = home / ".leases"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise ValueError("unsafe profile lease directory")
        os.chmod(directory, 0o700)
        key = "catalog" if kind == "catalog" else hashlib.sha256(name.encode()).hexdigest()
        paths = [directory / f"{key}.{kind}"]
        if kind != "catalog":
            legacy = home / "profiles" / name / {"engine": ".lock", "spawn": ".spawn"}[kind]
            # Imported profiles may not have legacy lock files yet.
            if legacy.parent.is_dir() or legacy.is_symlink():
                paths.append(legacy)
        handles = []
        lease = None
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            for path in paths:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    handle = os.fdopen(descriptor, "a+")
                except BaseException:
                    os.close(descriptor)
                    raise
                handles.append(handle)
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            _close_handles(handles)
                            yield None
                            return
                        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            lease = _Lease(handles, home, name, kind)
            yield lease
        finally:
            if lease is not None:
                lease.close()
            else:
                _close_handles(handles)

    def send(self, channel, body, lease, *, max_bytes):
        if not isinstance(lease, _Lease) or not lease._handles:
            raise ValueError("invalid or closed profile lease")
        LocalUnixMessages().send_files(channel, body, [h.fileno() for h in lease._handles], max_bytes=max_bytes)

    def receive(self, channel, *, max_bytes):
        body, received = LocalUnixMessages().receive_files(channel, max_bytes=max_bytes, max_files=2)
        try:
            return body, [_LeaseCopy(item) for item in received]
        except BaseException:
            for item in received:
                item.close()
            raise

    def send_copy(self, channel, body, lease, *, max_bytes):
        if not isinstance(lease, _LeaseCopy):
            raise ValueError("invalid profile lease copy")
        LocalUnixMessages().send_files(channel, body, [lease._received], max_bytes=max_bytes)
