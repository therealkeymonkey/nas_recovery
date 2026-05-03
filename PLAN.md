# NAS Recovery Plan

Source: two 500 GB drives from a dead **Thecus N3200**.
Target: a brand-new **Synology DS223** with a single 8 TB drive, reached over
SSH from this Ubuntu 24.04 box.

This plan has evolved as we learned what's actually on the drives. The
**Original plan** is preserved at the bottom for context; the **Current plan**
section above reflects the reality.

---

## What we now know about the drives (post-inspection, 2026-05-03)

Inspection of `/dev/sdb` revealed a substantially more complex situation than
expected. Findings, in order of severity:

### 1. Drive health: good

`smartctl -a /dev/sdb` reports overall PASSED, 0 reallocated sectors, 0
pending sectors, 0 errors. The 238,817 power-on hours value is bogus — known
counter-glitch on this generation of WD Caviar Blue. The drive itself is
healthy and reads cleanly.

### 2. The Thecus N3200 is big-endian (PowerPC); mdadm metadata is byte-reversed

`mdadm --examine` failed on every partition with:

```
mdadm: No super block found on /dev/sdb1 (Expected magic a92b4efc, got fc4e2ba9)
```

Those two values are exact byte-reversals. The Thecus N3200 uses a PowerPC
CPU (big-endian), so its mdadm 0.90 metadata was written in BE. Our x86 box
reads it in LE order and sees gibberish. Manual decode of the raw superblock
at the end of `sdb2` (where v0.90 lives) confirms: **RAID 1, 2 disks
expected, Array UUID `e7bfc5b9-3ae8-7986-2341-5357fdfa4268`** — matching
what `blkid` reports (libblkid handles BE 0.90 correctly; mdadm doesn't).

`blkid` and `lsblk` see the partitions as `linux_raid_member` v0.90.3 just
fine. So the metadata is intact, just unreadable by our `mdadm`.

### 3. The data partition has been overwritten with `mkswap`

`file -s /dev/sdb2` reports a Linux swap signature; `dumpe2fs` finds no ext
backup superblocks at any of the standard offsets (4K or 1K block sizes).
A loop-device test with the trailing RAID magic hidden still reports `swap`,
not LVM or any filesystem.

A 20 GB raw scan via `tools/scan_fs.py --carve` shows:

| Signature                       | Count       |
|----------------------------------|-------------|
| MP3 frames (ff fb / ID3)         | ~6.8 M (~1500–2500 songs in 20 GB) |
| Ogg pages                         | 220,124     |
| JPEG (Exif + JFIF)               | 367         |
| TIFF                              | 345         |
| ZIP/DOCX/XLSX/JAR                 | 109         |
| PNG                               | 28          |
| MP4 / MOV / MKV                   | 15          |
| PDF                               | 12          |
| Old Office (.doc/.xls/.ppt)       | 7           |

**Conclusion:** `mkswap` was run over what was an ext3/4 data partition.
`mkswap` only writes ~4 KB at the start of the partition, so the actual file
content is intact behind it. The filesystem *structure* (primary superblock,
inode table, directory entries, journal) is gone, but the file *bytes* are
still there. Recovery via file-carving is feasible but will lose original
filenames and directory structure. The earlier strings dump confirmed real
PDF objects, Adobe InDesign metadata, and Samba config fragments.

It's not yet known whether this happened during a botched Thecus operation,
a manual wipe attempt, or something else. It doesn't change the recovery
path.

### 4. Volume estimate

Extrapolating the 20 GB scan to the full 463 GB partition: a substantial
personal media collection — primarily music (Ogg + MP3, possibly tens of
thousands of songs), several hundred photos, dozens of documents, a handful
of videos. Worth recovering.

---

## Current plan

Five phases. Phases 0–1 happened today; Phases 2–5 wait for the Synology to
come online.

### Phase 0 — Inspect [done 2026-05-03]

- `smartctl -a` confirmed drive health.
- `mdadm --examine` failed in the expected way (BE metadata).
- `tools/scan_fs.py --carve` confirmed real file content under the swap
  header, with a music + photo + document mix.
- Drive 1 (`sdb`) characterized; drive 2 (`sdc`) not yet inspected (single
  SATA-USB adapter, one drive at a time).

### Phase 1 — Synology DS223 setup [pending — bottleneck]

Recovery cannot proceed until this is done because we need:
(a) somewhere safe to put the ~500 GB raw image and (b) somewhere for the
~tens-of-thousands of carved files to land. The 8 TB single-drive Synology
gives both, with headroom.

Steps:
1. Install the 8 TB drive in the DS223; power on; connect to LAN.
2. Find on `https://find.synology.com`; run DSM setup; create admin user.
3. Storage Pool + Volume on the new drive (SHR or Basic; btrfs).
4. Create shared folder `recovered` (target path `/volume1/recovered/`).
5. Control Panel → Terminal & SNMP → enable SSH.
6. Note IP; set DHCP reservation.
7. From this box: `ssh-copy-id admin@<syno-ip>`; smoke-test SSH.

### Phase 2 — Image drive 1 (`ddrescue`)

Reconnect SATA-USB with drive 1 (`sdb`). Then from this box:

```
sudo ddrescue -d -r3 /dev/sdb \
    /mnt/syno/recovered/sdb.img \
    /mnt/syno/recovered/sdb.map
```

Where `/mnt/syno/recovered/` is an SMB or NFS mount of the Synology share, or
we pipe via SSH. `ddrescue` is bit-for-bit, retries bad sectors, and the
`.map` file makes it resumable. ETA ~2 h on USB 3 + GbE.

Disconnect drive 1, swap to drive 2, repeat to `sdc.img`/`sdc.map`.

### Phase 3 — Carve (PhotoRec)

Recovery now happens entirely against the on-Synology images — the originals
are no longer touched. Loop-mount the image:

```
sudo losetup --find --show --read-only \
    --offset $((5028345 * 512)) \
    --sizelimit $((971739720 * 512)) \
    /mnt/syno/recovered/sdb.img
```

(That picks out the `sdb2` partition from the full-disk image — offset and
size from the partition table we recorded.)

Then `photorec` on the loop device, output directory under
`/mnt/syno/recovered/carved-sdb/`. PhotoRec is interactive but supports
non-interactive use via `photorec /d <outdir> /cmd <device> partition_none,options,fileopt,everything,enable,search`.

Repeat for `sdc.img` into `carved-sdc/`. Then de-duplicate across the two
output directories — `sdb` and `sdc` were RAID 1 mirrors so most files will
be identical between them; sectors that came back damaged on one may be
intact on the other.

### Phase 4 — Triage & verify

PhotoRec output is a sea of numbered files grouped by type. Triage:
- Sample several files of each type to confirm they open cleanly (PDFs
  render, JPEGs display, MP3s play).
- Some media files have embedded metadata (ID3 tags, Exif, PDF info dict)
  that lets us reconstruct partial structure or file-name hints.
- For music libraries, tools like `beets` or `MusicBrainz Picard` can
  re-tag and rename based on audio fingerprint — useful given the carved
  files lose their filenames.

### Phase 5 — Cleanup

- Once recovered files are validated, optionally delete the `.img` clones to
  free the ~1 TB they consume on the Synology.
- Keep the source drives shelved (untouched) as a final backstop.

---

## What changed vs. the original plan

| Original assumption | Reality |
|---------------------|---------|
| Plain `mdadm --assemble` would work | Metadata is big-endian; mdadm refuses |
| Mount `/dev/md127` read-only and rsync the file tree | No filesystem to mount — `mkswap` clobbered the primary superblock and there are no surviving ext backup superblocks |
| Skip imaging because drives are healthy | We now want a `ddrescue` clone before any carving — it's a one-shot read of an old drive and we want a working copy |
| `recover.py` orchestrates the rsync workflow | It's no longer the right tool for the actual recovery; keep it around as a reference and write new helpers under `tools/` for the carving workflow if needed |

`recover.py` and the rsync flow remain useful as a template for the *next*
NAS-recovery situation that goes the easy way; not deleting it.

---

## Open questions / risks

- **Drive 2 (`sdc`) not yet inspected.** Almost certainly the RAID 1 mirror
  twin — same partition layout, same swap-overlay damage — but we'll confirm
  during Phase 2 imaging.
- **Lost filenames and directory structure.** Carving cannot recover those.
  Music is largely re-taggable by audio fingerprint; photos retain Exif but
  not folder organization; documents will be `f00001.pdf`-style. This is the
  cost we're paying for the lost ext metadata.
- **Fragmented files.** PhotoRec recovers files that are sequential on disk;
  files split across non-contiguous extents may come out partial. Modern ext
  defrags reasonably aggressively, so most files should be intact.
- **Unknown how `mkswap` happened.** Doesn't affect recovery, but worth
  noting in case it tells us anything about whether further damage occurred
  beyond the visible swap-header overlay.

---

## Original plan (superseded — kept for reference)

The plan we worked out before inspecting the drives was an `mdadm` →
read-only mount → `rsync` over SSH workflow. The `recover.py` CLI still
implements that flow with subcommands `inspect` / `assemble` / `copy` /
`verify` / `teardown`. It would be the right tool if the drives had been
intact ext-on-mdadm; it's preserved in the repo as a reusable template for
the next time something is broken in a more cooperative way.
