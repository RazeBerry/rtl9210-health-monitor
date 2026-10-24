"""Worker lifecycle tests with fake privilege and a fake child module; no commands run."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('worker',ROOT/'scripts/privileged_worker.py')
W=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(W)

class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='rtl-worker-unit-',dir='/private/tmp');self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);(self.root/'scripts').mkdir();(self.root/'runs').mkdir()
  self.run=self.root/'runs/run';self.run.mkdir()
  (self.root/'scripts/bounded.py').write_text('''class Child:
 def __init__(self): self.polls=0
 def poll(self):
  self.polls+=1
  return None if self.polls<3 else 0
 def wait(self): return 0
child=None
def main(cancel_check=None):
 global child
 child=Child()
 return 125
''')
  self.argv=['worker',str(self.root),str(self.run),'probe','/dev/disk9','123','0x0bda','0x9210','501','20',str(os.getpid())]
  W.stop=False
 def invoke(self,alive=True,publication=None):
  with patch.object(sys,'argv',self.argv),patch.object(W.os,'geteuid',return_value=0), \
       patch.object(W.os,'chown'),patch.object(W,'parent_alive',return_value=alive), \
       patch.object(W.signal,'signal'),patch.object(W.time,'sleep'), \
       patch.object(W,'publish',side_effect=publication or W.publish):
   return W.main()
 def test_status_write_failure_does_not_abandon_child(self):
  (self.run/'probe.request').touch();events=[];original=W.publish
  def publication(path,value,uid,gid):
   events.append(path.name)
   if path.name=='worker-blocked.json': raise OSError('simulated full storage')
   original(path,value,uid,gid)
  self.assertEqual(self.invoke(publication=publication),125)
  self.assertEqual(events,['worker-ready.json','worker-blocked.json','worker-done.json'])
  self.assertTrue(json.loads((self.run/'worker-done.json').read_text())['quiescent'])
 def test_cancel_before_handshake_never_starts_probe(self):
  (self.run/'cancel.request').touch();(self.run/'probe.request').touch()
  self.assertEqual(self.invoke(),125)
  self.assertFalse((self.run/'worker-ready.json').exists());self.assertFalse((self.run/'summary.txt').exists())
  self.assertFalse(json.loads((self.run/'worker-done.json').read_text())['probe_requested'])
 def test_dead_frontend_never_starts_probe(self):
  self.assertEqual(self.invoke(alive=False),125)
  self.assertFalse((self.run/'summary.txt').exists())
 def test_actual_privilege_required(self):
  with patch.object(W.os,'geteuid',return_value=501):
   with self.assertRaises(RuntimeError): W.main()
  self.assertFalse((self.run/'worker-ready.json').exists())

 def test_ready_check_handles_root_permission_denial(self):
  spec=importlib.util.spec_from_file_location('client',ROOT/'scripts/worker_client.py')
  client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
  (self.run/'worker-ready.json').write_text(json.dumps({'ready':True,'worker_pid':12345}))
  with patch.object(client.os,'kill',side_effect=PermissionError('root UID')):
   self.assertEqual(client.wait_status(self.run,True,1,123),0)
 def test_stale_ready_record_cannot_authorize_unmount(self):
  spec=importlib.util.spec_from_file_location('client',ROOT/'scripts/worker_client.py')
  client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
  (self.run/'worker-ready.json').write_text(json.dumps({'ready':True,'worker_pid':12345}))
  with patch.object(client.os,'kill',side_effect=ProcessLookupError('worker exited')):
   self.assertEqual(client.wait_status(self.run,True,1,123),125)

 def client(self):
  spec=importlib.util.spec_from_file_location('client',ROOT/'scripts/worker_client.py')
  client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
  return client
 def test_launcher_eperm_waits_for_worker_handshake(self):
  client=self.client();events=[]
  def publish_after_wait(seconds):
   events.append('wait')
   (self.run/'worker-ready.json').write_text(json.dumps({'ready':True,'worker_pid':12345}))
  with patch.object(client.os,'kill',side_effect=PermissionError('root UID')),patch.object(client.time,'sleep',side_effect=publish_after_wait):
   self.assertEqual(client.wait_status(self.run,True,1,123),0)
  self.assertEqual(events,['wait'])
 def test_launcher_eperm_without_handshake_times_out(self):
  client=self.client()
  with patch.object(client.os,'kill',side_effect=PermissionError('root UID')),patch.object(client.time,'sleep'),patch.object(client.time,'monotonic',side_effect=[0,0,2]):
   self.assertEqual(client.wait_status(self.run,True,1,123),125)
 def test_missing_launcher_fails_without_handshake(self):
  client=self.client()
  with patch.object(client.os,'kill',side_effect=ProcessLookupError('launcher gone')):
   self.assertEqual(client.wait_status(self.run,True,1,123),125)
 def test_launcher_eperm_and_completed_worker_never_ready(self):
  client=self.client()
  def finish(seconds):
   (self.run/'worker-ready.json').write_text(json.dumps({'ready':True,'worker_pid':12345}))
   (self.run/'worker-done.json').write_text(json.dumps({'quiescent':True}))
  with patch.object(client.os,'kill',side_effect=PermissionError('root UID')),patch.object(client.time,'sleep',side_effect=finish):
   self.assertEqual(client.wait_status(self.run,True,1,123),125)

if __name__=='__main__':unittest.main(verbosity=2)
