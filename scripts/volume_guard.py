#!/usr/bin/env python3
"""Read-only diskutil inventory/verification; never mounts or changes a device."""
import json
import os
import plistlib
import re
import subprocess
import sys


def query(*args):
    result = subprocess.run(['/usr/sbin/diskutil', *args], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError('diskutil query failed: ' + ' '.join(args))
    return plistlib.loads(result.stdout)

def belongs(info, disk):
    node = disk.removeprefix('/dev/')
    stores = info.get('APFSPhysicalStores', [])
    if stores:
        return len(stores) == 1 and bool(re.fullmatch(re.escape(node) + r's[0-9]+',
            stores[0].get('APFSPhysicalStore', '')))
    return info.get('ParentWholeDisk', info.get('PartOfWhole')) == node

def is_mounted(info):
    # macOS diskutil info may omit Mounted; MountPoint is then an explicit
    # string (empty for an unmounted volume). Missing data is not proof of unmount.
    point = info.get('MountPoint')
    flag = info.get('Mounted')
    if isinstance(point, str):
        mounted = bool(point)
        if 'Mounted' in info and (type(flag) is not bool or flag != mounted):
            raise RuntimeError('Inconsistent volume mount state')
        return mounted
    if 'MountPoint' not in info and flag is False:
        return False
    raise RuntimeError('Volume mount state is unknown')

def mounted_on_disk(disk, require_all_mounted=False):
    whole = query('info', '-plist', disk)
    if not whole.get('WholeDisk') or whole.get('DeviceIdentifier') != disk[5:]:
        raise RuntimeError('Physical disk is missing or changed')
    # Only enumerate physical partitions of this disk and APFS volumes whose
    # physical stores belong to it. Never query every disk in the host inventory.
    node = disk.removeprefix('/dev/')
    physical = query('list', '-plist', disk)
    nodes = set(physical.get('AllDisks', []))
    if node not in nodes or any(not re.fullmatch(re.escape(node) + r'(?:s[0-9]+)?', n) for n in nodes):
        raise RuntimeError('Invalid target physical partition inventory')
    apfs_volumes = set()
    related_containers = 0
    for container in query('apfs', 'list', '-plist').get('Containers', []):
        stores = container.get('PhysicalStores', [])
        related = [p.get('DeviceIdentifier', '') for p in stores
                   if re.fullmatch(re.escape(node) + r's[0-9]+', p.get('DeviceIdentifier', ''))]
        if not related:
            continue
        related_containers += 1
        if len(stores) != 1:
            raise RuntimeError('Target APFS container spans multiple physical stores')
        for volume in container.get('Volumes', []):
            identifier = volume.get('DeviceIdentifier', '')
            if not re.fullmatch(r'disk[0-9]+s[0-9]+', identifier):
                raise RuntimeError('Incomplete target APFS volume inventory')
            nodes.add(identifier)
            apfs_volumes.add(identifier)
    if related_containers == 0:
        raise RuntimeError('Target APFS topology is not ready; refusing incomplete inventory')
    volumes = []
    for identifier in sorted(nodes):
        info = query('info', '-plist', identifier)
        if require_all_mounted and identifier in apfs_volumes and not belongs(info, disk):
            raise RuntimeError('Target APFS volume physical-store identity is incomplete')
        if require_all_mounted and belongs(info, disk) and (
                identifier in apfs_volumes or (info.get('FilesystemType') and info.get('Content') != 'EFI')):
            if not is_mounted(info):
                raise RuntimeError('All target data volumes must be mounted and unlocked before capture')
        if belongs(info, disk) and is_mounted(info):
            if not info.get('VolumeUUID'):
                raise RuntimeError('Mounted volume has no stable UUID')
            volumes.append({k: info.get(k, '') for k in
                            ('VolumeUUID', 'VolumeName', 'MountPoint', 'FilesystemType')})
    return volumes

def snapshot(disk, path, anchor_uuid=None):
    volumes = mounted_on_disk(disk, require_all_mounted=True)
    if anchor_uuid is not None and anchor_uuid not in {v['VolumeUUID'] for v in volumes}:
        raise RuntimeError('Selected APFS volume disappeared before snapshot')
    validate_manifest(volumes)
    with open(path, 'x', encoding='utf-8') as out:
        json.dump(volumes, out, indent=2)
    os.chmod(path, 0o600)

def validate_manifest(expected):
    fields = ('VolumeUUID', 'VolumeName', 'MountPoint', 'FilesystemType')
    if not isinstance(expected, list) or not expected:
        raise RuntimeError('Manifest must contain at least one original volume')
    for volume in expected:
        if not isinstance(volume, dict) or any(
                not isinstance(volume.get(key), str) or not volume[key] for key in fields):
            raise RuntimeError('Incomplete volume manifest')
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', volume['VolumeUUID']):
            raise RuntimeError('Invalid volume UUID in manifest')
        if not volume['MountPoint'].startswith('/Volumes/') or os.path.normpath(volume['MountPoint']) != volume['MountPoint'] or any(c in volume['MountPoint'] for c in ('\n', '\r', '\0')):
            raise RuntimeError('Invalid original mount path')
    if len({v['VolumeUUID'].lower() for v in expected}) != len(expected) or len({v['MountPoint'] for v in expected}) != len(expected):
        raise RuntimeError('Manifest has wrong or duplicate identities')
    return expected

def load_manifest(path):
    with open(path, encoding='utf-8') as src:
        return validate_manifest(json.load(src))

def verify_identity(disk, path):
    expected = load_manifest(path)
    for volume in expected:
        try:
            info = query('info', '-plist', volume['VolumeUUID'])
        except (RuntimeError, subprocess.TimeoutExpired):
            sys.exit(75)  # Temporary absence while Disk Arbitration discovers volumes.
        if not info.get('VolumeUUID') or not info.get('FilesystemType') or not (
                info.get('APFSPhysicalStores') or info.get('ParentWholeDisk') or info.get('PartOfWhole')):
            sys.exit(75)
        if not belongs(info, disk) or any(info.get(k, '') != volume[k]
                for k in ('VolumeUUID', 'VolumeName', 'FilesystemType')):
            raise RuntimeError('Refusing mount: volume identity/physical store changed')

def verify(disk, path):
    expected = load_manifest(path)
    actual = mounted_on_disk(disk)
    if sorted(expected, key=lambda v: v['VolumeUUID']) != sorted(actual, key=lambda v: v['VolumeUUID']):
        raise RuntimeError('Volume UUID/name/path/filesystem or physical-store verification failed')

def main():
    mode, disk, path, *anchor = sys.argv[1:]
    if anchor and (mode != 'snapshot' or len(anchor) != 1):
        raise RuntimeError('Unexpected arguments')
    if not re.fullmatch(r'/dev/disk[0-9]+', disk):
        raise RuntimeError('Invalid physical disk')
    if mode == 'snapshot':
        snapshot(disk, path, *anchor)
    elif mode == 'identity':
        verify_identity(disk, path)
    elif mode == 'verify':
        verify(disk, path)
    elif mode == 'unmounted':
        verify_identity(disk, path)
        for volume in load_manifest(path):
            info = query('info', '-plist', volume['VolumeUUID'])
            if is_mounted(info):
                raise RuntimeError('Original volume is still mounted or mount state unknown')
        if mounted_on_disk(disk):
            raise RuntimeError('Target still has mounted volumes; refusing USB capture')
    else:
        raise RuntimeError('Unknown verification mode')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('VOLUME GUARD: ' + str(exc), file=sys.stderr)
        sys.exit(1)
