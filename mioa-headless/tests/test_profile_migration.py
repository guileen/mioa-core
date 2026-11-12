import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import psutil

from mioa_core import native_spec, profile
from mioa_os import LocalArtifactContent, LocalAtomicDirectories, LocalProfileDataReset, LocalProfileLeases, LocalProfileOwners, LocalProxySecrets, LocalSecureRandom, LocalSnapshotDirectories, LocalWallClock


class NativeArtifactMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        previous = profile.BASE
        profile.BASE = root / 'home'
        self.addCleanup(setattr, profile, 'BASE', previous)
        self.directories = LocalAtomicDirectories()
        self.snapshots = LocalSnapshotDirectories(profile.BASE)
        self.old_binary, self.new_binary = root / 'old-runtime', root / 'new-runtime'
        for path, content in ((self.old_binary, b'old artifact'), (self.new_binary, b'new artifact')):
            path.write_bytes(content)
            path.chmod(0o700)
        self.old_font, self.new_font = root / 'old-font', root / 'new-font'
        for path in (self.old_font, self.new_font):
            path.write_bytes(b'identical font artifact')
        self.original = native_spec.create_native_spec('desktop', runtime='mioa', seed='ab' * 32,
            binary=self.old_binary, storage_path=profile.profile_dir('desktop') / 'data',
            asset_paths={'fonts': [str(self.old_font)]}, artifact_content=LocalArtifactContent(),
            random=LocalSecureRandom())
        profile.save_config('desktop', self.original)
        self.directory = profile.profile_dir('desktop')
        self.data = self.directory / 'data'
        self.data.mkdir(mode=0o700)
        (self.data / 'browser-storage').write_bytes(b'prior browser state')
        self.original_bytes = (self.directory / 'config.json').read_bytes()

    def test_list_ignores_noncanonical_profile_directories(self):
        for name in ('desktop!', '---'):
            directory = self.directory.parent / name
            directory.mkdir()
            (directory / 'config.json').write_text('{}')
        rows = profile.list_profiles(owners=LocalProfileOwners())
        self.assertEqual([row['name'] for row in rows], ['desktop'])

    def test_host_profile_root_is_required_even_with_ambient_environment(self):
        ambient = Path(self.temporary.name) / 'ambient'
        with patch.object(profile, 'BASE', None), patch.dict(os.environ, {'ANTI_TRACE_HOME': str(ambient)}):
            with self.assertRaisesRegex(profile.ProfileError, 'Host profile root is required'):
                profile.store()
            with self.assertRaisesRegex(profile.ProfileError, 'Host profile root is required'):
                profile.exclusive('desktop', leases=LocalProfileLeases())
        self.assertFalse(ambient.exists())

    def test_reset_data_uses_explicit_host_and_preserves_profile_and_locks(self):
        for name in ('engine.log', 'engine.sock', 'state.json', '.lock', '.spawn', '.catalog'):
            (self.directory / name).write_text(name)
        profile.reset_data('desktop', data_reset=LocalProfileDataReset())
        self.assertTrue(self.data.is_dir())
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual((self.directory / 'config.json').read_bytes(), self.original_bytes)
        for name in ('engine.log', 'engine.sock', 'state.json'):
            self.assertFalse((self.directory / name).exists())
        for name in ('.lock', '.spawn', '.catalog'):
            self.assertEqual((self.directory / name).read_text(), name)

    def test_reset_data_has_no_ambient_fallback_and_propagates_denial(self):
        with self.assertRaisesRegex(profile.ProfileError, 'Host profile data reset service is required'):
            profile.reset_data('desktop', data_reset=None)
        class DeniedReset:
            def reset(self, directory):
                self.directory = directory
                raise PermissionError('denied')
        denied = DeniedReset()
        with self.assertRaisesRegex(PermissionError, 'denied'):
            profile.reset_data('desktop', data_reset=denied)
        self.assertEqual(denied.directory, self.directory)
        self.assertEqual((self.data / 'browser-storage').read_bytes(), b'prior browser state')

    def test_reset_data_does_not_follow_data_directory_symlink(self):
        import shutil
        shutil.rmtree(self.data)
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('keep')
        self.data.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            profile.reset_data('desktop', data_reset=LocalProfileDataReset())
        self.assertEqual((outside / 'keep').read_text(), 'keep')

    def test_export_requires_explicit_destination(self):
        with self.assertRaisesRegex(profile.ProfileError, 'explicit export destination is required'):
            profile.export('desktop', None, leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent())

    def test_runtime_and_font_upgrade_preserves_identity_and_rolls_back_complete_tree(self):
        result = profile.migrate('desktop', binary=self.new_binary, assets={'fonts': [str(self.new_font)]},
                                 leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), clock=LocalWallClock(), directories=self.directories, snapshots=self.snapshots)
        upgraded = native_spec.validate_native_spec(profile.require('desktop'), artifact_content=LocalArtifactContent())
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
        profile.migrate('desktop', rollback=snapshot, leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), clock=LocalWallClock(), directories=self.directories, snapshots=self.snapshots)
        self.assertEqual((self.directory / 'config.json').read_bytes(), self.original_bytes)
        self.assertEqual((self.data / 'browser-storage').read_bytes(), b'prior browser state')
        native_spec.validate_native_spec(profile.require('desktop'), artifact_content=LocalArtifactContent())

    def test_rollback_restores_stored_secret_not_environment_override(self):
        authority = LocalProxySecrets(profile.BASE)
        profile.configure_proxy(self.original, 'http://127.0.0.1:8101', proxy_secrets=authority)
        profile.save_config('desktop', self.original)
        key = self.original['network']['proxy_ref'][4:]
        with patch.dict('os.environ', {key: 'http://127.0.0.1:8102'}):
            result = profile.migrate('desktop', binary=self.new_binary,
                                     leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), proxy_secrets=authority, clock=LocalWallClock(), directories=self.directories, snapshots=self.snapshots)
            authority.write(self.original['id'], 'http://127.0.0.1:8103')
            with patch.object(self.directories, 'exchange', side_effect=OSError('exchange failed')):
                with self.assertRaises(OSError):
                    profile.migrate('desktop', rollback=result['snapshot'],
                                    leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), proxy_secrets=authority, clock=LocalWallClock(), directories=self.directories, snapshots=self.snapshots)
            self.assertEqual(authority.read(self.original['id']), 'http://127.0.0.1:8103')
            profile.migrate('desktop', rollback=result['snapshot'],
                            leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), proxy_secrets=authority, clock=LocalWallClock(), directories=self.directories, snapshots=self.snapshots)
            self.assertEqual(authority.read(self.original['id']), 'http://127.0.0.1:8101')
            self.assertEqual(native_spec.resolve_proxy(profile.require('desktop'), proxy_secrets=authority),
                             'http://127.0.0.1:8102')

    def test_invalid_host_clock_does_not_create_snapshot_or_commit(self):
        class InvalidClock:
            def unix_time_ns(self):
                return True

        with self.assertRaisesRegex(profile.ProfileError, 'invalid Host wall clock'):
            profile.migrate('desktop', binary=self.new_binary, leases=LocalProfileLeases(),
                            owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(),
                            clock=InvalidClock(), directories=self.directories, snapshots=self.snapshots)
        self.assertEqual((self.directory / 'config.json').read_bytes(), self.original_bytes)
        self.assertFalse((profile.BASE / 'snapshots').exists())

    def test_snapshot_store_symlink_is_rejected_before_migration(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        (profile.BASE / 'snapshots').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(profile.ProfileError, 'unsafe_snapshot_store'):
            profile.migrate('desktop', binary=self.new_binary, leases=LocalProfileLeases(),
                            owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(),
                            clock=LocalWallClock(), directories=self.directories, snapshots=self.snapshots)
        self.assertEqual((self.directory / 'config.json').read_bytes(), self.original_bytes)
        self.assertEqual(list(outside.iterdir()), [])

    def test_missing_replacement_font_cannot_commit_or_change_browser_storage(self):
        with self.assertRaises(native_spec.NativeError) as caught:
            profile.migrate('desktop', binary=self.new_binary,
                            assets={'fonts': [str(self.new_font.parent / 'missing')]}, leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), clock=LocalWallClock(), directories=self.directories, snapshots=self.snapshots)
        self.assertEqual(caught.exception.code, 'incompatible_profile')
        self.assertEqual((self.directory / 'config.json').read_bytes(), self.original_bytes)
        self.assertEqual((self.data / 'browser-storage').read_bytes(), b'prior browser state')
        self.assertFalse((profile.BASE / 'snapshots').exists())

    def test_failed_credential_cleanup_still_removes_import_staging(self):
        source = LocalProxySecrets(profile.BASE)
        profile.configure_proxy(self.original, 'http://127.0.0.1:8101', proxy_secrets=source)
        profile.save_config('desktop', self.original)
        archive = profile.export('desktop', Path(self.temporary.name) / 'profile.tar.gz',
                                 leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), include_credentials=True, proxy_secrets=source)
        profile.BASE = Path(self.temporary.name) / 'destination'
        destination = LocalProxySecrets(profile.BASE)
        with patch.object(profile, '_sync_tree', side_effect=OSError('import failed')), \
             patch.object(destination, 'delete', side_effect=OSError('credential cleanup failed')):
            with self.assertRaisesRegex(OSError, 'credential cleanup failed') as caught:
                profile.import_archive(archive, rename='copy', leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(),
                                       proxy_secrets=destination)
        self.assertEqual(str(caught.exception.__context__), 'import failed')
        self.assertFalse(profile.profile_dir('copy').exists())
        self.assertEqual(list(profile.store().glob('.import-*')), [])
        self.assertEqual(destination.read(self.original['id']), 'http://127.0.0.1:8101')

    def test_failed_import_restores_preexisting_secret(self):
        source = LocalProxySecrets(profile.BASE)
        profile.configure_proxy(self.original, 'http://127.0.0.1:8101', proxy_secrets=source)
        profile.save_config('desktop', self.original)
        archive = profile.export('desktop', Path(self.temporary.name) / 'profile.tar.gz',
                                 leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(), include_credentials=True, proxy_secrets=source)
        profile.BASE = Path(self.temporary.name) / 'destination'
        destination = LocalProxySecrets(profile.BASE)
        for selected in (None, 'DIRECT'):
            with self.subTest(proxy=selected):
                destination.write(self.original['id'], 'http://127.0.0.1:8103')
                with patch.object(profile, '_sync_tree', side_effect=OSError('import failed')):
                    with self.assertRaisesRegex(OSError, 'import failed'):
                        profile.import_archive(archive, rename='copy', leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent(),
                                               proxy=selected, proxy_secrets=destination)
                self.assertEqual(destination.read(self.original['id']), 'http://127.0.0.1:8103')
                self.assertFalse(profile.profile_dir('copy').exists())
                self.assertEqual(list(profile.store().glob('.import-*')), [])

    def test_export_rejects_incomplete_legacy_owner(self):
        (self.directory / 'state.json').write_text('{"pid":1}')
        archive = Path(self.temporary.name) / 'profile.tar.gz'
        with self.assertRaisesRegex(ValueError, 'invalid legacy profile owner identity'):
            profile.export('desktop', archive, leases=LocalProfileLeases(), owners=LocalProfileOwners(), artifact_content=LocalArtifactContent())
        self.assertFalse(archive.exists())


