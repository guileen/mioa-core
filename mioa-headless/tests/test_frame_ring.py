"""Behavioral LPFR ownership regressions; compile the production C11 helper."""
from __future__ import annotations

import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mioa_headless.frame_ring import CAPACITY, HEADER, SLOT_SIZE, SIZE, FrameRing, atomic_library


class AtomicLoaderTests(unittest.TestCase):
    def test_prebuilt_library_skips_compilation(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "frame.dylib"
            library.touch()
            expected = object()
            with patch.dict(os.environ, {"MIOA_FRAME_ATOMICS": str(library)}), \
                    patch("mioa_headless.frame_ring._load_library", return_value=expected) as load, \
                    patch("mioa_headless.frame_ring.subprocess.run") as compile:
                self.assertIs(atomic_library(), expected)
                load.assert_called_once_with(library)
                compile.assert_not_called()

    def test_invalid_prebuilt_path_fails_without_compiler_fallback(self):
        with patch.dict(os.environ, {"MIOA_FRAME_ATOMICS": "relative.dylib"}), \
                patch("mioa_headless.frame_ring.subprocess.run") as compile:
            with self.assertRaises(ValueError):
                atomic_library()
            compile.assert_not_called()


class FrameRingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        os.chmod(self.root, 0o700)
        self.ring = FrameRing(self.root / "frame", self.root / "control.sock")

    def tearDown(self):
        self.ring.close()
        self.directory.cleanup()

    @staticmethod
    def metadata():
        return {"width": 8, "height": 8, "stride": 32, "dpr_milli": 1000,
                "theme_edge_rgba": 0xff102030, "caret_rect": [1.0, 2.0, 3.0, 4.0]}

    def test_exact_v2_abi_and_permissions(self):
        self.assertEqual(len(self.ring.mapping), SIZE)
        self.assertEqual(self.ring.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(struct.unpack_from("<IHHIIQ", self.ring.mapping), (0x4c504652, 2, 0, 1, CAPACITY, 0))
        self.assertEqual(self.ring.publish(self.metadata()), 1)
        front = self.ring.lib.lpfr_acquire(self.ring.base, 0)
        self.assertEqual(front, 2)
        header = struct.unpack_from("<Q9I4fI", self.ring.mapping, HEADER + front * SLOT_SIZE)
        self.assertEqual(header, (1, 8, 8, 32, 1000, 0, 0, 8, 8, 0xff102030, 1.0, 2.0, 3.0, 4.0, 0))

    def test_producer_never_overwrites_acquired_front_under_contention(self):
        errors = []
        started = threading.Event()
        def produce():
            try:
                for seq in range(1, 1001):
                    offset = HEADER + self.ring.back * SLOT_SIZE + HEADER
                    self.ring.mapping[offset:offset + 256] = bytes([seq % 256]) * 256
                    self.ring.publish(self.metadata())
                    started.set()
                    time.sleep(0.00005)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=produce)
        thread.start()
        started.wait(2)
        front, observed = 0, 0
        try:
            while thread.is_alive():
                front = self.ring.lib.lpfr_acquire(self.ring.base, front)
                offset = HEADER + front * SLOT_SIZE
                seq, = struct.unpack_from("<Q", self.ring.mapping, offset)
                if seq:
                    expected = bytes([seq % 256]) * 256
                    self.assertEqual(self.ring.mapping[offset + HEADER:offset + HEADER + 256], expected)
                    time.sleep(0.0002)  # retain front while producer rotates its other two slots
                    self.assertEqual(self.ring.mapping[offset + HEADER:offset + HEADER + 256], expected)
                    self.assertEqual(struct.unpack_from("<Q", self.ring.mapping, offset)[0], seq)
                    observed += 1
        finally:
            thread.join()
        self.assertFalse(errors)
        self.assertGreater(observed, 0)

    def test_oversize_never_publishes_or_advances_generation(self):
        for changed in ({"width": 3841, "height": 2160, "stride": 3841 * 4}, {"stride": 31}, {"width": True}):
            with self.assertRaises(ValueError):
                self.ring.publish({**self.metadata(), **changed})
            self.assertEqual(self.ring.seq, 0)

    def test_live_owner_cannot_be_reclaimed(self):
        with self.assertRaises(BlockingIOError):
            FrameRing(self.ring.path, self.ring.socket)
        self.assertTrue(self.ring.path.exists())
        self.ring.publish(self.metadata())

    def test_crashed_owner_files_are_reinitialized_after_lock_release(self):
        self.ring.publish(self.metadata())
        self.ring.mapping.close()
        os.close(self.ring.lock)  # simulate process death without unlinking artifacts
        self.ring = FrameRing(self.root / "frame", self.root / "control.sock")
        self.assertEqual(self.ring.seq, 0)
        self.assertEqual(struct.unpack_from("<Q", self.ring.mapping, 16)[0], 0)
