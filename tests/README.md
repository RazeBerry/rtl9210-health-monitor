# Hardware-free regression tests

Run from the repository on macOS, using an internally installed Python 3.9+:

```sh
python3 -m unittest discover -s tests -v
bash -n rtl9210-health scripts/macos-sudo-askpass
shellcheck rtl9210-health scripts/macos-sudo-askpass
git diff --check
```

The suite never invokes the production front door against a real disk. Workflow
tests copy it into a disposable internal temporary directory, substitute every
hardware/privilege command, and use a fake compiler/native executable. There is
no hardware fallback. Fake `diskutil info` refuses ordinary paths, matching the
macOS interface; fake `df` supplies the filesystem device separately. Volume
fixtures also cover macOS responses with `MountPoint` but no `Mounted` key,
including explicit empty mount points, missing metadata and contradictory flags.
The only forwarded system parser is `plutil`, restricted
to fixture files or stdin. Tests never invoke real sudo, diskutil, chown or USB
capture. They signal only their own test processes. Fixtures use invented labels,
serials and UUIDs; none are collected from the host.

- `test_simulated.py`: inventory, manifest validation, internal-storage rejection,
  path-to-device resolution through `df -P` (including APFS Data firmlinks),
  child receipts, signal permissions, recovery deadlines and cleanup ordering.
- `test_workflow.py`: complete setup/probe, refusal paths, delayed identities,
  authorization failure, lost sudo monitor, cancellation, ownership return,
  missing receipts, independent asynchronous fake disk operations, atomic profile
  publication, output backpressure and final volume loss.
- `test_worker.py`: actual privilege prerequisite (mocked), root-owner readiness,
  root-owned sudo launcher EPERM before readiness (never itself authorization),
  cancellation, parent disappearance and retaining ownership of a live child even
  when status publication fails.
- `test_cancellation.py`: compiles a USB-free C harness using the production
  signal header, tests SIGINT/SIGTERM/SIGHUP at lifecycle boundaries, and checks
  that the native source includes cancellation checkpoints. It does not load
  IOUSBHost or run the native probe.

`/private/tmp` is used for disposable fixtures on macOS. Fake timeouts are shorter
than production timeouts; tests do not replace recovery decisions. Privileged
ownership and EPERM are simulated, not exercised with a real root process.

Compile-only native validation (do not execute the resulting binary):

```sh
mkdir -p .build
xcrun clang -arch arm64 -mmacosx-version-min=15.0 -fobjc-arc \
  -Wall -Wextra -Werror -framework Foundation -framework IOKit \
  -framework IOUSBHost src/rtl9210_health_probe.m \
  -o .build/rtl9210_health_probe
```

Passing these checks does not prove real sudo policy, Disk Arbitration scheduling,
USB rematching, encrypted-volume unlocking, firmware behavior or recovery after
SIGKILL/power loss. Hardware validation must be a separate, informed operation.
