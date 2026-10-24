"""Full front-door control flow in disposable copies with simulated OS/USB boundaries."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UUID='11111111-1111-1111-1111-111111111111'
PROFILE=dict(media_name='RTL9210 MOCK',physical_size_bytes=2000000000000,
 volume_uuid=UUID,mount_path='/Volumes/Example Data',usb_vid='0x0bda',usb_pid='0x9210',
 usb_bcd='0x2151',usb_serial='MOCK',nvme_model='MOCK NVME',nvme_serial='MOCK-SSD')

# This file is the only replacement for diskutil/sudo/tmutil/xcrun/chown/native.
# No forwarding fallback exists for hardware, privilege or compiler operations.
FAKE = r'''import json,os,plistlib,sys,time,signal
from pathlib import Path
base=Path(os.environ['MOCK_ROOT'])
scenario=os.environ['MOCK_SCENARIO']
name=Path(sys.argv[0]).name
args=sys.argv[1:]
def event(value):
 with (base/'events').open('a') as f: f.write(value+'\n')
def state(): return json.loads((base/'state.json').read_text())
def save(s): (base/'state.json').write_text(json.dumps(s))
def plist(d): sys.stdout.buffer.write(plistlib.dumps(d))
event(name+' '+ ' '.join(args))
if name=='sudo':
 if scenario=='auth_fail': sys.exit(1)
 if scenario=='launcher_eperm': time.sleep(0.5)
 assert args[0]=='-A' and '-v' not in args
 assert state()['mounted'], 'authorization requested while unmounted'
 cmd=args[1:]
 assert os.path.realpath(cmd[0])==os.path.realpath(sys.executable)
 assert cmd[2]==str(base/'scripts/privileged_worker.py')
 # A separate monitor and separate session model real sudo more closely than exec.
 import subprocess
 env=dict(os.environ,MOCK_PRIVILEGED='0' if scenario=='worker_not_privileged' else '1')
 child=subprocess.Popen(cmd,env=env,start_new_session=True)
 event('PRIVILEGED_WORKER_STARTED')
 if scenario=='launcher_lost':
  while not list((base/'runs').glob('*/worker-ready.json')): time.sleep(0.01)
  sys.exit(1)
 sys.exit(child.wait())
elif name=='daemon':
 assert args[0] in ('unmountDisk','mountDisk')
 time.sleep(0.5)
 s=state();s['mounted']=args[0]=='mountDisk';s['daemon_completed']=True;save(s)
 event('DAEMON_COMPLETED '+args[0])
elif name=='plutil':
 assert args[-1]=='-' or args[-1].startswith(str(base)), args
 if args[-1].endswith('/release-status.plist') and scenario=='late_exfat_loss':
  s=state();s['late_loss']=True;save(s)
 if args[-1].endswith('/release-status.plist') and not state().get('chown_done'):
  event('RELEASE_PERMISSION_DENIED');sys.exit(13)
 os.execv('/usr/bin/plutil',['/usr/bin/plutil',*args])
elif name=='chown':
 s=state();s['chown_done']=True;save(s)
 if scenario=='late_exfat_loss':
  s=state();s['late_loss']=True;save(s)
 sys.exit(1 if scenario=='chown_fail' else 0)
elif name=='tmutil': print('Running = '+('1;' if scenario=='backup_busy' else '0;'))
elif name=='xcrun':
 if scenario=='build_fail': sys.exit(1)
 target=Path(args[args.index('-o')+1]); assert target==base/'.build/rtl9210_health_probe'
 target.write_text((base/'fakebin/native').read_text()); target.chmod(0o700)
elif name in ('native','rtl9210_health_probe'):
 s=state()
 if args[0]=='binding':
  s['bindings']=s.get('bindings',0)+1; save(s)
  print(999 if scenario=='binding_changed' and s['bindings']>1 else 123)
  sys.exit(0)
 assert args[0] in ('probe','enroll')
 s['native_entered']=True;save(s)
 event('NATIVE_ENTER'); assert not s['mounted']; assert args[-2]=='/dev/disk9' and args[-1]=='123'
 if scenario=='cancel_running_probe':
  observed=[False]
  signal.signal(signal.SIGTERM,lambda signum,frame: observed.__setitem__(0,True))
  os.kill(int(os.environ['MOCK_FRONT_PID']),signal.SIGTERM)
  until=time.monotonic()+8
  while not observed[0] and time.monotonic()<until: time.sleep(0.02)
  assert observed[0], 'root owner did not relay cancellation'
  event('NATIVE_CANCELLED_BY_OWNER')
 if scenario=='native_timeout': time.sleep(10)
 if scenario in ('signal_probe','signal_probe_no_receipt'):
  os.kill(int(os.environ['MOCK_FRONT_PID']),signal.SIGTERM)
 if scenario=='native_fail': sys.exit(9)
 out=Path(args[1]); assert out.parent==base/'runs'
 for filename in ('identify_controller.bin','smart_health_log.bin','error_info_log.bin'):
  (out/filename).write_bytes(b'SIMULATED')
 if args[0]=='enroll' and scenario!='missing_enrollment':
  profile=json.loads((base/'profile-fixture.json').read_text())
  if scenario=='invalid_enrollment': profile['nvme_serial']=''
  (out/'enrollment.plist').write_bytes(plistlib.dumps(profile))
 if scenario!='missing_release':
  (out/'release-status.plist').write_bytes(plistlib.dumps({'release_path_completed':True,'capture_acquired':True,'cancel_signal':15 if scenario=='native_cancel_cooperative' else 0}))
 print('SIMULATED HEALTH'+('x'*2000000 if scenario=='blocked_output' else ''))
 if scenario in ('native_cancel_cooperative','cancel_running_probe'): sys.exit(143)
elif name=='df':
 assert args[0]=='-P' and Path(args[1]).exists()
 print('Filesystem 512-blocks Used Available Capacity Mounted on')
 print('/dev/disk3s5 100 20 80 20% /System/Volumes/Data')
elif name=='diskutil':
 s=state()
 if args[0] in ('unmountDisk','mountDisk'):
  assert args[1]=='/dev/disk9'
  event('MUTATION '+args[0])
  if scenario=='async_'+args[0]:
   import subprocess
   subprocess.Popen([str(base/'fakebin/daemon'),args[0]],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   s['attempted']=True;save(s)
   time.sleep(10)
  if args[0]=='unmountDisk':
   s['attempted']=True
   if scenario!='unmount_fail': s['mounted']=False
   save(s)
   if scenario=='signal_unmount': os.kill(int(os.environ['MOCK_FRONT_PID']),signal.SIGTERM)
   if scenario=='unmount_fail': sys.exit(1)
  else:
   s['mounted']=True; s['restored']=True; save(s)
   if scenario=='mount_error_but_restored': sys.exit(1)
  sys.exit(0)
 if args==['list','external','physical']:
  if not(scenario=='disk_missing' and s.get('attempted')): print('/dev/disk9 (external, physical):')
 elif args==['list','-plist','/dev/disk9']: plist({'AllDisks':['disk9','disk9s1','disk9s2']+([] if scenario=='single_volume' else ['disk9s3'])+(['disk9s4'] if scenario in ('three_volumes','third_volume_loss') else [])})
 elif args==['apfs','list','-plist']: plist({'Containers':[{'PhysicalStores':[{'DeviceIdentifier':'disk9s2'}], 'Volumes':[{'DeviceIdentifier':'disk10s1'}]}]})
 elif args[:2]==['info','-plist']:
  target=args[2]
  if target=='/dev/disk3s5':
   plist({'Internal':scenario!='external_storage','DeviceNode':target}); sys.exit(0)
  assert not target.startswith((str(base),'/opt/','/usr/','/Library/','/System/')), 'diskutil cannot resolve ordinary paths'
  if target in ('disk9','/dev/disk9'):
   plist(dict(WholeDisk=True,DeviceIdentifier='disk9',MediaName='RTL9210 MOCK',IOKitSize=2000000000000,BusProtocol='USB'));sys.exit(0)
  if target in ('disk9s1','disk9s2'):
   plist({'DeviceIdentifier':target,'ParentWholeDisk':'disk9','Mounted':False});sys.exit(0)
  third=target in ('disk9s4','33333333-3333-3333-3333-333333333333')
  second=target in ('disk9s3','22222222-2222-2222-2222-222222222222')
  assert second or third or target in ('disk10s1','11111111-1111-1111-1111-111111111111','/Volumes/Example Data'), target
  if scenario=='delayed_identity' and s.get('native_entered') and not s.get('restored'):
   n=s.get('identity_attempts',0);s['identity_attempts']=n+1;save(s)
   if n<2: sys.exit(1)
  info=dict(VolumeUUID='22222222-2222-2222-2222-222222222222' if second else '11111111-1111-1111-1111-111111111111',
   VolumeName='Example Exchange' if second else 'Example Data',FilesystemType='exfat' if second else 'apfs',MountPoint='')
  if third: info.update(VolumeUUID='33333333-3333-3333-3333-333333333333',VolumeName='Example 数据',FilesystemType='exfat')
  if s['mounted']: info['MountPoint']='/Volumes/'+info['VolumeName']
  if second or third: info['ParentWholeDisk']='disk9'
  else: info['APFSPhysicalStores']=[{'APFSPhysicalStore':'disk9s2'}]
  if scenario=='third_volume_loss' and third and s.get('restored'): info['MountPoint']=''
  if scenario=='identity_mismatch' and s.get('attempted'): info['VolumeName']='REPLACED'
  if scenario=='snapshot_invalid' and second: info.pop('FilesystemType',None)
  if s.get('late_loss') and second: info['MountPoint']=''
  if scenario=='snapshot_missing' and second: info['MountPoint']=''
  if scenario=='partial_restore' and second and s.get('restored'): info['MountPoint']=''
  plist(info)
 else: raise AssertionError(args)
else: raise AssertionError(name)
'''

class WorkflowTests(unittest.TestCase):
 def run_case(self,mode,scenario,replace=False):
  roundtrip = mode=='roundtrip'
  if roundtrip: mode='setup'
  with tempfile.TemporaryDirectory(prefix='rtl-full-mock-',dir='/private/tmp') as directory:
   root=Path(directory);(root/'scripts').mkdir();(root/'src').mkdir();(root/'fakebin').mkdir()
   (root/'profile-fixture.json').write_text(json.dumps(PROFILE))
   (root/'state.json').write_text(json.dumps({'mounted':True}))
   for name in ('diskutil','sudo','chown','tmutil','xcrun','native','plutil','daemon','df'):
    p=root/'fakebin'/name;p.write_text('#!'+sys.executable+'\n'+FAKE);p.chmod(0o700)
   source=(ROOT/'rtl9210-health').read_text()
   for command in ('diskutil','sudo','chown','tmutil','xcrun','plutil'):
    for prefix in ('/usr/bin/','/usr/sbin/'):
     source=source.replace(prefix+command,str(root/'fakebin'/command))
   # Advance only retry-loop time in this disposable copy. No recovery branch replaced.
   source=source.replace('case "${1:-status}" in', '''export MOCK_FRONT_PID=$$
sleep() { SECONDS=$((SECONDS+31)); }
case "${1:-status}" in''')
   for dangerous in ('/usr/sbin/diskutil','/usr/bin/sudo','/usr/bin/xcrun','/usr/bin/tmutil','/usr/sbin/chown'):
    self.assertNotIn(dangerous,source)
   (root/'rtl9210-health').write_text(source)
   (root/'src/rtl9210_health_probe.m').write_text('SIMULATED BUILD INPUT; NEVER COMPILE')
   for helper in ('storage_guard.py','volume_guard.py','bounded.py','privileged_worker.py','worker_client.py'):
    text=(ROOT/'scripts'/helper).read_text().replace('/usr/sbin/diskutil',str(root/'fakebin/diskutil')).replace('/bin/df',str(root/'fakebin/df'))
    if helper=='bounded.py':
     # Preserve supervisor/reaping logic, shorten only fake native timeout.
     text=text.replace('deadline = time.monotonic() + float(seconds)',"deadline = time.monotonic() + (0.15 if (os.environ['MOCK_SCENARIO']=='native_timeout' and seconds=='120') or (os.environ['MOCK_SCENARIO'].startswith('async_') and seconds=='25') else float(seconds))")
     text=text.replace("    if receipt != '-':\n        fd =", "    if os.environ['MOCK_SCENARIO'] in ('no_probe_receipt','signal_probe_no_receipt') and receipt.endswith('/child-exited'):\n        return 125\n    if os.environ['MOCK_SCENARIO']=='no_unmount_receipt' and receipt.endswith('/unmountDisk-exited'):\n        return 125\n    if receipt != '-':\n        fd =")
    if helper=='privileged_worker.py':
     text=text.replace('if os.geteuid() != 0:', "if os.environ.get('MOCK_PRIVILEGED') != '1':")
     text=text.replace('os.chown(', 'mock_chown(')
     hook="""
