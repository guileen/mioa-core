"""LPFR v2 single-producer/single-consumer ring; pixels never enter JSON."""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import mmap
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile

CAPACITY = 3840 * 2160 * 4
HEADER = 64
SLOT_SIZE = HEADER + CAPACITY
SIZE = HEADER + 3 * SLOT_SIZE
MAGIC = 0x4C504652


def atomic_library():
    prebuilt = os.environ.get("MIOA_FRAME_ATOMICS") or os.environ.get("LIGHTPANDA_FRAME_ATOMICS")
    if prebuilt is not None:
        path = Path(prebuilt)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("MIOA_FRAME_ATOMICS must name an absolute prebuilt library")
        return _load_library(path)
    source = Path(__file__).with_name("frame_atomic.c")
    digest = hashlib.sha256(source.read_bytes() + sys.platform.encode() + os.uname().machine.encode()).hexdigest()[:24]
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mioa-headless"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = cache.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("native helper cache must be a private owned directory")
    target = cache / (digest + (".dylib" if sys.platform == "darwin" else ".so"))
    with (cache / "compile.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            fd, temporary = tempfile.mkstemp(dir=cache, prefix="atomic-")
            os.close(fd)
            try:
                subprocess.run(["cc", "-std=c11", "-O2", "-dynamiclib" if sys.platform == "darwin" else "-shared",
                                "-fPIC", str(source), "-o", temporary], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return _load_library(target)


def _load_library(path: Path):
    lib = ctypes.CDLL(str(path))
    lib.lpfr_supported.argtypes = [ctypes.c_void_p]
    lib.lpfr_supported.restype = ctypes.c_int
    lib.lpfr_initialize.argtypes = [ctypes.c_void_p]
    lib.lpfr_initialize.restype = None
    lib.lpfr_publish.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64]
    lib.lpfr_publish.restype = ctypes.c_uint32
    lib.lpfr_acquire.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.lpfr_acquire.restype = ctypes.c_uint32
    return lib


class FrameRing:
    def __init__(self, path: Path, socket: Path):
        self.path, self.socket = path, socket
        if (not path.is_absolute() or not socket.is_absolute() or path.parent != socket.parent or path == socket
                or path.name == ".native-owner" or socket.name == ".native-owner" or len(os.fsencode(socket)) > 103):
            raise ValueError("native paths must be distinct absolute paths in the same private directory")
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("native session directory must be owned and mode 0700")
        self.lock_path = path.parent / ".native-owner"
        self.lock = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        created = False
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            # Only the lock owner may reclaim the previous crashed session's files.
            for item, kind in ((path, stat.S_ISREG), (socket, stat.S_ISSOCK)):
                if item.exists() or item.is_symlink():
                    old = item.lstat()
                    if old.st_uid != os.getuid() or not kind(old.st_mode):
                        raise ValueError("refusing to replace a foreign native artifact")
                    item.unlink()
            self.lib = atomic_library()
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            try:
                os.ftruncate(fd, SIZE)
                self.mapping = mmap.mmap(fd, SIZE)
            finally:
                os.close(fd)
            self.base = ctypes.addressof(ctypes.c_char.from_buffer(self.mapping))
            if not self.lib.lpfr_supported(self.base):
                raise RuntimeError("LPFR requires lock-free 32/64-bit process-shared atomics")
            struct.pack_into("<IHHIIQ", self.mapping, 0, MAGIC, 2, 0, 1, CAPACITY, 0)
            self.lib.lpfr_initialize(self.base)
            self.back, self.seq = 2, 0
        except BaseException:
            if created:
                if hasattr(self, "mapping"):
                    self.mapping.close()
                path.unlink(missing_ok=True)
            if acquired:
                self.lock_path.unlink(missing_ok=True)
            os.close(self.lock)
            raise

    def publish(self, metadata: dict) -> int:
        width, height, stride = (metadata[key] for key in ("width", "height", "stride"))
        if (any(type(value) is not int for value in (width, height, stride)) or width <= 0 or height <= 0
                or stride != width * 4 or stride * height > CAPACITY):
            raise ValueError("raw frame exceeds immutable LPFR capacity")
        dpr = metadata["dpr_milli"]
        caret = metadata.get("caret_rect", [0.0] * 4)
        theme = metadata["theme_edge_rgba"]
        self.seq += 1
        offset = HEADER + self.back * SLOT_SIZE
        # The renderer has finished writing the pixels of this exclusively owned slot.
        struct.pack_into("<Q9I4fI", self.mapping, offset, self.seq, width, height, stride, dpr,
                         0, 0, width, height, theme, *caret, 0)
        self.back = self.lib.lpfr_publish(self.base, self.back, self.seq)
        return self.seq

    def close(self):
        self.mapping.close()
        self.path.unlink(missing_ok=True)
        self.socket.unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)
        os.close(self.lock)
