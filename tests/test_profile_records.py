import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from mioa_os import LocalProfileRecordWriter
from mioa_os_api.profile_records import ProfileCommitUncertain


class ProfileRecordTests(unittest.TestCase):
    def test_replaces_only_selected_record_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "state.json").write_bytes(b"state")
            writer = LocalProfileRecordWriter(path)
            writer.write("config", b"old")
            writer.write("config", b"new")
            self.assertEqual((path / "config.json").read_bytes(), b"new")
            self.assertEqual((path / "state.json").read_bytes(), b"state")
            self.assertEqual(stat.S_IMODE((path / "config.json").stat().st_mode), 0o600)
            self.assertEqual({p.name for p in path.iterdir()}, {"config.json", "state.json"})
            with self.assertRaises(ValueError):
                writer.write("../state", b"bad")

    def test_file_sync_failure_preserves_record_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            target = path / "state.json"
            target.write_bytes(b"old")
            with patch("mioa_os.profile_records.os.fsync", side_effect=OSError("sync failed")):
                with self.assertRaisesRegex(OSError, "sync failed"):
                    LocalProfileRecordWriter(path).write("state", b"new")
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual([p.name for p in path.iterdir()], ["state.json"])

    def test_directory_sync_failure_reports_uncertain_committed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            target = path / "config.json"
            target.write_bytes(b"old")
            sync = os.fsync
            count = 0

            def fail_directory(fd):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("directory sync failed")
                sync(fd)

            with patch("mioa_os.profile_records.os.fsync", side_effect=fail_directory):
                with self.assertRaises(ProfileCommitUncertain):
                    LocalProfileRecordWriter(path).write("config", b"new")
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual([p.name for p in path.iterdir()], ["config.json"])
