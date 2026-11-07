import hashlib
from pathlib import Path
import tempfile
import unittest

from mioa_core import native_spec, profile


class NativeArtifactMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        previous = profile.BASE
        profile.BASE = root / 'home'
        self.addCleanup(setattr, profile, 'BASE', previous)
        self.old_binary, self.new_binary = root / 'old-runtime', root / 'new-runtime'
        for path, content in ((self.old_binary, b'old artifact'), (self.new_binary, b'new artifact')):
            path.write_bytes(content)
            path.chmod(0o700)
        self.old_font, self.new_font = root / 'old-font', root / 'new-font'
        for path in (self.old_font, self.new_font):
            path.write_bytes(b'identical font artifact')
        self.original = native_spec.create_native_spec('desktop', runtime='mioa', seed='ab' * 32,
            binary=self.old_binary, storage_path=profile.profile_dir('desktop') / 'data',
            asset_paths={'fonts': [str(self.old_font)]})
        profile.save_config('desktop', self.original)
        self.directory = profile.profile_dir('desktop')
        self.data = self.directory / 'data'
        self.data.mkdir(mode=0o700)
        (self.data / 'browser-storage').write_bytes(b'prior browser state')
        self.original_bytes = (self.directory / 'config.json').read_bytes()

    def test_runtime_and_font_upgrade_preserves_identity_and_rolls_back_complete_tree(self):
        result = profile.migrate('desktop', binary=self.new_binary, assets={'fonts': [str(self.new_font)]})
        upgraded = native_spec.validate_native_spec(profile.require('desktop'))
        for field in ('id', 'seed', 'resolved', 'network', 'permissions', 'storage'):
            self.assertEqual(upgraded[field], self.original[field], field)
        self.assertEqual(upgraded['revision'], self.original['revision'] + 1)
        self.assertEqual(upgraded['runtime']['artifact_sha256'], hashlib.sha256(b'new artifact').hexdigest())
        self.assertEqual(upgraded['asset_paths']['fonts'], [str(self.new_font)])
        self.assertEqual(upgraded['assets'], self.original['assets'])
        self.assertEqual((self.data / 'browser-storage').read_bytes(), b'prior browser state')
        snapshot = Path(result['snapshot'])
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), result['snapshot_sha256'])
        (self.data / 'browser-storage').write_bytes(b'new runtime browser state')
        profile.migrate('desktop', rollback=snapshot)
        self.assertEqual((self.directory / 'config.json').read_bytes(), self.original_bytes)
        self.assertEqual((self.data / 'browser-storage').read_bytes(), b'prior browser state')
        native_spec.validate_native_spec(profile.require('desktop'))

    def test_missing_replacement_font_cannot_commit_or_change_browser_storage(self):
        with self.assertRaises(native_spec.NativeError) as caught:
            profile.migrate('desktop', binary=self.new_binary,
                            assets={'fonts': [str(self.new_font.parent / 'missing')]})
        self.assertEqual(caught.exception.code, 'incompatible_profile')
        self.assertEqual((self.directory / 'config.json').read_bytes(), self.original_bytes)
        self.assertEqual((self.data / 'browser-storage').read_bytes(), b'prior browser state')
        self.assertFalse((profile.BASE / 'snapshots').exists())
