#!/usr/bin/env python3
"""One authenticated, fixed-purpose owner of one native probe. No disk mutations."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time

stop = False

def cancelled(signum, frame):
    global stop
    stop = True

def publish(path, value, uid, gid):
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'x', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream)
    os.chown(temporary, uid, gid)
    os.replace(temporary, path)

def parent_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False

def main():
    if os.geteuid() != 0:
        raise RuntimeError('Worker must be authorized before any unmount')
    root, directory, mode, disk, binding, vid, pid, uid, gid, parent = sys.argv[1:]
    root, directory = Path(root).resolve(), Path(directory).resolve()
    uid, gid, parent = int(uid), int(gid), int(parent)
    if directory.parent != root / 'runs' or mode not in ('enroll', 'probe') or uid == 0 or parent <= 1:
        raise RuntimeError('Invalid worker plan')
    spec = importlib.util.spec_from_file_location('worker_bounded', root/'scripts/bounded.py')
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancelled)
    def should_stop():
        return stop or (directory/'cancel.request').exists() or not parent_alive(parent)
    rc = 125
    request_seen = False
    try:
        if should_stop():
            return 125
        # This is a handshake from the actual authorized process, not a cached sudo -v.
        publish(directory/'worker-ready.json', {'ready': True, 'worker_pid': os.getpid()}, uid, gid)
        deadline = time.monotonic() + 120
        while not should_stop() and time.monotonic() < deadline:
            if (directory/'probe.request').exists():
                request_seen = True
                break
            time.sleep(0.1)
        if not request_seen or should_stop():
            return 125
        binary = str(root/'.build/rtl9210_health_probe')
        command = [binary, mode, str(directory)]
        command += [vid, pid] if mode == 'enroll' else [str(root/'config/device.plist')]
        command += [disk, binding]
        sys.argv = ['bounded', '120', str(directory/'child-exited'), *command]
        # Only this root owner signals/reaps its native child; the shell never kills sudo.
        with open(directory/'summary.txt', 'w') as output, open(directory/'probe.log', 'w') as errors:
            saved_out, saved_err = os.dup(1), os.dup(2)
            try:
                os.dup2(output.fileno(), 1); os.dup2(errors.fileno(), 2)
                rc = supervisor.main(cancel_check=should_stop)
            finally:
                os.dup2(saved_out, 1); os.dup2(saved_err, 2)
                os.close(saved_out); os.close(saved_err)
        return rc
    finally:
        child = supervisor.child
        if child is not None and child.poll() is None:
            try:
                publish(directory/'worker-blocked.json', {'quiescent': False, 'reason': 'child exit unconfirmed'}, uid, gid)
            except OSError:
                pass  # A status-write failure must not abandon a still-live child.
            # Do not abandon an unkillable child. Retain its privileged owner until reaped.
            while child.poll() is None:
                time.sleep(1)
            child.wait()
            rc = 125
        # Fixed filenames only; never recursive ownership changes or arbitrary commands.
        for name in ('summary.txt', 'probe.log', 'child-exited', 'release-status.plist',
                     'identify_controller.bin', 'smart_health_log.bin', 'error_info_log.bin', 'enrollment.plist'):
            path = directory/name
            if path.is_file() and not path.is_symlink():
                try:
                    os.chown(path, uid, gid)
                except OSError:
                    rc = 1
        publish(directory/'worker-done.json', {'quiescent': True, 'returncode': rc,
                'probe_requested': request_seen}, uid, gid)

if __name__ == '__main__':
    sys.exit(main())
