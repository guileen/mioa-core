"""Host-owned terminal capability regressions."""
import ctypes as c
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest

if os.name == 'posix':
    import fcntl
    import termios


ROOT = Path(__file__).resolve().parents[1]

class TerminalMode(c.Structure):
    _fields_ = [('opaque', c.c_uint64 * 32)]


@unittest.skipUnless(os.name == 'posix', 'PTY regression requires POSIX')
class TerminalCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        library = Path(cls.build.name) / ('terminal.dylib' if sys.platform == 'darwin' else 'terminal.so')
        command = shlex.split(os.environ.get('CC', 'cc')) + [
            '-std=c11', '-Wall', '-Wextra', '-Werror', '-fPIC',
            '-dynamiclib' if sys.platform == 'darwin' else '-shared',
            '-I', str(ROOT / 'mioa-os/api/system'),
            str(ROOT / 'mioa-os/terminal.c'), '-o', str(library)]
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        cls.lib = c.CDLL(str(library))
        cls.lib.mioa_terminal_stderr_columns_v1.argtypes = []
        cls.lib.mioa_terminal_stderr_columns_v1.restype = c.c_uint32
        cls.lib.mioa_terminal_stdin_raw_enter_v1.argtypes = [c.POINTER(TerminalMode)]
        cls.lib.mioa_terminal_stdin_raw_enter_v1.restype = c.c_int32
        cls.lib.mioa_terminal_stdin_raw_restore_v1.argtypes = [c.POINTER(TerminalMode)]
        cls.lib.mioa_terminal_stdin_raw_restore_v1.restype = c.c_int32

    def with_stderr(self, fd, callback):
        saved = os.dup(2)
        try:
            os.dup2(fd, 2)
            return callback()
        finally:
            os.dup2(saved, 2)
            os.close(saved)

    def test_queries_current_width_and_tracks_resize(self):
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 93, 0, 0))
            self.assertEqual(self.with_stderr(slave, self.lib.mioa_terminal_stderr_columns_v1), 93)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 51, 0, 0))
            self.assertEqual(self.with_stderr(slave, self.lib.mioa_terminal_stderr_columns_v1), 51)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 0, 0, 0))
            self.assertEqual(self.with_stderr(slave, self.lib.mioa_terminal_stderr_columns_v1), 0)
        finally:
            os.close(master)
            os.close(slave)

    def test_non_terminal_stderr_reports_unavailable(self):
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            self.assertEqual(self.with_stderr(fd, self.lib.mioa_terminal_stderr_columns_v1), 0)
        finally:
            os.close(fd)

    def test_raw_mode_restores_original_and_retries_failed_restore(self):
        master, slave = os.openpty()
        saved = os.dup(0)
        null = os.open(os.devnull, os.O_RDONLY)
        mode = TerminalMode()
        original = termios.tcgetattr(slave)
        try:
            os.dup2(slave, 0)
            self.assertEqual(self.lib.mioa_terminal_stdin_raw_enter_v1(c.byref(mode)), 0)
            raw = termios.tcgetattr(slave)
            self.assertEqual(raw[3] & (termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG), 0)
            self.assertEqual(raw[6][termios.VMIN], 0)
            self.assertEqual(raw[6][termios.VTIME], 1)
            self.assertNotEqual(self.lib.mioa_terminal_stdin_raw_enter_v1(c.byref(mode)), 0)
            os.dup2(null, 0)
            self.assertNotEqual(self.lib.mioa_terminal_stdin_raw_restore_v1(c.byref(mode)), 0)
            os.dup2(slave, 0)
            self.assertEqual(self.lib.mioa_terminal_stdin_raw_restore_v1(c.byref(mode)), 0)
            self.assertEqual(termios.tcgetattr(slave), original)
            self.assertNotEqual(self.lib.mioa_terminal_stdin_raw_restore_v1(c.byref(mode)), 0)
        finally:
            termios.tcsetattr(slave, termios.TCSAFLUSH, original)
            os.dup2(saved, 0)
            os.close(saved)
            os.close(null)
            os.close(master)
            os.close(slave)

    def test_raw_mode_rejects_non_terminal_without_changing_state(self):
        saved = os.dup(0)
        null = os.open(os.devnull, os.O_RDONLY)
        mode = TerminalMode()
        try:
            os.dup2(null, 0)
            self.assertNotEqual(self.lib.mioa_terminal_stdin_raw_enter_v1(c.byref(mode)), 0)
            self.assertEqual(bytes(mode), bytes(TerminalMode()))
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(null)


if __name__ == '__main__':
    unittest.main()
