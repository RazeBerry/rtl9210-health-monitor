"""Compile/run only the USB-free cancellation harness, never the native probe."""
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class CancellationTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory(prefix='rtl-cancel-only-',dir='/private/tmp')
  cls.exe=str(Path(cls.tmp.name)/'cancel-mock')
  subprocess.run(['/usr/bin/xcrun','clang','-Wall','-Wextra','-Werror','-I',str(ROOT/'src'),
   str(ROOT/'tests/native_cancel_mock.c'),'-o',cls.exe],check=True,capture_output=True)
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()
 def check_signal(self,signum):
  for phase in range(4):
   with self.subTest(phase=phase):
    result=subprocess.run([self.exe,str(signum),str(phase)],capture_output=True,text=True,timeout=3)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertIn('fake cleanup completed',result.stdout)
 def test_term(self):self.check_signal(signal.SIGTERM)
 def test_int(self):self.check_signal(signal.SIGINT)
 def test_hup(self):self.check_signal(signal.SIGHUP)
 def test_native_uses_handler_and_checkpoints(self):
  # Structural assertions complement the USB-free behavior tests; not a USB simulation.
  src=(ROOT/'src/rtl9210_health_probe.m').read_text()
  self.assertIn('rtl_install_cancellation()',src)
  for operation in ('device = [[IOUSBHostDevice alloc]', 'BOOL unconfigured =',
                    'BOOL configured =','g_intf = open_interface'):
   index=src.index(operation)
   self.assertIn('if (rtl_cancel_signal) goto done;',src[index-65:index])
  self.assertLess(src.index('[device destroy]'),src.index('@"release_path_completed"'))
  self.assertIn('if (rtl_cancel_signal) return -6;',src)

if __name__=='__main__':unittest.main(verbosity=2)
