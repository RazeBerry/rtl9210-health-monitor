# RTL9210 Health Monitor

**Read NVMe SMART health data from a Realtek RTL9210 enclosure on macOS—even
when Disk Utility reports `SMART Status: Not Supported`.**

RTL9210 Health Monitor is a small, read-only macOS evidence collector for an
NVMe SSD hidden behind a Realtek RTL9210 USB bridge. It temporarily arbitrates the
USB device away from macOS, reads the controller identity, SMART/Health log and
Error Information log, then returns the disk to macOS and verifies that its
APFS volume remounted from the expected physical store.

**No kernel extension. No SIP changes. No virtual machine. No firmware
flashing.** The output is raw, checksummed evidence rather than a guessed health
score.

> **Search context:** `RTL9210`, `RTL9210B`, `SMART Status: Not Supported`,
> `IOUSBMassStorageUASDriver`, `kIOReturnNoResources` (`0xe00002be`),
> `IOUSBHostDevice`, `configureWithValue:0`, `matchInterfaces:NO`, Realtek
> vendor command `0xE4`, and smartmontools device type `sntrealtek`.

## Status and compatibility

The method has been validated on Apple silicon and macOS 26 with one Realtek
`0bda:9210` USB-to-NVMe bridge. Exact per-device identifiers and raw evidence
are intentionally excluded from this public repository.

This is a working reference implementation, not a universal compatibility
claim. Related RTL9210A/B/B-CG/C and RTL9220 searches may lead here, but those
variants, endpoint layouts and firmware behaviors have not all been tested.
Enrollment fails closed unless the selected disk identifies as RTL9210 and
exactly one `0bda:9210` bridge is attached.

This branch adds macOS 15 build compatibility and stricter capture/recovery
supervision. One explicitly authorized enrollment capture has completed on
Apple silicon and macOS 15 with an RTL9210 bridge, including normal unmount,
identity/health/error-log reads, native release, and verified restoration of
APFS and exFAT volumes. This is one successful hardware run, not universal
compatibility or hardware validation of every cancellation/failure path. Those
paths remain covered by offline simulation; keep this patch under review.

## Quick start

Requirements:

- Apple silicon, macOS 15 or later, and an administrator account;
- Python 3.9+ installed on internal storage;
- Xcode Command Line Tools;
- one RTL9210 enclosure connected, with other RTL9210 devices disconnected;
- one mounted, unlocked APFS volume on the target disk for enrollment;
- all target data volumes mounted and unlocked under `/Volumes/`;
- the checkout, output directory, Python and its runtime on internal storage;
- Time Machine and every other workload on that disk idle;
- another copy of important data.

Install:

```bash
git clone https://github.com/RazeBerry/rtl9210-health-monitor.git
cd rtl9210-health-monitor
```

### 1. Enroll one device

```bash
./rtl9210-health setup
```

For an automation agent that has already proved the exact targets with
`diskutil`, use the same front door without terminal prompts:

```bash
./rtl9210-health setup \
  --disk /dev/diskN \
  --volume /Volumes/Name \
  --confirm
```

Capture authorization uses a local macOS administrator-password dialog
through `sudo -A` (a logged-in GUI session is required). The bundled helper sends that password directly to `sudo`; it does not
return the credential to the calling agent, print it or store it. Existing
profiles still require explicit `--replace` to be overwritten.

Setup is an interactive, trust-on-first-use operation. It:

1. lists external physical USB disks;
2. requires you to enter the exact `/dev/diskN` for the RTL9210;
3. requires one mounted APFS volume path on that disk;
4. proves that the volume's physical store belongs to the selected disk;
5. requires typed `ENROLL` confirmation;
6. unmounts the whole disk and captures exactly one `0bda:9210` bridge;
7. reads the bridge and underlying NVMe identities plus health pages;
8. releases USB capture, requests remount, and verifies all original volumes;
9. writes exact identities to `config/device.plist` with mode `600`.

`config/device.plist` and all controller captures are gitignored. Do not
force-add them. The tracked `config/device.example.plist` documents the schema;
it is not a working profile and should not be edited into one by hand when
guarded setup is available.

### 2. Passively verify the enrolled device

```bash
./rtl9210-health status
```

`status` does not request administrator access, unmount the disk, capture USB or
send a controller command.

### 3. Capture current health

```bash
./rtl9210-health probe
```

