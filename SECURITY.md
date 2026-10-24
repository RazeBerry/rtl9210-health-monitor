# Security and privacy

RTL9210 Health Monitor temporarily takes privileged ownership of a USB storage
device. Treat transport and target-selection changes as storage-safety work.

## Private local data

First-time enrollment writes exact bridge, SSD, volume, and filesystem
identities to `config/device.plist`. Health captures under `runs/` contain raw
NVMe pages, including the SSD serial in `identify_controller.bin`. Both paths
are gitignored and must remain private.

The human-readable summary deliberately redacts the SSD serial. Do not attach a
raw run directory or local device profile to a public issue. Create a minimal,
manually reviewed excerpt instead.

## Administrator authorization

The front door authorizes one fixed-purpose worker through `sudo -A` before
unmounting. `scripts/macos-sudo-askpass` presents a hidden-input AppleScript
dialog in the logged-in macOS GUI. No reusable sudo timestamp is assumed.
The helper's standard output is connected directly to `sudo`'s credential
reader. It does not write the password to disk, print it to the calling process
or place it in a command-line argument or environment variable.

Do not replace this with automation that types, logs, shells out with or stores
an administrator password. Cancel any authorization dialog that was not caused
by a `setup` or `probe` operation you initiated.

Before publishing a fork, scan both tracked content and Git history for:

- hardware serials and volume UUIDs;
- volume labels, mount paths, hostnames, usernames and email addresses;
- raw controller pages and logs containing absolute paths;
- accidentally force-added files that match `.gitignore`.

## Reporting a vulnerability

Open a public issue only when the report contains no device identifiers or raw
storage evidence. For a disclosure that cannot be safely anonymized, use the
repository owner's private GitHub contact channel rather than posting the data.

## Scope

This project is not a recovery, repair, firmware, or destructive-diagnostics
tool. It implements only data-in identity and health commands. A clean SMART
reading does not guarantee that a drive will not fail; maintain independent,
tested backups.

## Recovery boundary

Only a worker acknowledgement after native-child reaping permits restoration.
The release marker is evidence that the cleanup path returned, not proof that
macOS recovered the hardware. Recovery verifies the original volume manifest.
Uncertain mutation completion or missing ownership acknowledgement retains the
capture lock and requires operator inspection. SIGKILL, power loss, hostile
modification of the authorized checkout, and a stuck kernel/driver are outside
the guarantee. Keep the checkout and Python installation trusted and on internal
storage; the worker executes code from that checkout with administrator rights.

The regression fixtures contain synthetic identifiers only. They do not establish
real-device compatibility or validate kernel behavior.
