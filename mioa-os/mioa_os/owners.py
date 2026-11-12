"""Trusted-local persisted profile owner identities; leases remain authoritative."""
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat

import psutil


def _name(name):
    if not name or name != "".join(c for c in name if c.isalnum() or c in "-_").strip("-_"):
        raise ValueError("invalid profile name")
    return name


def _identity(value):
    if not isinstance(value, dict):
        return None
    pid, start = value.get("pid"), value.get("process_start")
    if type(pid) is not int or pid <= 0 or pid > 2**31 - 1:
        return None
    if type(start) not in (int, float) or start <= 0:
        return None
    try:
        if not math.isfinite(start):
            return None
    except OverflowError:
        return None
    return pid, start


def _alive(identity):
    if identity is None:
        return False
    pid, start = identity
    try:
        process = psutil.Process(pid)
        if process.create_time() != start:
            return False
        if process.uids().real != os.getuid():
            raise PermissionError("profile owner belongs to another user")
        return process.status() != psutil.STATUS_ZOMBIE and process.is_running()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def _invalid_constant(value):
    raise ValueError("invalid JSON number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(fd):
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        return json.load(stream, parse_constant=_invalid_constant, object_pairs_hook=_unique_object)


def _directory(home, *, create):
    path = Path(home) / ".owners"
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if not create:
            return None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe profile owner directory")
        if create:
            os.fchmod(fd, 0o700)
        elif info.st_mode & 0o077:
            raise ValueError("unsafe profile owner directory permissions")
        return fd
    except BaseException:
        os.close(fd)
        raise


class LocalProfileOwners:
    """Explicit same-user local owner observations, separate from profile data."""

    def publish(self, home, name):
        name = _name(name)
        pid = os.getpid()
        process = psutil.Process(pid)
        if process.uids().real != os.getuid() or process.status() == psutil.STATUS_ZOMBIE:
            raise RuntimeError("cannot identify current profile owner")
        identity = {"pid": pid, "process_start": process.create_time()}
        if _identity(identity) is None or not process.is_running():
            raise RuntimeError("cannot identify current profile owner")
        data = (json.dumps(identity, allow_nan=False, separators=(",", ":")) + "\n").encode()
        key = hashlib.sha256(name.encode()).hexdigest() + ".json"
        temporary = "." + key + "." + secrets.token_hex(16)
        directory = _directory(home, create=True)
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
            created = True
            try:
                with os.fdopen(fd, "wb") as stream:
                    fd = -1
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)
            os.replace(temporary, key, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                if created:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(directory)

    def alive(self, home, name):
        name = _name(name)
        state = Path(home) / "profiles" / name / "state.json"
        try:
            fd = os.open(state, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("invalid profile state file")
        legacy = _read_json(fd)
        if not isinstance(legacy, dict):
            raise ValueError("invalid profile state")
        legacy_identity = "pid" in legacy or "process_start" in legacy
        identity = _identity(legacy)
        if legacy_identity and identity is None:
            raise ValueError("invalid legacy profile owner identity")
        if _alive(identity):
            return True
        directory = _directory(home, create=False)
        if directory is None:
            if legacy_identity:
                return False
            raise ValueError("profile owner record missing")
        try:
            key = hashlib.sha256(name.encode()).hexdigest() + ".json"
            try:
                fd = os.open(key, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
            except FileNotFoundError:
                if legacy_identity:
                    return False
                raise ValueError("profile owner record missing")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                os.close(fd)
                raise ValueError("unsafe profile owner record")
            value = _read_json(fd)
            identity = _identity(value)
            if identity is None:
                raise ValueError("invalid profile owner record")
            return _alive(identity)
        finally:
            os.close(directory)
