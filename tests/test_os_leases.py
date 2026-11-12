"""Real Profile lock compatibility; no browser or sandbox qualification."""
import fcntl
from pathlib import Path
import tempfile
import unittest

from mioa_os import LocalProfileLeases


class ProfileLeaseCompatibilityTests(unittest.TestCase):
    def test_missing_legacy_lock_still_excludes_old_owner_until_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            profile = home / "profiles" / "imported"
            profile.mkdir(parents=True, mode=0o700)
            legacy = profile / ".lock"
            self.assertFalse(legacy.exists())
            with LocalProfileLeases().acquire(home, "imported") as lease:
                self.assertIsNotNone(lease)
                with legacy.open("a+") as old_owner:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(old_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with legacy.open("a+") as old_owner:
                fcntl.flock(old_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