The probe waits for typed `PROBE` confirmation and administrator authority. It
then briefly unmounts the enrolled disk, reads three NVMe pages, restores the
device and verifies the original set of mounted volumes.

An automation agent can use:

```bash
./rtl9210-health probe --confirm
```

This skips only the typed confirmation. It does not skip device matching, Time
Machine idleness, administrator authorization, unmount, transport validation,
driver restoration or remount verification.

Do **not** run `.build/rtl9210_health_probe` directly. The front door owns the
Time Machine preflight, disk-to-volume proof, confirmation, cleanup trap and
post-remount verification.

## Capture and recovery lifecycle

The front door takes an exclusive `.capture-lock`, verifies internal dependency
storage, snapshots all target volumes, and binds the selected BSD disk to its
actual USB registry entry. Volume names and counts are discovered; no device
serial, label or disk number is built in. Enrollment still requires an APFS
anchor and the supported single-store RTL9210 topology. Other filesystems may
coexist on that disk, but every captured volume needs a stable UUID. Locked or
unmounted data volumes are rejected before any mutation. EFI support partitions
are excluded from that requirement.

The profile location is fixed at `config/device.plist`; the former
`RTL9210_HEALTH_CONFIG` override is no longer accepted, so an external profile
cannot become unavailable midway through capture.

One authorized worker starts **before** unmount and owns the native child for
its entire lifetime. It handles cancellation and reaping with the same privilege
as the child, then returns ownership of private outputs. An unprivileged shell
never tries to kill a root child through a `sudo` monitor. Signals set a native
cancellation flag; USB cleanup occurs in normal execution context. Native release
completion and successful filesystem restoration are separate checks.

A timed-out or failed `diskutil` mutation may still leave asynchronous Disk
Arbitration work in progress. Such an outcome retains the lock and blocks further
mutations; merely observing a CLI exit or a mounted volume is insufficient.
Recovery waits for exact volume identities, then verifies every original UUID,
name, path, filesystem and physical store before publishing `latest`. Critical
output goes to an internal transaction log, so a stalled terminal reader cannot
hold up recovery.

Exit status 70 indicates unconfirmed recovery. Do not retry capture or delete the
lock without inspecting the prior run and actual device state. No force mount,
repair, formatting or automatic lock expiry is implemented. An uncatchable shell
termination, kernel hang, power loss or physical disconnection cannot be repaired
by an EXIT trap; the worker can release its child but cannot guarantee remount
without the front door. A retained lock is intentional in uncertain cases.

## Private outputs

A successful run is stored locally under `runs/YYYYMMDD_HHMMSS/`:

```text
runs/20260101_120000/
├── identify_controller.bin   # raw 4 KiB; contains the SSD serial
├── smart_health_log.bin      # raw 512 B NVMe SMART/Health log 0x02
├── error_info_log.bin        # raw 512 B NVMe Error Information log 0x01
├── summary.txt               # human-readable; serial deliberately redacted
├── probe.log                 # USB transport and restoration evidence
└── SHA256SUMS                # integrity hashes for captured artifacts
```

Runs also contain private volume manifests, process acknowledgements, release
status, mutation receipts and a transaction log. Enrollment runs also contain `enrollment.plist`, which is private. `latest`
points to the newest successful run. Failed runs are retained for local
diagnosis but never replace `latest`.

Never attach the raw directory to a public issue. The Identify Controller page
contains a hardware serial, and health counters can fingerprint a device when
combined with dates and firmware information.

## When to use it

Appropriate uses include:

- macOS uses an external NVMe disk normally but reports SMART as unsupported;
- a known RTL9210 bridge is the only device of that family attached;
- you need a reproducible baseline or periodic controller-health snapshot;
- you need private evidence for trend analysis, migration, support or warranty;
- you understand that the operation temporarily resets USB storage.

Reasonable capture times include initial enrollment, quarterly or semiannual
checks, after filesystem and backup verification following an abnormal
disconnect, before migration, or when temperature, performance, disconnect or
I/O behavior changes.

There is little benefit in probing daily. For a backup disk, completed and
restorable backups matter more than frequent healthy SMART snapshots.

## When not to use it

Do not use this project:

- on a disk with active read failures before securing another copy;
- while Time Machine, file copies or another workload are active;
- on SATA media, RAID, multi-drive enclosures or non-Realtek bridges;
- on a different Realtek generation without validating its command and endpoint
  layout;
- as a substitute for filesystem checks, restoration tests or professional
  recovery;
