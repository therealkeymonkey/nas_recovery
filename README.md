# nas_recovery

Tooling to recover data from two 500 GB drives pulled from a dead Thecus N3200
NAS, copied over SSH/rsync to a Synology DS223 with a fresh 8 TB volume.

All operations against the source drives are **strictly read-only**: assembled
via `mdadm --assemble --readonly`, mounted with `-o ro,noload`. The drives
should leave this process bit-for-bit identical to how they came in.

## Prerequisites

```
sudo apt install mdadm smartmontools rsync
```

`lvm2`, `ssh`, and Python 3.11+ should already be on Ubuntu 24.04.

## One-time setup

```
cp config.example.toml config.toml
# edit config.toml — fill in the Synology host/user and confirm /dev paths

ssh-keygen -t ed25519                    # if you don't already have a key
ssh-copy-id <user>@<syno-host>           # passwordless rsync
ssh <user>@<syno-host> "ls /volume1/recovered/"   # smoke test
```

## Recovery workflow

Run as a regular user — the script invokes `sudo` itself for the privileged
steps so individual `apt`/`mdadm`/`mount` failures stay visible.

```
./recover.py inspect                     # read-only survey of the drives
./recover.py assemble --component /dev/sdb3 --component /dev/sdc3
./recover.py copy --dry-run              # sanity check what rsync will do
./recover.py copy                        # actual transfer
./recover.py verify                      # counts + checksum sample
./recover.py teardown                    # unmount and stop the array
```

`./recover.py status` shows current mount/array state at any time.

### What `inspect` is telling you

`mdadm --examine /dev/sdXN` will print metadata for each partition. The
partitions that belong to the same array share an **Array UUID** and report
the same **Raid Level**. On a Thecus N3200 the layout is typically:

| Partition  | Size      | Role                        |
|-----------|------------|-----------------------------|
| sdX1      | ~256 MB    | Thecus firmware / boot      |
| sdX2      | ~2 GB      | Swap or system              |
| sdX3      | ~rest      | Data — this is the RAID member |

The biggest partition with consistent `mdadm --examine` output across both
drives is the data array. Pass those as `--component` flags to `assemble`.

### Filesystem note

Thecus N3200 typically uses ext3 or ext4 on the data array, and `mount -o
ro,noload` skips journal replay (forensic-friendly). If the filesystem turns
out to be xfs/btrfs, drop `noload` from `[mount].mount_options` in
`config.toml` and re-run.

## Logs

Every subcommand writes to `logs/<timestamp>-<command>.log`. The rsync run
also produces `logs/rsync-<timestamp>.log` with per-file detail.

## Files

- `recover.py` — CLI orchestrator (subcommands above)
- `config.example.toml` — template, committed
- `config.toml` — host-specific values, gitignored
- `PLAN.md` — the worked-out recovery plan and rationale