def mock_chown(path, uid, gid):
    base=Path(os.environ['MOCK_ROOT']);scenario=os.environ['MOCK_SCENARIO']
    if Path(path).name in ('summary.txt','probe.log','release-status.plist'):
        with (base/'events').open('a') as log: log.write('OWNERSHIP_RETURN\\n')
        state=json.loads((base/'state.json').read_text());state['chown_done']=True
        (base/'state.json').write_text(json.dumps(state))
        if scenario=='chown_fail': raise PermissionError('simulated ownership failure')
"""
     text=text.replace('stop = False',hook+'\nstop = False',1)
    if helper=='worker_client.py':
     text=text.replace('os.kill(launcher, 0)', 'mock_launcher_check(launcher)')
     text=text.replace('def wait_status(', '''def mock_launcher_check(launcher):
    if os.environ.get('MOCK_SCENARIO') == 'launcher_eperm':
        with (Path(os.environ['MOCK_ROOT'])/'events').open('a') as stream: stream.write('LAUNCHER_EPERM\\n')
        raise PermissionError('simulated root-owned sudo')
    os.kill(launcher, 0)

def wait_status(''')
     text=text.replace('deadline = time.monotonic() + seconds', "deadline = time.monotonic() + (2 if os.environ.get('MOCK_SCENARIO') in ('auth_fail','worker_not_privileged') else min(seconds, 12))")
    (root/'scripts'/helper).write_text(text)
   (root/'scripts/macos-sudo-askpass').write_text('#!/bin/sh\nexit 99\n');(root/'scripts/macos-sudo-askpass').chmod(0o700)
   if mode=='probe' or replace:
    (root/'config').mkdir();(root/'config/device.plist').write_bytes(plistlib.dumps(PROFILE));(root/'config/device.plist').chmod(0o600)
   if scenario=='lock_exists': (root/'.capture-lock').mkdir()
   if scenario=='publish_fail': (root/'latest').mkdir()
   args=['/bin/bash',str(root/'rtl9210-health'),mode,'--confirm']
   if mode=='setup': args+=['--disk','/dev/disk9','--volume','/Volumes/Example Data']
   if replace: args+=['--replace']
   env=dict(os.environ,MOCK_ROOT=str(root),MOCK_SCENARIO=scenario,PATH=str(Path(sys.executable).parent)+':/usr/bin:/bin')
   for key in ('BASH_ENV','ENV','PYTHONPATH','PYTHONSTARTUP'): env.pop(key,None)
   if scenario=='blocked_output':
    # Do not read either output channel until the process exits. Large fixture logs
    # fill this pipe; replay must time out AFTER restoration and lock release.
    read_fd,write_fd=os.pipe()
    try:
     process=subprocess.Popen(args,cwd=root,env=env,stdout=write_fd,stderr=write_fd)
     os.close(write_fd);write_fd=-1
     try: rc=process.wait(timeout=35)
     except BaseException:
      process.kill();process.wait();raise
     captured=os.read(read_fd,1000000).decode(errors='replace')
     result=subprocess.CompletedProcess(args,rc,captured,'')
    finally:
     os.close(read_fd)
     if write_fd!=-1: os.close(write_fd)
   else:
    result=subprocess.run(args,cwd=root,env=env,capture_output=True,text=True,timeout=35)
   if roundtrip:
    self.assertEqual(result.returncode,0,result.stderr)
    import time
    time.sleep(1.1)  # Separate the production second-resolution run IDs.
    result=subprocess.run(['/bin/bash',str(root/'rtl9210-health'),'probe','--confirm'],cwd=root,env=env,capture_output=True,text=True,timeout=35)
   if scenario.startswith('async_'):
    import time
    until=time.monotonic()+3
    while not json.loads((root/'state.json').read_text()).get('daemon_completed') and time.monotonic()<until: time.sleep(0.05)
   done_files=list((root/'runs').glob('*/worker-done.json')) if (root/'runs').exists() else []
   worker_done=json.loads(done_files[0].read_text()) if done_files else None
   transactions='\n'.join(p.read_text() for p in (root/'runs').glob('*/transaction.log')) if (root/'runs').exists() else ''
   events=(root/'events').read_text() if (root/'events').exists() else ''
   profile=plistlib.loads((root/'config/device.plist').read_bytes()) if (root/'config/device.plist').exists() else None
   return dict(rc=result.returncode,out=result.stdout,err=result.stderr,events=events,transactions=transactions,worker_done=worker_done,
    locked=(root/'.capture-lock').exists(),latest=(root/'latest').is_symlink(),profile=profile,
    state=json.loads((root/'state.json').read_text()))
 def check_case(self,mode,scenario):
  r=self.run_case(mode,scenario)
  detail=json.dumps(r,indent=2)
  success=scenario in ('ok','delayed_identity')
  self.assertEqual(r['rc']==0,success,detail)
  before=scenario in ('auth_fail','backup_busy','build_fail','binding_changed','snapshot_missing','external_storage','lock_exists')
  uncertain=scenario in ('no_unmount_receipt','unmount_fail')
  failed_recovery=scenario in ('disk_missing','identity_mismatch','partial_restore','mount_error_but_restored')
  if before:
   self.assertNotIn('MUTATION',r['events'],detail)
  elif uncertain or scenario in ('disk_missing','identity_mismatch'):
   self.assertNotIn('MUTATION mountDisk',r['events'],detail)
  else:
   self.assertIn('MUTATION mountDisk',r['events'],detail)
  if uncertain or failed_recovery:
   self.assertEqual(r['rc'],70,detail);self.assertTrue(r['locked'],detail)
  elif scenario not in ('lock_exists','publish_fail','auth_fail'):
   self.assertFalse(r['locked'],detail)
  self.assertEqual(r['latest'],success,detail)
  self.assertNotIn('RELEASE_PERMISSION_DENIED',r['events'],detail)
  if scenario=='native_timeout': self.assertIn('Native release unconfirmed',r['transactions'])
  if mode=='setup' and not success: self.assertIsNone(r['profile'],detail)

 def test_general_volume_layouts_complete_setup_and_probe(self):
  for scenario in ('single_volume', 'three_volumes'):
   with self.subTest(scenario=scenario):
    r=self.run_case('roundtrip',scenario)
    self.assertEqual(r['rc'],0,json.dumps(r,indent=2))
    self.assertTrue(r['latest']);self.assertFalse(r['locked'])
    self.assertEqual(r['events'].count('NATIVE_ENTER'),2)

 def test_third_volume_loss_blocks_publication(self):
  r=self.run_case('probe','third_volume_loss')
  self.assertEqual(r['rc'],70,json.dumps(r,indent=2))
  self.assertTrue(r['locked']);self.assertFalse(r['latest'])

 def test_root_launcher_permission_denied_waits_for_handshake(self):
  for mode in ('setup','probe'):
   with self.subTest(mode=mode):
    r=self.run_case(mode,'launcher_eperm')
    self.assertEqual(r['rc'],0,json.dumps(r,indent=2))
    self.assertIn('LAUNCHER_EPERM',r['events'])
    self.assertLess(r['events'].index('PRIVILEGED_WORKER_STARTED'),r['events'].index('MUTATION unmountDisk'))
    self.assertTrue(r['state']['mounted']);self.assertFalse(r['locked'])

 def test_setup_invalid_enrollment_preserves_profile(self):
  r=self.run_case('setup','invalid_enrollment',replace=True)
  self.assertNotEqual(r['rc'],0)
  self.assertIn('invalid nvme_serial',(r['out']+r['err']),(r['out']+r['err']))
  self.assertEqual(r['profile'],PROFILE,json.dumps(r,indent=2))
  self.assertFalse(r['latest']);self.assertFalse(r['locked'])
 def test_setup_missing_enrollment(self):
  r=self.run_case('setup','missing_enrollment')
  self.assertNotEqual(r['rc'],0);self.assertIsNone(r['profile'])
  self.assertIn('without a private enrollment result',(r['out']+r['err']),(r['out']+r['err']))
  self.assertTrue(r['state']['mounted']);self.assertFalse(r['locked'])
 def test_setup_publish_failure(self):
  r=self.run_case('setup','publish_fail')
  self.assertNotEqual(r['rc'],0);self.assertFalse(r['latest'])
  self.assertIn('IsADirectoryError',(r['out']+r['err']),(r['out']+r['err']))
  self.assertTrue(r['state']['mounted']);self.assertFalse(r['locked'],json.dumps(r,indent=2))
 def test_probe_publish_failure(self):
  r=self.run_case('probe','publish_fail')
  self.assertNotEqual(r['rc'],0);self.assertFalse(r['latest'])
  self.assertIn('IsADirectoryError',(r['out']+r['err']),(r['out']+r['err']))
  self.assertTrue(r['state']['mounted']);self.assertFalse(r['locked'],json.dumps(r,indent=2))

 def test_setup_then_probe_roundtrip(self):
  r=self.run_case('roundtrip','ok')
  self.assertEqual(r['rc'],0,(r['out']+r['err']));self.assertTrue(r['latest'])
  self.assertEqual(r['events'].count('NATIVE_ENTER'),2)
  self.assertFalse(r['locked']);self.assertTrue(r['state']['mounted'])

 def test_blocked_output_does_not_block_recovery(self):
  r=self.run_case('probe','blocked_output')
  self.assertEqual(r['rc'],0);self.assertTrue(r['state']['mounted'])
  self.assertFalse(r['locked']);self.assertTrue(r['latest'])
  self.assertIn('Recovery verified',r['transactions'])
 def test_setup_invalid_snapshot_never_unmounts(self):
  r=self.run_case('setup','snapshot_invalid')
  self.assertNotEqual(r['rc'],0);self.assertNotIn('MUTATION',r['events'])
  self.assertIsNone(r['profile']);self.assertFalse(r['locked'])
 def test_probe_invalid_snapshot_never_unmounts(self):
  r=self.run_case('probe','snapshot_invalid')
  self.assertNotEqual(r['rc'],0);self.assertNotIn('MUTATION',r['events'])
  self.assertFalse(r['locked'])
 def test_setup_final_exfat_loss_keeps_lock(self):
  r=self.run_case('setup','late_exfat_loss')
  self.assertEqual(r['rc'],70);self.assertTrue(r['locked']);self.assertFalse(r['latest'])
  self.assertIn('final verification failed',r['transactions'])
 def test_probe_final_exfat_loss_keeps_lock(self):
  r=self.run_case('probe','late_exfat_loss')
  self.assertEqual(r['rc'],70);self.assertTrue(r['locked']);self.assertFalse(r['latest'])
  self.assertIn('final verification failed',r['transactions'])
 def test_cooperative_cancel_release_and_recovery(self):
  r=self.run_case('probe','native_cancel_cooperative')
  self.assertNotEqual(r['rc'],0);self.assertFalse(r['latest'])
  self.assertTrue(r['state']['mounted']);self.assertFalse(r['locked'])
  self.assertIn('Native release path completed',r['transactions'])
 def test_missing_release_marker_cannot_report_probe_success(self):
  r=self.run_case('probe','missing_release')
  self.assertNotEqual(r['rc'],0);self.assertFalse(r['latest'])
  self.assertTrue(r['state']['mounted']);self.assertFalse(r['locked'])
  self.assertIn('Native release unconfirmed',r['transactions'])

 def test_release_record_read_after_ownership_return(self):
  r=self.run_case('probe','ok')
  self.assertEqual(r['rc'],0,r['out']+r['err'])
  self.assertNotIn('RELEASE_PERMISSION_DENIED',r['events'])
  self.assertLess(r['events'].index('OWNERSHIP_RETURN'),r['events'].index('plutil -extract release_path_completed'))

 def test_one_authorized_owner_before_unmount(self):
  r=self.run_case('probe','ok')
  self.assertEqual(r['rc'],0,r['out']+r['err'])
  self.assertEqual(r['events'].count('sudo -A'),1)
  self.assertNotIn('sudo -n',r['events'])
  self.assertLess(r['events'].index('PRIVILEGED_WORKER_STARTED'),r['events'].index('MUTATION unmountDisk'))
  self.assertTrue(r['worker_done']['quiescent'])
 def test_missing_actual_privilege_never_unmounts(self):
  r=self.run_case('probe','worker_not_privileged')
  self.assertNotEqual(r['rc'],0);self.assertNotIn('MUTATION',r['events'])
 def test_lost_sudo_monitor_does_not_lose_worker(self):
  r=self.run_case('probe','launcher_lost')
  self.assertEqual(r['rc'],0,r['transactions'])
  self.assertTrue(r['worker_done']['quiescent']);self.assertTrue(r['state']['mounted'])
 def test_cancel_running_probe_acknowledged_by_owner(self):
  r=self.run_case('probe','cancel_running_probe')
  self.assertNotEqual(r['rc'],0);self.assertTrue(r['state']['mounted'])
  self.assertIn('NATIVE_CANCELLED_BY_OWNER',r['events'])
  self.assertTrue(r['worker_done']['quiescent'])
 def test_late_unmount_callback_never_triggers_second_mutation(self):
  r=self.run_case('probe','async_unmountDisk')
  self.assertEqual(r['rc'],70);self.assertTrue(r['locked'])
  self.assertNotIn('NATIVE_ENTER',r['events']);self.assertNotIn('MUTATION mountDisk',r['events'])
  self.assertTrue(r['state']['daemon_completed'])
 def test_late_mount_callback_does_not_become_success(self):
  r=self.run_case('probe','async_mountDisk')
  self.assertEqual(r['rc'],70);self.assertTrue(r['locked']);self.assertFalse(r['latest'])
  self.assertTrue(r['state']['daemon_completed'])
  self.assertEqual(r['events'].count('MUTATION mountDisk'),1)

SCENARIOS=('ok','auth_fail','backup_busy','build_fail','binding_changed','snapshot_missing',
 'external_storage','lock_exists','unmount_fail','no_unmount_receipt','native_fail','native_timeout',
 'no_probe_receipt','signal_probe','signal_probe_no_receipt','signal_unmount','disk_missing',
 'identity_mismatch','partial_restore','delayed_identity','mount_error_but_restored','chown_fail')
for mode in ('setup','probe'):
 for scenario in SCENARIOS:
  def test(self,mode=mode,scenario=scenario): self.check_case(mode,scenario)
  setattr(WorkflowTests,'test_'+mode+'_'+scenario,test)

if __name__=='__main__':
 if '--parallel' in sys.argv:
  from concurrent.futures import ThreadPoolExecutor
  import io
  names=unittest.defaultTestLoader.getTestCaseNames(WorkflowTests)
  def run_one(name):
   output=io.StringIO()
   result=unittest.TextTestRunner(stream=output,verbosity=2).run(unittest.TestSuite([WorkflowTests(name)]))
   return result.wasSuccessful(),output.getvalue()
  with ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(run_one,names))
  for success,output in results: print(output)
  print(f'TOTAL {len(results)}, PASS {sum(ok for ok,_ in results)}, FAIL {sum(not ok for ok,_ in results)}')
  sys.exit(0 if all(ok for ok,_ in results) else 1)
 else: unittest.main(verbosity=2)
