#!/usr/bin/env python3
"""Reject redirected paths and external dependency storage before any capture."""
import os
import pathlib
import plistlib
import re
import subprocess
import sys

if sys.version_info < (3, 9):
    raise SystemExit('Python 3.9+ required')
root = pathlib.Path(sys.argv[1]).resolve(strict=True)

def internal(path):
    # diskutil info accepts a device or mount point, not an arbitrary pathname.
    # Ask df for the actual filesystem: walking parents is wrong for APFS firmlinks.
    path = pathlib.Path(path).resolve(strict=True)
    before = path.stat().st_dev
    result = subprocess.run(['/bin/df', '-P', str(path)], check=True,
                            capture_output=True, timeout=15,
                            env=dict(os.environ, LC_ALL='C'))
    lines = result.stdout.decode('utf-8').splitlines()
    if len(lines) != 2 or not lines[0].startswith('Filesystem') or not lines[1].split():
        raise SystemExit('Cannot resolve storage device: ' + str(path))
    device = lines[1].split()[0]
    if not re.fullmatch(r'/dev/disk[0-9]+(?:s[0-9]+)*', device):
        raise SystemExit('Unsupported storage device: ' + device)
    data = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', device],
                          check=True, capture_output=True, timeout=15)
    info = plistlib.loads(data.stdout)
    if (info.get('Internal') is not True or info.get('DeviceNode') != device
            or path.stat().st_dev != before):
        raise SystemExit('Storage is not verified internal: ' + str(path))

internal(root)
for relative in ('.build', '.build/rtl9210_health_probe', 'runs', 'config',
                 'config/device.plist', 'scripts', 'scripts/volume_guard.py',
                 'scripts/bounded.py', 'scripts/privileged_worker.py', 'scripts/worker_client.py', 'scripts/storage_guard.py',
                 'scripts/macos-sudo-askpass', 'src', 'src/rtl9210_health_probe.m', 'src/cancellation.h'):
    target = root / relative
    for component in (target, *target.parents):
        if component == root:
            break
        if component.is_symlink():
            raise SystemExit('Refusing symlink: ' + str(component))
    existing = target
    while not existing.exists():
        existing = existing.parent
    internal(existing.resolve(strict=True))
# Executable, runtime prefixes and actual stdlib modules can differ with venvs.
for path in {sys.executable, sys.prefix, sys.base_prefix, os.__file__,
             subprocess.__file__, plistlib.__file__}:
    internal(pathlib.Path(path).resolve(strict=True))
