#!/usr/bin/env python3
"""Bound one direct child; receipt is written only after wait() proves exit."""
import json
import os
import signal
import subprocess
import sys
import time

child = None
cancelled = False

def cancel(signum, frame):
    global cancelled
    cancelled = True

for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, cancel)

def terminate(sig):
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass

def main(cancel_check=None):
    global child
    seconds, receipt, *command = sys.argv[1:]
    deadline = time.monotonic() + float(seconds)
    if cancelled:
        return 130
    if receipt != '-' and os.path.lexists(receipt):
        raise RuntimeError('Refusing stale child-exit receipt')
    child = subprocess.Popen(command, start_new_session=True)
    timed_out = False
    while child.poll() is None:
        if cancelled or (cancel_check is not None and cancel_check()) or time.monotonic() >= deadline:
            timed_out = True
            try:
                terminate(signal.SIGTERM)
            except PermissionError:
                print('No permission to stop child; exit unconfirmed', file=sys.stderr)
                return 125
            try:
                child.wait(timeout=30 if receipt.endswith('/child-exited') else 3)
            except subprocess.TimeoutExpired:
                try:
                    terminate(signal.SIGKILL)
                except PermissionError:
                    return 125
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print('CHILD EXIT UNCONFIRMED; no recovery receipt', file=sys.stderr)
                    return 125
            break
        time.sleep(0.1)
    result = child.wait()
    if receipt != '-':
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({'reaped': True, 'normal_exit': not timed_out and result >= 0,
                       'returncode': result, 'cancelled_or_timed_out': timed_out}, out)
    return 124 if timed_out else (result if result >= 0 else 128 - result)

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--check-complete':
        try:
            with open(sys.argv[2]) as stream: result = json.load(stream)
            sys.exit(0 if result.get('reaped') is True and result.get('normal_exit') is True and result.get('returncode') == 0 else 1)
        except (OSError, ValueError, AttributeError):
            sys.exit(1)
    sys.exit(main())