class ProfileOwnerLifecycleTests(unittest.TestCase):

    def test_state_without_owner_record_is_not_reported_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            location = home / 'profiles' / 'desktop'
            location.mkdir(parents=True)
            (location / 'state.json').write_text('{"status":"READY"}')
            with self.assertRaisesRegex(ValueError, 'owner record missing'):
                LocalProfileOwners().alive(home, 'desktop')

    def test_incomplete_legacy_identity_never_reports_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            location = home / 'profiles' / 'desktop'
            location.mkdir(parents=True)
            for value in ({'pid': 1}, {'process_start': 1}, {'pid': True, 'process_start': 1}):
                with self.subTest(value=value):
                    (location / 'state.json').write_text(json.dumps(value))
                    with LocalProfileLeases().acquire(home, 'desktop', kind='spawn') as lease:
                        self.assertTrue(lease)
                        with self.assertRaisesRegex(ValueError, 'invalid legacy profile owner identity'):
                            LocalProfileOwners().alive(home, 'desktop')

    def test_host_tracks_real_owner_and_rejects_reused_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            location = home / 'profiles' / 'desktop'
            location.mkdir(parents=True)
            state = location / 'state.json'
            code = ("from mioa_os import LocalProfileOwners; import sys; "
                    "LocalProfileOwners().publish(sys.argv[1], 'desktop'); "
                    "print('ready', flush=True); sys.stdin.read()")
            process = subprocess.Popen([sys.executable, '-c', code, str(home)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                owners = LocalProfileOwners()
                self.assertFalse(owners.alive(home, 'desktop'))
                state.write_text('{"status":"READY"}')
                self.assertTrue(owners.alive(home, 'desktop'))
                state.unlink()
                self.assertFalse(owners.alive(home, 'desktop'))
                state.write_text('{"status":"READY"}')
                record = next((home / '.owners').glob('*.json'))
                identity = json.loads(record.read_text())
                record.write_text(json.dumps({**identity, 'process_start': identity['process_start'] + 1}))
                self.assertFalse(owners.alive(home, 'desktop'))
                record.write_text(json.dumps(identity))
                process.terminate()
                process.wait(timeout=5)
                self.assertFalse(owners.alive(home, 'desktop'))
                state.write_text(json.dumps({'pid': os.getpid(),
                                             'process_start': psutil.Process().create_time()}))
                self.assertTrue(owners.alive(home, 'desktop'))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()
