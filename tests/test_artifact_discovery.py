"""Host font discovery over real local files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mioa_os import LocalArtifactContent, LocalArtifactDiscovery


class ArtifactDiscoveryTests(unittest.TestCase):
    def test_missing_font_entry_does_not_hide_other_fonts(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            fonts = home / "Library" / "Fonts"
            fonts.mkdir(parents=True)
            valid = fonts / "present.ttf"
            valid.write_bytes(b"font")
            (fonts / "missing.otf").symlink_to(fonts / "deleted.otf")
            walk = os.walk

            def user_fonts_only(directory, **kwargs):
                return walk(directory, **kwargs) if directory == fonts else iter(())

            with patch.object(Path, "home", return_value=home), \
                    patch("mioa_os.artifacts.os.walk", side_effect=user_fonts_only):
                self.assertEqual(LocalArtifactDiscovery().font_paths(), [str(valid.resolve())])

    def test_disappearing_font_directory_does_not_hide_other_fonts(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            fonts = home / "Library" / "Fonts"
            fonts.mkdir(parents=True)
            valid = fonts / "present.ttf"
            valid.write_bytes(b"font")
            disappearing = fonts / "removed"
            disappearing.mkdir()
            walk = os.walk

            def remove_during_walk(directory, **kwargs):
                if directory != fonts:
                    return
                for entry in walk(directory, **kwargs):
                    yield entry
                    if Path(entry[0]) == fonts:
                        disappearing.rmdir()

            with patch.object(Path, "home", return_value=home), \
                    patch("mioa_os.artifacts.os.walk", side_effect=remove_during_walk):
                self.assertEqual(LocalArtifactDiscovery().font_paths(), [str(valid.resolve())])


class ArtifactContentTests(unittest.TestCase):
    def test_resolution_reads_and_bounds_real_files(self):
        import hashlib

        with tempfile.TemporaryDirectory() as temporary:
            content = LocalArtifactContent()
            path = Path(temporary) / "asset.bin"
            path.write_bytes(b"artifact bytes")
            resolved = content.resolve(path)
            self.assertEqual(resolved, str(path.resolve()))
            self.assertEqual(content.resolve(Path(temporary) / "absent"), str((Path(temporary) / "absent").resolve()))
            self.assertEqual(content.size(resolved), len(b"artifact bytes"))
            self.assertEqual(content.digest(resolved), hashlib.sha256(b"artifact bytes").hexdigest())
            self.assertEqual(content.read(resolved, 2, 5), b"tifac")
            self.assertIsInstance(content.fingerprint(resolved), tuple)
            for offset, length in ((-1, 1), (0, -1), (0, 15)):
                with self.subTest(offset=offset, length=length), self.assertRaises(OSError):
                    content.read(resolved, offset, length)
            with self.assertRaises(OSError):
                content.size(temporary)
            with self.assertRaises(OSError):
                content.digest(temporary)

    def test_executable_requires_regular_executable_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            content = LocalArtifactContent()
            path = Path(temporary) / "runtime"
            path.write_bytes(b"runtime")
            path.chmod(0o600)
            with self.assertRaises(OSError) as rejected:
                content.executable(path)
            self.assertIn("not executable", str(rejected.exception))
            path.chmod(0o700)
            self.assertEqual(content.executable(path), str(path.resolve()))
