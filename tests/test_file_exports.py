"""Real export side effects through NativePage; CDP image source is a fixture."""
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mioa_core.native import NativeContext, NativeError, NativePage
from mioa_os import LocalFileExports

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mPQKDryHwAEggJefd/xlgAAAABJRU5ErkJggg==")


class ContractImportTests(unittest.TestCase):
    def test_core_loads_without_os_implementation(self):
        script = """
import importlib.abc
import sys

class DenyImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mioa_os' or fullname.startswith('mioa_os.'):
            raise AssertionError('Core attempted to import the OS implementation')
        return None

sys.meta_path.insert(0, DenyImplementation())
import mioa_os_api
import mioa_core.engine
import mioa_core.native
import mioa_core.profile_service
assert not any(name == 'mioa_os' or name.startswith('mioa_os.') for name in sys.modules)
"""
        subprocess.run([sys.executable, "-c", script], check=True)


class ScreenshotExportTests(unittest.IsolatedAsyncioTestCase):
    def page(self, exports, data=PNG):
        cdp = SimpleNamespace(listeners=[], send=AsyncMock(return_value={"data": base64.b64encode(data).decode()}))
        owner = SimpleNamespace(file_exports=exports)
        return NativePage(NativeContext(owner, cdp), "page", "session", "about:blank")

    async def test_export_overwrites_existing_file_and_returns_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "截图.png"
            destination.write_bytes(b"old trailing bytes" * 100)
            page = self.page(LocalFileExports())
            self.assertEqual(await page.screenshot(path=destination), PNG)
            self.assertEqual(destination.read_bytes(), PNG)
            destination.unlink()
            self.assertEqual(await page.screenshot(path=str(destination)), PNG)
            self.assertEqual(destination.read_bytes(), PNG)

    async def test_bytes_only_needs_no_export_authority(self):
        self.assertEqual(await self.page(None).screenshot(), PNG)

    async def test_missing_authority_does_not_touch_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "existing.png"
            destination.write_bytes(b"preserve")
            with self.assertRaises(NativeError) as caught:
                await self.page(None).screenshot(path=destination)
            self.assertEqual(caught.exception.code, "capability_unavailable")
            self.assertEqual(destination.read_bytes(), b"preserve")

    async def test_invalid_capture_does_not_truncate_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "existing.png"
            destination.write_bytes(b"preserve")
            with self.assertRaises(NativeError) as caught:
                await self.page(LocalFileExports(), b"not an image").screenshot(path=destination)
            self.assertEqual(caught.exception.code, "protocol_error")
            self.assertEqual(destination.read_bytes(), b"preserve")

    async def test_export_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing" / "image.png"
            with self.assertRaises(FileNotFoundError):
                await self.page(LocalFileExports()).screenshot(path=missing)
            self.assertFalse(missing.parent.exists())

    async def test_child_context_uses_root_host_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            page = self.page(None)
            root = self.page(LocalFileExports()).context
            page.context._application_root = root
            destination = Path(temporary) / "child.png"
            self.assertEqual(await page.screenshot(path=destination), PNG)
            self.assertEqual(destination.read_bytes(), PNG)
            root.owner.file_exports = None
            with self.assertRaises(NativeError):
                await page.screenshot(path=destination)
            self.assertEqual(destination.read_bytes(), PNG)

    async def test_short_os_writes_finish_without_copying_or_losing_tail(self):
        real_open = open

        class ShortWriter:
            def __init__(self, *args, **kwargs):
                self.output = real_open(*args, **kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.output.close()
            def write(self, data):
                return self.output.write(data[:3])

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "short.png"
            with patch("mioa_os.open", ShortWriter, create=True):
                self.assertEqual(await self.page(LocalFileExports()).screenshot(path=destination), PNG)
            self.assertEqual(destination.read_bytes(), PNG)

    def test_write_private_bytes_creates_file_with_0o600_mode(self):
        import stat
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "config.json"
            exports = LocalFileExports()
            exports.write_private_bytes(destination, b'{"private": true}')
            self.assertEqual(destination.read_bytes(), b'{"private": true}')
            mode = stat.S_IMODE(destination.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_private_export_restricts_existing_file_before_writing(self):
        import stat
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "config.json"
            destination.write_bytes(b"old")
            destination.chmod(0o644)
            LocalFileExports().write_private_bytes(destination, b"private")
            self.assertEqual(destination.read_bytes(), b"private")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_private_export_does_not_truncate_when_permissions_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "config.json"
            destination.write_bytes(b"preserve")
            with patch("mioa_os.os.fchmod", side_effect=PermissionError("cannot restrict")):
                with self.assertRaises(PermissionError):
                    LocalFileExports().write_private_bytes(destination, b"private")
            self.assertEqual(destination.read_bytes(), b"preserve")

    def test_private_export_rejects_fifo_without_waiting_for_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "pipe"
            os.mkfifo(destination)
            failures = []
            finished = threading.Event()

            def export():
                try:
                    LocalFileExports().write_private_bytes(destination, b"private")
                except OSError as error:
                    failures.append(error)
                finally:
                    finished.set()

            worker = threading.Thread(target=export, daemon=True)
            worker.start()
            reader = None
            try:
                self.assertTrue(finished.wait(1), "private export waited for a FIFO reader")
                self.assertEqual(len(failures), 1)
            finally:
                if not finished.is_set():
                    reader = os.open(destination, os.O_RDONLY | os.O_NONBLOCK)
                worker.join(2)
                if reader is not None:
                    os.close(reader)


if __name__ == "__main__":
    unittest.main()
