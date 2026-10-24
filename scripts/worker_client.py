#!/usr/bin/env python3
"""Unprivileged bounded handshake; cancellation is a request, never a root signal."""
import json
import os
from pathlib import Path
import signal
import sys
import time

def wait_status(directory, ready, seconds, launcher):
    deadline = time.monotonic() + seconds
    name = 'worker-ready.json' if ready else 'worker-done.json'
    while time.monotonic() < deadline:
        if (directory/name).is_file():
            with open(directory/name) as source:
                status = json.load(source)
            if ready:
                if status.get('ready') is not True or (directory/'worker-done.json').exists():
                    return 125
                pid = status.get('worker_pid')
                if not isinstance(pid, int) or pid <= 1:
                    return 125
                try:
                    os.kill(pid, 0)  # Existence check only; no signal is delivered.
                except ProcessLookupError:
                    return 125
                except PermissionError:
                    pass  # A root-owned live worker can deny a user's existence check.
                return 0
            return 0 if status.get('quiescent') is True else 125
        if ready:
            try:
                os.kill(launcher, 0)
            except ProcessLookupError:
                return 125
            except PermissionError:
                # sudo may already be root-owned before publishing worker-ready.
                # EPERM is not readiness: continue waiting for the worker handshake.
                pass
        if (directory/'worker-blocked.json').exists():
            return 125
        time.sleep(0.1)
    return 125

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(125))
    mode, directory, seconds, launcher = sys.argv[1:]
    if mode not in ('ready', 'done'): raise SystemExit(2)
    raise SystemExit(wait_status(Path(directory), mode == 'ready', float(seconds), int(launcher)))