- for self-tests, repair, firmware updates, format, sanitize or namespace
  management—none of those operations is implemented.

## Why the usual tools did not work

The ecosystem already contained pieces of the answer, but not the complete
macOS transport:

| Existing component | What it provides | Missing layer on this path |
|---|---|---|
| smartmontools `sntrealtek` | Realtek's vendor-command encoding for NVMe behind RTL9210 | The device type is excluded from the Darwin build section because the needed generic SCSI transport is unavailable there. |
| Apple's `SCSITask` interfaces | A possible user-space SCSI route | The installed plug-in would not attach to the already claimed logical unit. |
| Apple's `IOUSBHostDevice` API | USB capture, configuration and interface primitives | Device capture alone allowed the UAS interface driver to re-bind; opening the interface returned `kIOReturnNoResources`. |
| QEMU plus Linux smartmontools | A guest where `sntrealtek` normally has a transport | The bridge stalled while setting USB configuration 1, so Linux never received a block device. |
| macOS SAT SMART drivers | Pass-through for some external SATA/ATA devices | NVMe behind the Realtek translation layer is not ATA behind SAT. |
| RTL9210 firmware tools | Firmware flashing and bridge configuration | They do not provide read-only NVMe health access on macOS and add destructive risk. |

The missing piece was safe ownership of interface 0. The tested bridge exposes
Bulk-Only Transport (BOT, protocol `0x50`) as alternate setting 0 and USB
Attached SCSI (UAS, protocol `0x62`) as alternate setting 1. macOS binds
`IOUSBMassStorageUASDriver` to that interface.

```text
Capture exact IOUSBHostDevice
            │
            ▼
configureWithValue:0 matchInterfaces:NO
unconfigure device; tear down the UAS interface
            │
            ▼
configureWithValue:1 matchInterfaces:NO
recreate interfaces without driver matching
            │
            ▼
Open interface 0; select BOT alternate setting 0
            │
            ▼
Send read-only SCSI INQUIRY and Realtek 0xE4 data-in commands
            │
            ▼
Destroy capture; re-register drivers; remount and verify APFS
```

The Realtek command encoding follows the command layer implemented by
smartmontools. This project supplies the missing macOS USB-driver arbitration
and guarded operator workflow.

## Read-only safety contract

Routine probes fail unless all of these agree:

- one external whole USB disk matches the enrolled media name and byte size;
- the enrolled APFS volume UUID is mounted at its enrolled path;
- that volume's APFS physical store belongs to the matched disk;
- Time Machine is idle;
- bridge VID, PID, revision and USB serial match enrollment;
- underlying NVMe model and serial match enrollment;
- whole-disk unmount succeeds;
- every USB transfer has the exact expected length;
- every BOT Command Status Wrapper has the expected signature and command tag,
  success status and zero residue;
- every originally mounted volume remounts with matching identity and path afterward.

The native probe implements only data-in commands:

| Command | Purpose |
|---|---|
| SCSI INQUIRY (`0x12`) | Confirm the bridge-facing logical unit |
| Realtek `0xE4` → NVMe Identify Controller (`0x06`, CNS 1) | Read and match SSD identity and firmware |
| Realtek `0xE4` → NVMe Get Log Page (`0x02`, log `0x02`) | Read the SMART/Health log |
| Realtek `0xE4` → NVMe Get Log Page (`0x02`, log `0x01`) | Read the Error Information log |

No data-out storage command exists in the code. Hardware interaction is still
not risk-free: the tool temporarily unmounts and resets USB storage. Maintain
independent backups.

## Reading the health fields

| Field | Interpretation |
|---|---|
| `Critical Warning` | Current NVMe warning bitmask. `0x00` means no warning was asserted at capture time. |
| `Percentage Used` | Manufacturer estimate of endurance consumed. Reaching 100% does not itself mean immediate failure; trend it. |
| `Available Spare` / threshold | Normalized spare capacity and its warning threshold. |
| `Composite Temperature` | Controller-reported capture-time temperature; compare with the SSD vendor's specification. |
| `Data Units Read/Written` | Units of 512,000 bytes, not 512 bytes. |
| `Unsafe Shutdowns` | Cumulative power-loss/reset events. Nonzero is not proof of NAND damage; investigate changes and correlated filesystem evidence. |
| `Media/Data Integrity Errors` | Controller-recorded unrecovered media or integrity errors. Nonzero or increasing values need investigation. |
| `Error Info Log Entries` | Lifetime controller error-log count; focus on changes and inspect the private raw log. |

