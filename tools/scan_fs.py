#!/usr/bin/env python3
"""Scan a block device for filesystem magic numbers, file-type signatures,
and printable strings.

Read-only. Useful when something has clobbered the primary superblock and
you're trying to figure out (a) what filesystem (if any) was there and
(b) roughly what user files are recoverable via carving.

Usage:
    sudo ./tools/scan_fs.py /dev/sdb2 [--gb 10] [--carve]
"""
from __future__ import annotations

import argparse
import re
import sys

# (signature_bytes, label) — searched in raw chunks.
SIGS: list[tuple[bytes, str]] = [
    (b"\x53\xef",      "ext2/3/4 magic (0xef53)"),
    (b"XFSB",          "XFS superblock"),
    (b"_BHRfS_M",      "btrfs superblock"),
    (b"ReIsErFs",      "reiserfs"),
    (b"ReIsEr2Fs",     "reiserfs2 (3.6+)"),
    (b"JFS1",          "JFS"),
    (b"NTFS    ",      "NTFS"),
    (b"LVM2 001",      "LVM2 PV label"),
    (b"\xeb\x58\x90mkfs.fat", "FAT (mkfs.fat marker)"),
    (b"SWAPSPACE2",    "Linux swap signature"),
]

# File-type signatures for the --carve estimate. Each entry is
# (magic_bytes, label). These count distinct headers; counts are approximate
# (e.g. a PDF that embeds JPEGs will inflate JPEG counts).
CARVE_SIGS: list[tuple[bytes, str]] = [
    (b"%PDF-1.",                       "PDF"),
    (b"\xff\xd8\xff\xe0",               "JPEG (JFIF)"),
    (b"\xff\xd8\xff\xe1",               "JPEG (Exif)"),
    (b"\x89PNG\r\n\x1a\n",              "PNG"),
    (b"GIF87a",                         "GIF87"),
    (b"GIF89a",                         "GIF89"),
    (b"II*\x00",                        "TIFF (LE)"),
    (b"MM\x00*",                        "TIFF (BE)"),
    (b"PK\x03\x04",                     "ZIP/DOCX/XLSX/JAR"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "Old Office (.doc/.xls/.ppt)"),
    (b"7z\xbc\xaf\x27\x1c",             "7-Zip"),
    (b"Rar!\x1a\x07\x00",                "RAR"),
    (b"\x1f\x8b\x08",                   "gzip"),
    (b"BZh",                            "bzip2"),
    (b"ID3",                            "MP3 (ID3)"),
    (b"\xff\xfb",                       "MP3 (MPEG frame)"),
    (b"fLaC",                           "FLAC"),
    (b"OggS",                           "Ogg"),
    (b"RIFF",                           "RIFF (WAV/AVI)"),
    (b"ftypmp4",                        "MP4"),
    (b"ftypisom",                       "MP4 (isom)"),
    (b"ftypM4A",                        "M4A"),
    (b"ftypqt",                         "QuickTime MOV"),
    (b"ftypheic",                       "HEIC"),
    (b"\x1aE\xdf\xa3",                  "Matroska/MKV/WebM"),
]

# strings(1) regex: runs of >=N printable ascii bytes
PRINTABLE = re.compile(rb"[\x20-\x7e]{8,}")
INTERESTING = re.compile(
    rb"(?i)(thecus|nas|raid|volume|share|backup|home|public|user|"
    rb"family|photo|video|movie|music|document)"
)


def scan(device: str, max_gb: int, carve: bool) -> None:
    chunk = 4 * 1024 * 1024  # 4 MB reads
    n_chunks = (max_gb * 1024) // 4
    mode = "carve estimate" if carve else "fs+strings survey"
    print(f"Scanning {device} for {max_gb} GB ({mode})...")
    print()

    fs_hits: list[tuple[str, int]] = []
    carve_counts: dict[str, int] = {label: 0 for _, label in CARVE_SIGS}
    interesting_strings: list[bytes] = []
    last_progress = 0

    with open(device, "rb") as f:
        for i in range(n_chunks):
            buf = f.read(chunk)
            if not buf:
                break

            for sig, name in SIGS:
                start = 0
                while True:
                    pos = buf.find(sig, start)
                    if pos < 0:
                        break
                    fs_hits.append((name, i * chunk + pos))
                    start = pos + 1
                    if len(fs_hits) > 200:
                        break

            if carve:
                for sig, label in CARVE_SIGS:
                    n = buf.count(sig)
                    if n:
                        carve_counts[label] += n
            else:
                for m in PRINTABLE.finditer(buf):
                    s = m.group(0)
                    if INTERESTING.search(s):
                        interesting_strings.append(s)

            scanned_gb = (i + 1) * chunk / (1024 ** 3)
            if scanned_gb - last_progress >= 1.0:
                print(f"  ...scanned {scanned_gb:.1f} GB", flush=True)
                last_progress = scanned_gb

    print()
    print("=== Filesystem signature hits ===")
    if not fs_hits:
        print("  (none)")
    else:
        for name, off in fs_hits[:30]:
            print(f"  {name:<35} 0x{off:x}  ({off:,})")
        if len(fs_hits) > 30:
            print(f"  ... and {len(fs_hits) - 30} more")

    if carve:
        print()
        print("=== File-type signature counts ===")
        any_hits = False
        for label, count in sorted(carve_counts.items(), key=lambda kv: -kv[1]):
            if count:
                print(f"  {label:<32} {count:>8,}")
                any_hits = True
        if not any_hits:
            print("  (none — no recognised file headers found)")
    else:
        print()
        print("=== Interesting printable strings (filtered, deduped) ===")
        if not interesting_strings:
            print("  (none)")
        else:
            seen: set[bytes] = set()
            shown = 0
            for s in interesting_strings:
                if s in seen:
                    continue
                seen.add(s)
                try:
                    print(f"  {s.decode('utf-8', errors='replace')[:160]}")
                    shown += 1
                    if shown >= 80:
                        print("  ... (output truncated)")
                        break
                except Exception:
                    pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("device")
    ap.add_argument("--gb", type=int, default=10,
                    help="How many GB from the start of the device to scan (default 10).")
    ap.add_argument("--carve", action="store_true",
                    help="Count file-type signatures (PDF/JPEG/MP4/etc.) instead of "
                         "dumping printable strings. Use for a recovery-volume estimate.")
    args = ap.parse_args()
    try:
        scan(args.device, args.gb, args.carve)
    except PermissionError:
        sys.exit(f"Permission denied reading {args.device}. Run with sudo.")
    except FileNotFoundError:
        sys.exit(f"No such device: {args.device}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")


if __name__ == "__main__":
    main()
