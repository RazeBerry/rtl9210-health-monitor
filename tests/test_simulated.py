"""Offline tests. Never run the front door, native probe, diskutil or sudo."""
import ast
import copy
import importlib.util
import json
import os
import plistlib
import runpy
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    with patch('signal.signal'):
        spec.loader.exec_module(mod)
    return mod

G = module('volume_guard')
B = module('bounded')
VOLUMES = [dict(VolumeUUID='11111111-1111-1111-1111-111111111111', VolumeName='Example Data',
                MountPoint='/Volumes/Example Data', FilesystemType='apfs'),
           dict(VolumeUUID='22222222-2222-2222-2222-222222222222', VolumeName='Example Exchange',
                MountPoint='/Volumes/Example Exchange', FilesystemType='exfat')]

class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rtl-mock-', dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'manifest.json'
        self.path.write_text(json.dumps(VOLUMES))
        self.data = {'/dev/disk9': dict(WholeDisk=True, DeviceIdentifier='disk9')}
        for index, volume in enumerate(VOLUMES):
            info = dict(volume, Mounted=True)
            info.update(APFSPhysicalStores=[dict(APFSPhysicalStore='disk9s2')]) if index == 0 else info.update(ParentWholeDisk='disk9')
            self.data[volume['VolumeUUID']] = info
        self.deny = patch.object(G.subprocess, 'run', side_effect=AssertionError('Real subprocess forbidden'))
        self.deny.start(); self.addCleanup(self.deny.stop)
        self.mock = patch.object(G, 'query', side_effect=self.query)
        self.mock.start(); self.addCleanup(self.mock.stop)
    def query(self, *args):
        if args == ('list', '-plist', '/dev/disk9'):
            return {'AllDisks': ['disk9','disk9s1','disk9s2','disk9s3']}
        if args == ('apfs', 'list', '-plist'):
            return {'Containers': [{'PhysicalStores':[{'DeviceIdentifier':'disk9s2'}], 'Volumes':[{'DeviceIdentifier':'disk10s1'}]}]}
        if args[:2] == ('info', '-plist'):
            if args[2] in ('disk9s1','disk9s2'): return {'DeviceIdentifier':args[2],'ParentWholeDisk':'disk9','Mounted':False}
            target={'disk9':'/dev/disk9','disk9s3':VOLUMES[1]['VolumeUUID'],'disk10s1':VOLUMES[0]['VolumeUUID']}.get(args[2],args[2])
            if target not in self.data: raise RuntimeError('not discovered')
            return copy.deepcopy(self.data[target])
        raise AssertionError(args)
    def test_snapshot_and_verify(self):
        self.path.unlink(); G.snapshot('/dev/disk9', self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        G.verify('/dev/disk9', self.path)
    def test_missing_second_volume(self):
        del self.data[VOLUMES[1]['VolumeUUID']]
        with self.assertRaises(RuntimeError): G.snapshot('/dev/disk9', self.path)
    def test_disappeared_physical_disk(self):
        del self.data['/dev/disk9']
        with self.assertRaises(RuntimeError): G.mounted_on_disk('/dev/disk9')
    def test_identity_absent_is_retryable(self):
        del self.data[VOLUMES[0]['VolumeUUID']]
        with self.assertRaises(SystemExit) as exc: G.verify_identity('/dev/disk9', self.path)
        self.assertEqual(exc.exception.code, 75)
    def test_incomplete_metadata_retryable(self):
        del self.data[VOLUMES[0]['VolumeUUID']]['FilesystemType']
        with self.assertRaises(SystemExit) as exc: G.verify_identity('/dev/disk9', self.path)
        self.assertEqual(exc.exception.code, 75)
    def test_wrong_store_rejected(self):
        self.data[VOLUMES[0]['VolumeUUID']]['APFSPhysicalStores'][0]['APFSPhysicalStore'] = 'disk90s2'
        with self.assertRaises(RuntimeError): G.verify_identity('/dev/disk9', self.path)
    def test_wrong_name_rejected(self):
        self.data[VOLUMES[1]['VolumeUUID']]['VolumeName'] = 'other'
        with self.assertRaises(RuntimeError): G.verify_identity('/dev/disk9', self.path)
    def test_changed_mount_rejected(self):
        self.data[VOLUMES[1]['VolumeUUID']]['MountPoint'] += ' 1'
        with self.assertRaises(RuntimeError): G.verify('/dev/disk9', self.path)
    def test_partial_mount_rejected(self):
        self.data[VOLUMES[1]['VolumeUUID']]['Mounted'] = False
        with self.assertRaises(RuntimeError): G.verify('/dev/disk9', self.path)
    def test_unmounted_refuses_mounted_volume(self):
        with patch.object(sys, 'argv', ['guard', 'unmounted', '/dev/disk9', str(self.path)]):
            with self.assertRaises(RuntimeError): G.main()
    def test_unmounted_accepts_both_unmounted(self):
        for v in VOLUMES:
            self.data[v['VolumeUUID']]['Mounted'] = False
            self.data[v['VolumeUUID']].pop('MountPoint',None)
        with patch.object(sys, 'argv', ['guard', 'unmounted', '/dev/disk9', str(self.path)]): G.main()
    def test_empty_manifest_rejected(self):
        self.path.write_text('[]')
        with self.assertRaises(RuntimeError): G.verify_identity('/dev/disk9', self.path)

    def test_duplicate_manifest_rejected(self):
        self.path.write_text(json.dumps([VOLUMES[0], VOLUMES[0]]))
        with self.assertRaises(RuntimeError): G.verify_identity('/dev/disk9', self.path)
    def test_malformed_manifest_rejected(self):
        for data in (None, {}, [1, 2], [{}, {}]):
            with self.subTest(data=data):
                self.path.write_text(json.dumps(data))
                with self.assertRaises(RuntimeError): G.verify_identity('/dev/disk9', self.path)
    def test_duplicate_uuid_snapshot_rejected(self):
        self.data[VOLUMES[1]['VolumeUUID']]['VolumeUUID'] = VOLUMES[0]['VolumeUUID']
        with self.assertRaises(RuntimeError): G.snapshot('/dev/disk9', self.path)
    def test_invalid_disk_argument_rejected(self):
        with patch.object(sys, 'argv', ['guard', 'unmounted', '/dev/disk9s2', str(self.path)]):
            with self.assertRaises(RuntimeError): G.main()
    def test_apfs_multiple_stores_rejected(self):
        self.data[VOLUMES[0]['VolumeUUID']]['APFSPhysicalStores'].append(dict(APFSPhysicalStore='disk8s2'))
        with self.assertRaises(RuntimeError): G.verify_identity('/dev/disk9', self.path)

    def test_snapshot_rejects_incomplete_manifest_before_writing(self):
        self.path.unlink()
        del self.data[VOLUMES[1]['VolumeUUID']]['FilesystemType']
        with self.assertRaises(RuntimeError): G.snapshot('/dev/disk9',self.path)
        self.assertFalse(self.path.exists())
    def test_snapshot_rejects_malformed_uuid_before_writing(self):
        self.path.unlink()
        self.data[VOLUMES[1]['VolumeUUID']]['VolumeUUID']='invalid'
        with self.assertRaises(RuntimeError): G.snapshot('/dev/disk9',self.path)
        self.assertFalse(self.path.exists())

    def test_unrelated_disks_never_queried(self):
        original=self.query
        seen=[]
        def inventory(*args):
            seen.append(args)
            value=original(*args)
            if args==('apfs','list','-plist'):
                value['Containers'] += [{'PhysicalStores':[{'DeviceIdentifier':'disk888s2'}],
                                        'Volumes':[{'DeviceIdentifier':'disk999s1'}]}]*100
            return value
        with patch.object(G,'query',side_effect=inventory): G.verify('/dev/disk9',self.path)
        self.assertNotIn(('info','-plist','disk999s1'),seen)
        self.assertNotIn(('list','-plist'),seen)
    def test_target_inventory_query_failure_still_blocks(self):
        original=self.query
        def inventory(*args):
            if args==('info','-plist','disk10s1'): raise RuntimeError('target disappeared')
            return original(*args)
        with patch.object(G,'query',side_effect=inventory):
            with self.assertRaises(RuntimeError): G.verify('/dev/disk9',self.path)

    def test_incomplete_target_apfs_topology_never_proves_unmounted(self):
        original=self.query
        def inventory(*args):
            if args==('apfs','list','-plist'): return {'Containers':[]}
            return original(*args)
        with patch.object(G,'query',side_effect=inventory):
            with self.assertRaises(RuntimeError): G.mounted_on_disk('/dev/disk9')

    def test_arbitrary_volume_count_and_labels(self):
        third = dict(VolumeUUID='33333333-3333-3333-3333-333333333333',
                     VolumeName='Example 数据', MountPoint='/Volumes/Example 数据', FilesystemType='apfs')
        for volumes in ([VOLUMES[0]], VOLUMES, [*VOLUMES, third]):
            with self.subTest(count=len(volumes)), patch.object(G, 'mounted_on_disk', return_value=volumes):
                self.path.unlink(missing_ok=True)
                G.snapshot('/dev/disk9', self.path, VOLUMES[0]['VolumeUUID'])
                self.assertEqual(G.load_manifest(self.path), volumes)
                G.verify('/dev/disk9', self.path)

    def test_missing_anchor_rejected_before_manifest_write(self):
        self.path.unlink()
        with self.assertRaises(RuntimeError):
            G.snapshot('/dev/disk9', self.path, '33333333-3333-3333-3333-333333333333')
        self.assertFalse(self.path.exists())

    def test_unmounted_data_volume_refused_before_capture(self):
        self.path.unlink()
        self.data[VOLUMES[1]['VolumeUUID']]['Mounted'] = False
        with self.assertRaises(RuntimeError): G.snapshot('/dev/disk9', self.path)
        self.assertFalse(self.path.exists())

    def test_locked_apfs_volume_refused_before_capture(self):
        self.path.unlink()
        self.data[VOLUMES[0]['VolumeUUID']].update(Mounted=False, FilesystemType='')
        with self.assertRaises(RuntimeError): G.snapshot('/dev/disk9', self.path)
        self.assertFalse(self.path.exists())

    def test_snapshot_refuses_incomplete_apfs_store(self):
        self.path.unlink()
        del self.data[VOLUMES[0]['VolumeUUID']]['APFSPhysicalStores']
        with self.assertRaises(RuntimeError): G.snapshot('/dev/disk9', self.path)
        self.assertFalse(self.path.exists())

    def test_duplicate_mount_paths_rejected(self):
        volumes = copy.deepcopy(VOLUMES)
        volumes[1]['MountPoint'] = volumes[0]['MountPoint']
        with self.assertRaises(RuntimeError): G.validate_manifest(volumes)

    def test_unsafe_mount_paths_rejected(self):
        for path in ('/Volumes/', '/Volumes/../tmp', '/Volumes/data/../other', '/Volumes/data\nother'):
            volumes = copy.deepcopy(VOLUMES); volumes[0]['MountPoint'] = path
            with self.subTest(path=path), self.assertRaises(RuntimeError): G.validate_manifest(volumes)

    def test_duplicate_labels_allowed_with_distinct_uuid_and_path(self):
        volumes = copy.deepcopy(VOLUMES); volumes[1]['VolumeName'] = volumes[0]['VolumeName']
        self.assertEqual(G.validate_manifest(volumes), volumes)

    def test_macos_mountpoint_without_mounted_flag(self):
        for info in self.data.values(): info.pop('Mounted',None)
        self.path.unlink()
        G.snapshot('/dev/disk9',self.path,VOLUMES[0]['VolumeUUID'])
        G.verify('/dev/disk9',self.path)

    def test_macos_empty_mountpoint_proves_unmounted(self):
        for volume in VOLUMES:
            info=self.data[volume['VolumeUUID']]
            info.pop('Mounted',None);info['MountPoint']=''
        with patch.object(sys,'argv',['guard','unmounted','/dev/disk9',str(self.path)]): G.main()

    def test_unknown_mount_metadata_never_proves_unmounted(self):
        for info in ({},{'MountPoint':None},{'Mounted':True}):
            with self.subTest(info=info),self.assertRaises(RuntimeError):G.is_mounted(info)

    def test_conflicting_mount_metadata_rejected(self):
        for info in ({'MountPoint':'/Volumes/Example','Mounted':False},
                     {'MountPoint':'','Mounted':True},{'MountPoint':'','Mounted':0}):
            with self.subTest(info=info),self.assertRaises(RuntimeError):G.is_mounted(info)

    def test_macos_unmounted_data_volume_refused_before_capture(self):
        info=self.data[VOLUMES[1]['VolumeUUID']]
        info.pop('Mounted',None);info['MountPoint']=''
        with self.assertRaises(RuntimeError):G.snapshot('/dev/disk9',self.path)

class FakeChild:
    pid = 987654
    def __init__(self, running=False, stuck=False):
        self.running, self.stuck = running, stuck
        self.waited = False
    def poll(self): return None if self.running else 0
    def wait(self, timeout=None):
        if self.stuck: raise subprocess.TimeoutExpired('fake-child', timeout)
        self.waited = True; self.running = False
        return 0

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rtl-supervisor-mock-', dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.receipt = Path(self.tmp.name) / 'exited'
        B.cancelled = False
    def run_fake(self, child, seconds='0'):
        with patch.object(sys, 'argv', ['bounded', seconds, str(self.receipt), 'FAKE-NOT-EXECUTED']), \
             patch.object(B.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(B.os, 'killpg') as kill, patch.object(B.time, 'sleep'):
            result = B.main()
        return result, spawn, kill
    def test_normal_receipt_after_wait(self):
        child = FakeChild(); rc, spawn, kill = self.run_fake(child)
        self.assertEqual(rc, 0); self.assertTrue(child.waited)
        self.assertTrue(self.receipt.exists()); kill.assert_not_called()
    def test_timeout_reaped(self):
        child = FakeChild(running=True); rc, _, kill = self.run_fake(child)
        self.assertEqual(rc, 124); self.assertTrue(child.waited)
        self.assertTrue(self.receipt.exists()); kill.assert_called_once_with(child.pid, signal.SIGTERM)
    def test_unreapable_no_receipt(self):
        rc, _, kill = self.run_fake(FakeChild(running=True, stuck=True))
        self.assertEqual(rc, 125); self.assertEqual(kill.call_count, 2)
        self.assertFalse(self.receipt.exists())
    def test_cancel_before_spawn(self):
        B.cancelled = True
        rc, spawn, _ = self.run_fake(FakeChild())
        spawn.assert_not_called(); self.assertNotEqual(rc, 0)


    def test_stale_receipt_does_not_spawn(self):
        self.receipt.write_text('old')
        with patch.object(B.subprocess, 'Popen') as spawn:
            with patch.object(sys, 'argv', ['bounded', '1', str(self.receipt), 'FAKE']):
                with self.assertRaises(RuntimeError): B.main()
            spawn.assert_not_called()
    def test_nonzero_child_still_reaped(self):
        child=FakeChild(); child.wait=lambda timeout=None: 7
        rc, _, _ = self.run_fake(child)
        self.assertEqual(rc,7); self.assertTrue(self.receipt.exists())
    def test_kill_after_term_ignored(self):
        child=FakeChild(running=True)
        calls=[]
        def wait(timeout=None):
            calls.append(timeout)
            if len(calls)==1: raise subprocess.TimeoutExpired('fake',timeout)
            return -9
        child.wait=wait
        rc, _, kill=self.run_fake(child)
        self.assertEqual(rc,124); self.assertEqual(kill.call_count,2)
        self.assertTrue(self.receipt.exists())
    def test_cancel_during_child(self):
        child=FakeChild(running=True)
        def poll():
            B.cancelled=True
            return None
        child.poll=poll
        rc, _, kill=self.run_fake(child,seconds='100')
        self.assertEqual(rc,124); self.assertTrue(self.receipt.exists())
        kill.assert_called_once()
    def test_native_child_has_cooperative_release_grace(self):
        self.receipt=Path(self.tmp.name)/'child-exited'
        child=FakeChild(running=True)
        waits=[]
        def wait(timeout=None):
            waits.append(timeout)
            child.running=False
            return 143
        child.wait=wait
        rc, _, kill=self.run_fake(child)
        self.assertEqual(rc,124);self.assertEqual(waits[0],30)
        kill.assert_called_once_with(child.pid,signal.SIGTERM)
        self.assertTrue(self.receipt.exists())
    def test_spawn_failure_no_receipt(self):
        with patch.object(sys,'argv',['bounded','1',str(self.receipt),'FAKE']), patch.object(B.subprocess,'Popen',side_effect=OSError('simulated')):
            with self.assertRaises(OSError): B.main()
        self.assertFalse(self.receipt.exists())

    def test_permission_denied_never_claims_child_exit(self):
        child=FakeChild(running=True)
        with patch.object(sys,'argv',['bounded','0',str(self.receipt),'FAKE']), \
             patch.object(B.subprocess,'Popen',return_value=child), \
             patch.object(B.os,'killpg',side_effect=PermissionError('different UID')):
            self.assertEqual(B.main(),125)
        self.assertFalse(self.receipt.exists());self.assertFalse(child.waited)
    def test_cancelled_receipt_is_not_disk_operation_completion(self):
        rc, _, _=self.run_fake(FakeChild(running=True))
        data=json.loads(self.receipt.read_text())
        self.assertEqual(rc,124);self.assertTrue(data['reaped'])
        self.assertFalse(data['normal_exit'])

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='rtl-storage-mock-',dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
    def run_guard(self, internal=True, df_output=None, info_override=None, fail_command=None):
        self.calls=[]
        def fake(command, **kwargs):
            self.calls.append(command)
            if command[0] == fail_command:
                raise subprocess.CalledProcessError(1, command)
            if command[0] == '/bin/df':
                self.assertTrue(Path(command[-1]).exists())
                self.assertEqual(kwargs['env']['LC_ALL'], 'C')
                output = df_output if df_output is not None else (
                    b'Filesystem 512-blocks Used Available Capacity Mounted on\n'
                    b'/dev/disk3s5 100 20 80 20% /System/Volumes/Data\n')
                return subprocess.CompletedProcess(command,0,stdout=output)
            self.assertEqual(command, ['/usr/sbin/diskutil','info','-plist','/dev/disk3s5'])
            info = {'Internal':internal, 'DeviceNode':'/dev/disk3s5'}
            if info_override is not None: info = info_override
            return subprocess.CompletedProcess(command,0,stdout=plistlib.dumps(info))
        with patch.object(sys,'argv',['storage_guard',str(self.root)]), patch('subprocess.run',side_effect=fake):
            runpy.run_path(str(ROOT/'scripts/storage_guard.py'),run_name='__main__')
        self.assertTrue(self.calls)
    def test_internal_paths(self): self.run_guard()
    def test_external_root_refused(self):
        with self.assertRaises(SystemExit): self.run_guard(False)
    def test_symlinked_output_refused(self):
        (self.root/'runs').symlink_to(self.root)
        with self.assertRaises(SystemExit): self.run_guard()
    def test_symlinked_helper_parent_refused(self):
        (self.root/'scripts').symlink_to(self.root)
        with self.assertRaises(SystemExit): self.run_guard()

    def test_plain_directory_with_spaces_and_unicode(self):
        self.root=self.root/'Example 数据';self.root.mkdir()
        self.run_guard()
        self.assertEqual(self.calls[0][-1],str(self.root))

    def test_missing_outputs_use_existing_parent(self):
        self.run_guard()
        self.assertFalse((self.root/'runs').exists())
        self.assertTrue(all(Path(c[-1]).exists() for c in self.calls if c[0]=='/bin/df'))

    def test_firmlink_uses_df_device_not_root_mount(self):
        self.run_guard()
        self.assertTrue(all(c[-1]=='/dev/disk3s5' for c in self.calls if c[0]=='/usr/sbin/diskutil'))

    def test_mount_name_spaces_do_not_affect_device(self):
        self.run_guard(df_output=b'Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/disk3s5 100 20 80 20% /Volumes/Example Data\n')

    def test_missing_internal_flag_rejected(self):
        with self.assertRaises(SystemExit): self.run_guard(info_override={'DeviceNode':'/dev/disk3s5'})

    def test_mismatched_device_rejected(self):
        with self.assertRaises(SystemExit): self.run_guard(info_override={'Internal':True,'DeviceNode':'/dev/disk8s1'})

    def test_malformed_df_rejected(self):
        for output in (b'',b'header\n',b'Filesystem\n\n',b'Filesystem\n/dev/disk3s5 1\nextra\n',b'Filesystem\nserver:/share 1\n'):
            with self.subTest(output=output), self.assertRaises(SystemExit): self.run_guard(df_output=output)

    def test_df_failure_blocks_disk_query(self):
        with self.assertRaises(subprocess.CalledProcessError): self.run_guard(fail_command='/bin/df')
        self.assertFalse(any(c[0]=='/usr/sbin/diskutil' for c in self.calls))

    def test_diskutil_failure_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError): self.run_guard(fail_command='/usr/sbin/diskutil')

    def test_dangling_output_symlink_refused(self):
        (self.root/'runs').symlink_to(self.root/'missing')
        with self.assertRaises(SystemExit): self.run_guard()

# Extract ONLY these two production functions; no top-level/front-door evaluation.
# All external operations they reference are replaced by Bash builtins below.
def function(name):
    source = (ROOT / 'rtl9210-health').read_text()
    start = source.index(name + '() {')
    return source[start:source.index('\n}\n', start) + 3]

class RecoveryTests(unittest.TestCase):
    def simulate(self, scenario, interrupt_transition=False):
        restore_source=function('restore_disk')
        if interrupt_transition:
            restore_source=restore_source.replace('    NEEDS_RESTORE=0', '    NEEDS_RESTORE=0\n    kill -TERM $$', 1)
        script = '''set -u
NEEDS_RESTORE=1; PROBE_PENDING=0; MUTATION_PENDING=0; RECOVERY_FAILED=0
PROBE_RECEIPT=/nonexistent-test-receipt; MUTATION_RECEIPT=/nonexistent-mutation-receipt; PYTHON=FAKE; BOUNDED=FAKE; VOLUME_GUARD=FAKE; VOLUME_MANIFEST=FAKE
CAPTURE_LOG=""; ROOT=FAKE; WORKER_STARTED=0; LOCK_HELD=1; mounts=0; identities=0; verifies=0
resolve_disk() { printf '/dev/disk9\\n'; }
sleep() { SECONDS=$((SECONDS+100)); }
mutate_disk() { mounts=$((mounts+1)); return 0; }
FAKE() { return 1; }
settle_worker() { return 0; }
rmdir() { printf 'LOCK_REMOVED\\n'; }
bounded() {
 case "$5" in
 identity) identities=$((identities+1)); return 0;;
 verify) verifies=$((verifies+1)); return 0;;
 *) printf 'Unexpected mock operation' >&2; exit 98;;
 esac
}
'''+restore_source+'\n'+function('cleanup')+'\n'+scenario+'''
rc=0; restore_disk || rc=$?
printf 'RESULT=%s MOUNTS=%s IDENTITIES=%s VERIFY=%s FAILED=%s\\n' "$rc" "$mounts" "$identities" "$verifies" "$RECOVERY_FAILED"
'''
        # Bash receives only extracted functions and mocks; PATH prevents external lookup.
        return subprocess.run(['/bin/bash', '-c', script], env={'PATH':'/nonexistent'}, capture_output=True, text=True, timeout=5)
    def test_success(self):
        r=self.simulate(''); self.assertEqual(r.returncode,0,r.stderr); self.assertIn('RESULT=0 MOUNTS=1',r.stdout)
    def test_no_exit_receipt_no_mount(self):
        r=self.simulate('PROBE_PENDING=1'); self.assertIn('RESULT=1 MOUNTS=0',r.stdout)
    def test_pending_mutation_no_mount(self):
        r=self.simulate('MUTATION_PENDING=1'); self.assertIn('RESULT=1 MOUNTS=0',r.stdout)
    def test_identity_mismatch_no_mount(self):
        r=self.simulate('bounded() { return 1; }'); self.assertIn('RESULT=1 MOUNTS=0',r.stdout)
    def test_retry_delayed_identity(self):
        r=self.simulate('''sleep() { :; }
bounded() { if [[ "$5" == identity ]]; then identities=$((identities+1)); [[ $identities -ge 3 ]]; if [[ $identities -lt 3 ]]; then return 75; fi; fi; return 0; }''')
        self.assertIn('RESULT=0 MOUNTS=1 IDENTITIES=3',r.stdout)
    def test_discovery_deadline_no_mount(self):
        r=self.simulate('resolve_disk() { return 1; }'); self.assertIn('RESULT=1 MOUNTS=0',r.stdout)
    def test_partial_recovery_fails(self):
        r=self.simulate('bounded() { [[ "$5" == identity ]]; }'); self.assertIn('RESULT=1 MOUNTS=1',r.stdout)
    def test_mount_failure_but_verified_success(self):
        r=self.simulate('mutate_disk() { mounts=$((mounts+1)); return 1; }'); self.assertIn('RESULT=0 MOUNTS=1',r.stdout)
    def test_unconfirmed_mount_aborts_verify(self):
        r=self.simulate('mutate_disk() { mounts=$((mounts+1)); MUTATION_PENDING=1; return 125; }')
        self.assertIn('RESULT=1 MOUNTS=1 IDENTITIES=1 VERIFY=0',r.stdout)

    def test_cleanup_keeps_lock_on_failure(self):
        r=self.simulate('PROBE_PENDING=1; cleanup')
        self.assertEqual(r.returncode,70); self.assertNotIn('LOCK_REMOVED',r.stdout)
    def test_cleanup_removes_lock_on_success(self):
        r=self.simulate('cleanup')
        self.assertEqual(r.returncode,0); self.assertIn('LOCK_REMOVED',r.stdout)
    def test_cleanup_keeps_prior_failure_lock(self):
        r=self.simulate('NEEDS_RESTORE=0; RECOVERY_FAILED=1; cleanup')
        self.assertNotIn('LOCK_REMOVED',r.stdout)
        self.assertEqual(r.returncode,70)
    def test_cleanup_preserves_original_failure(self):
        r=self.simulate('NEEDS_RESTORE=0; false; cleanup')
        self.assertEqual(r.returncode,1)

    def test_interrupted_recovery_keeps_lock_and_fails(self):
        r=self.simulate("bounded() { return 75; }; sleep() { exit 130; }; trap cleanup EXIT")
        self.assertEqual(r.returncode,70); self.assertNotIn('LOCK_REMOVED',r.stdout)
    def test_failed_identity_marks_incomplete(self):
        r=self.simulate('bounded() { return 1; }')
        self.assertIn('FAILED=1',r.stdout)

    def test_exact_transition_signal_preserves_incomplete_lock(self):
        r=self.simulate("trap cleanup EXIT; trap 'exit 130' TERM", interrupt_transition=True)
        self.assertEqual(r.returncode,70,r.stderr)
        self.assertNotIn('LOCK_REMOVED',r.stdout)

if __name__ == '__main__': unittest.main(verbosity=2)
