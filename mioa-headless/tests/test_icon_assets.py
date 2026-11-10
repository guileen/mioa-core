"""Original resource identity, private permissions, retention and cleanup."""
import base64
import hashlib
from pathlib import Path
import stat
import struct
import tempfile
import unittest

from mioa_core.native import NativeSiteIcon
from mioa_headless.icon_assets import IconAssets

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mPQKDryHwAEggJefd/xlgAAAABJRU5ErkJggg==")
ICO = struct.pack("<HHHBBBBHHII", 0, 1, 1, 1, 1, 0, 0, 1, 32, len(PNG), 22) + PNG


def resource(data, mime):
    return NativeSiteIcon("document", 1, hashlib.sha256(data).hexdigest(), mime, data)


class IconAssetTests(unittest.TestCase):
    def test_exact_content_addressed_bytes_private_modes_and_referenced_retention(self):
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root)
            unrelated = parent / "unrelated"
            unrelated.write_bytes(b"owned by another component")
            store = IconAssets(parent)
            try:
                self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o700)
                png = store.publish(resource(PNG, "image/png"))
                path = Path(png["path"])
                self.assertTrue(path.is_absolute())
                self.assertEqual(path.read_bytes(), PNG)
                self.assertEqual(png["sha256"], "f071d5a85a4d70c825fe3b3d78e72ff337e77a1dc576de67fd897aef0c286b85")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                inode, modified = path.stat().st_ino, path.stat().st_mtime_ns
                self.assertEqual(store.publish(resource(PNG, "image/png")), png)
                self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (inode, modified))
                ico = store.publish(resource(ICO, "image/x-icon"))
                self.assertEqual(Path(ico["path"]).read_bytes(), ICO)
                store.retain({ico["sha256"]})
                self.assertFalse(path.exists())
                self.assertTrue(Path(ico["path"]).exists())
                self.assertIsNone(store.publish(None))
            finally:
                store.close()
            self.assertFalse(store.path.exists())
            self.assertEqual(unrelated.read_bytes(), b"owned by another component")

    def test_invalid_or_oversized_resource_cannot_create_an_asset(self):
        with tempfile.TemporaryDirectory() as root:
            store = IconAssets(Path(root))
            try:
                for icon in (NativeSiteIcon("document", 1, "../foreign", "image/png", PNG),
                             resource(b"x" * (512 * 1024 + 1), "image/png"),
                             resource(PNG, "text/html")):
                    with self.subTest(icon=icon.mime), self.assertRaises(ValueError):
                        store.publish(icon)
                self.assertEqual(list(store.path.iterdir()), [])
            finally:
                store.close()

    def test_retention_failure_still_reclaims_other_assets_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as root:
            store = IconAssets(Path(root))
            png = store.publish(resource(PNG, "image/png"))
            ico = store.publish(resource(ICO, "image/x-icon"))
            blocked = Path(png["path"])
            blocked.unlink()
            blocked.mkdir()
            foreign = blocked / "foreign"
            foreign.write_bytes(b"not an icon asset")
            try:
                with self.assertRaises(OSError):
                    store.retain(set())
                self.assertFalse(Path(ico["path"]).exists())
                self.assertEqual(foreign.read_bytes(), b"not an icon asset")
            finally:
                foreign.unlink()
                blocked.rmdir()
                blocked.write_bytes(PNG)
                store.close()
            self.assertFalse(store.path.exists())