SMART is evidence, not a guarantee. Drives can fail suddenly after clean
telemetry. Use SMART trends, filesystem status, host I/O logs and verified
restoration together.

## Troubleshooting

| Message | Action |
|---|---|
| `no enrolled device profile` | Run `./rtl9210-health setup`. |
| `expected one enrolled RTL9210 disk; found 0` | Reconnect the enrolled device. Do not weaken the identity guard. |
| `Time Machine is running` | Wait for it to finish. |
| `enrolled volume UUID is unavailable` | Mount and unlock the enrolled APFS volume. |
| `could not unmount` | Close files, shells and applications using the volume. |
| Administrator prompt does not appear | Run from a logged-in macOS GUI session, or use the interactive command in Terminal. |
| Build cannot find `xcrun` or SDK headers | Install Command Line Tools with `xcode-select --install`. |
| Probe fails after unmount | The recovery trap attempts remount. Review the private `probe.log`; `latest` is unchanged. |
| Disk does not remount | Identify it with `diskutil list external physical`, verify model and size, then mount the verified dynamic identifier. Never assume a fixed `diskN`. |

If recovery is uncertain, stop issuing commands and inspect the device in Disk
Utility. Do not unplug it while macOS is restoring its driver or mounting APFS.

## Agent quick reference

```yaml
project: RTL9210 Health Monitor
platform: macOS; IOUSBHost user space
problem: NVMe SMART hidden behind Realtek RTL9210; diskutil reports SMART Not Supported
root_cause: IOUSBMassStorageUASDriver owns interface 0 and rebinds after ordinary capture
observed_open_failure: kIOReturnNoResources (0xe00002be)
protocol_reference: smartmontools sntrealtek / scsinvme.cpp
working_sequence:
  - capture exact IOUSBHostDevice
  - configureWithValue:0 matchInterfaces:NO
  - configureWithValue:1 matchInterfaces:NO
  - open driverless interface 0
  - select BOT alternate setting 0
  - send Realtek 0xE4 data-in commands
teardown: destroy interface and device; rematch drivers; remount and verify APFS
entrypoints:
  - ./rtl9210-health setup  # interactive enrollment
  - ./rtl9210-health setup --disk /dev/diskN --volume /Volumes/Name --confirm
  - ./rtl9210-health status
  - ./rtl9210-health probe  # interactive confirmation
  - ./rtl9210-health probe --confirm  # agent/non-interactive confirmation
authorization: single sudo -A worker with local macOS dialog
private_paths:
  - config/device.plist
  - runs/
never_commit: serials, UUIDs, raw pages, mount labels, usernames, emails, absolute user paths
never_add: data-out, firmware, format, sanitize, self-test, namespace commands
```

See [AGENTS.md](AGENTS.md) for mandatory modification and verification rules.
See [tests/README.md](tests/README.md) for hardware-free validation and its limits.

## Project layout

```text
rtl9210-health                         guarded operator front door
scripts/macos-sudo-askpass             local GUI authorization bridge
src/rtl9210_health_probe.m             native IOUSBHost/BOT implementation
config/device.example.plist            public schema with placeholders
config/device.plist                    private enrolled identity; ignored
docs/rtl9210_macos_smart_passthrough.md sanitized technical record
SECURITY.md                            disclosure and privacy boundary
runs/                                  private captures; ignored
```

## Technical references

- [smartmontools official repository](https://github.com/smartmontools/smartmontools)
  and [`smartctl` device-type documentation](https://github.com/smartmontools/smartmontools/blob/main/src/smartctl.8.in#L3121-L3163)
- [Apple `configureWithValue:matchInterfaces:error:`](https://developer.apple.com/documentation/iousbhost/iousbhostdevice/configurewithvalue%3Amatchinterfaces%3Aerror%3A)
- [NVM Express: SSD management, error reporting and logging](https://nvmexpress.org/how-ssds-fail-nvme-ssd-management-error-reporting-and-logging-capabilities/)

No packaged project was found during the original investigation that combined
the RTL9210 command layer with the macOS `configureWithValue:0` /
`matchInterfaces:NO` sequence. That is a search result, not a claim that no
independent implementation can exist.

## License

No license has been selected. Publication makes the source inspectable but does
not automatically grant reuse rights. Add an explicit license before treating
this as an open-source release.
