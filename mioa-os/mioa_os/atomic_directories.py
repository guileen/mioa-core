"""Atomic local directory exchange for explicitly trusted Host assemblies."""
import ctypes
import os
import sys
from pathlib import PurePath

from mioa_os_api import AtomicExchangeUnavailable


class LocalAtomicDirectories:
    def exchange(self, left: str | PurePath, right: str | PurePath) -> None:
        if sys.platform == "darwin":
            symbol, cwd = "renameatx_np", -2
        elif sys.platform.startswith("linux"):
            symbol, cwd = "renameat2", -100
        else:
            raise AtomicExchangeUnavailable("atomic directory exchange is unavailable")
        libc = ctypes.CDLL(None, use_errno=True)
        operation = getattr(libc, symbol, None)
        if operation is None:
            raise AtomicExchangeUnavailable("atomic directory exchange is unavailable")
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        if operation(cwd, os.fsencode(left), cwd, os.fsencode(right), 2):
            raise OSError(ctypes.get_errno(), "atomic profile tree exchange failed")
