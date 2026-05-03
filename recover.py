#!/usr/bin/env python3
"""NAS recovery orchestrator: Thecus N3200 -> Synology DS223 over rsync/SSH.

All operations on the source drives are strictly read-only. Run subcommands
in order: inspect -> assemble -> copy -> verify -> teardown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import random
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence

CONFIG_PATH = Path(__file__).parent / "config.toml"
LOG = logging.getLogger("recover")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit("config.toml not found. Copy config.example.toml to config.toml and edit it.")
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def setup_logging(log_dir: str, command: str) -> Path:
    p = Path(log_dir)
    p.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = p / f"{stamp}-{command}.log"

    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    LOG.addHandler(fh)
    LOG.addHandler(ch)
    return log_file


def run(cmd: Sequence[str], *, check: bool = True, capture: bool = False, sudo: bool = False) -> subprocess.CompletedProcess:
    if sudo and os.geteuid() != 0:
        cmd = ["sudo", *cmd]
    LOG.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    result = subprocess.run(cmd, check=False, text=True, capture_output=capture)
    if capture:
        for line in (result.stdout or "").rstrip().splitlines():
            LOG.info("  | %s", line)
        for line in (result.stderr or "").rstrip().splitlines():
            LOG.info("  ! %s", line)
    if check and result.returncode != 0:
        raise SystemExit(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
    return result


def confirm(prompt: str, *, assume_yes: bool) -> None:
    if assume_yes:
        LOG.info("[--yes] %s", prompt)
        return
    answer = input(f"{prompt} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        sys.exit("Aborted.")


def require_tools(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        sys.exit(
            f"Missing tool(s): {', '.join(missing)}. "
            f"Try: sudo apt install mdadm smartmontools"
        )


def list_partitions(devices: list[str]) -> list[str]:
    """Return /dev/sdXN for every partition under the given disks."""
    parts: list[str] = []
    out = run(["lsblk", "-rno", "NAME,TYPE", *devices], capture=True, check=False, sudo=True).stdout
    for line in out.splitlines():
        name, _, typ = line.partition(" ")
        if typ.strip() == "part":
            parts.append(f"/dev/{name}")
    return parts


def cmd_inspect(args: argparse.Namespace, cfg: dict) -> None:
    require_tools("smartctl", "lsblk", "mdadm")
    devices: list[str] = cfg["source"]["devices"]
    LOG.info("=== Inspecting source devices: %s ===", ", ".join(devices))
    for dev in devices:
        if not Path(dev).exists():
            LOG.warning("Device %s does not exist; skipping.", dev)
            continue
        LOG.info("--- SMART: %s ---", dev)
        run(["smartctl", "-a", dev], check=False, capture=True, sudo=True)
        LOG.info("--- Partitions: %s ---", dev)
        run(["lsblk", "-O", dev], check=False, capture=True, sudo=True)

    LOG.info("=== mdadm --examine on candidate partitions ===")
    for part in list_partitions(devices):
        LOG.info("--- mdadm --examine %s ---", part)
        run(["mdadm", "--examine", part], check=False, capture=True, sudo=True)

    LOG.info("Inspection complete. Look in the log for partitions reporting an "
             "Array UUID and the same RAID Level — those are your components.")


def cmd_assemble(args: argparse.Namespace, cfg: dict) -> None:
    require_tools("mdadm", "mount")
    md = cfg["mount"]["md_device"]
    mountpoint = cfg["mount"]["mountpoint"]
    mount_opts = cfg["mount"].get("mount_options", "ro")

    if not args.components:
        sys.exit("Pass --component /dev/sdXN at least once. Identify them via `inspect`.")

    confirm(
        f"Assemble {md} READ-ONLY from {args.components} and mount at {mountpoint} (-o {mount_opts})?",
        assume_yes=args.yes,
    )

    # --run lets mdadm start the array even if fewer drives are present than
    # the metadata expects (we're recovering one RAID 1 member at a time).
    run(["mdadm", "--assemble", "--readonly", "--run", md, *args.components], sudo=True)
    run(["vgscan"], check=False, sudo=True)
    run(["vgchange", "-ay", "--readonly"], check=False, sudo=True)

    Path(mountpoint).mkdir(parents=True, exist_ok=True)
    try:
        run(["mount", "-o", mount_opts, md, mountpoint], sudo=True)
    except SystemExit:
        if "noload" in mount_opts:
            LOG.warning("Mount with %s failed; retrying with bare 'ro' (xfs/btrfs maybe?).", mount_opts)
            run(["mount", "-o", "ro", md, mountpoint], sudo=True)
        else:
            raise

    LOG.info("Mounted %s at %s. Top-level entries:", md, mountpoint)
    run(["ls", "-la", mountpoint], capture=True, sudo=True)


def _rsync_target(cfg: dict) -> str:
    t = cfg["target"]
    if not t.get("host"):
        sys.exit("config.toml: [target].host is empty. Set the Synology IP/hostname first.")
    return f"{t['user']}@{t['host']}:{t['remote_path']}"


def cmd_copy(args: argparse.Namespace, cfg: dict) -> None:
    require_tools("rsync", "ssh")
    src = cfg["mount"]["mountpoint"]
    if not os.path.ismount(src):
        sys.exit(f"{src} is not mounted. Run `assemble` first.")
    target = _rsync_target(cfg)
    excludes = cfg["copy"].get("excludes", [])
    log_dir = Path(cfg["copy"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    rsync_log = log_dir / f"rsync-{dt.datetime.now():%Y%m%d-%H%M%S}.log"

    cmd = [
        "rsync",
        "-aHAX",
        "--numeric-ids",
        "--info=progress2",
        "--partial",
        "--partial-dir=.rsync-partial",
        f"--log-file={rsync_log}",
    ]
    cmd += [f"--exclude={e}" for e in excludes]
    if args.dry_run:
        cmd.append("--dry-run")
    cmd += [f"{src.rstrip('/')}/", target]

    confirm(
        f"Run rsync{' (DRY RUN)' if args.dry_run else ''} from {src}/ to {target}?",
        assume_yes=args.yes,
    )
    run(cmd, sudo=True)
    LOG.info("rsync log: %s", rsync_log)


def _shellquote(s: str) -> str:
    return shlex.quote(s)


def cmd_verify(args: argparse.Namespace, cfg: dict) -> None:
    require_tools("ssh", "sha256sum")
    src = cfg["mount"]["mountpoint"]
    target = _rsync_target(cfg)
    user_host, _, remote_path = target.partition(":")
    if not os.path.ismount(src):
        sys.exit(f"{src} is not mounted. Run `assemble` first.")

    LOG.info("Counting source files & bytes...")
    src_files = run(["bash", "-c", f"find {_shellquote(src)} -type f | wc -l"],
                    capture=True, sudo=True).stdout.strip()
    src_bytes = run(["bash", "-c", f"du -sb {_shellquote(src)} | cut -f1"],
                    capture=True, sudo=True).stdout.strip()
    LOG.info("source: %s files, %s bytes", src_files, src_bytes)

    LOG.info("Counting target files & bytes...")
    dst_files = run(["ssh", user_host, f"find {_shellquote(remote_path)} -type f | wc -l"],
                    capture=True).stdout.strip()
    dst_bytes = run(["ssh", user_host, f"du -sb {_shellquote(remote_path)} | cut -f1"],
                    capture=True).stdout.strip()
    LOG.info("target: %s files, %s bytes", dst_files, dst_bytes)

    if src_files == dst_files and src_bytes == dst_bytes:
        LOG.info("OK: file count and total size match.")
    else:
        LOG.warning("MISMATCH: counts differ. Investigate before declaring done.")

    sample = int(cfg.get("verify", {}).get("sample_size", 25))
    LOG.info("Spot-checking %d random files with sha256...", sample)
    listing = run(
        ["bash", "-c", f"cd {_shellquote(src)} && find . -type f -printf '%P\\n'"],
        capture=True, sudo=True,
    ).stdout.splitlines()
    if not listing:
        LOG.warning("No files found to sample.")
        return

    picks = random.sample(listing, min(sample, len(listing)))
    mismatches: list[str] = []
    for rel in picks:
        local = run(
            ["bash", "-c", f"sha256sum {_shellquote(os.path.join(src, rel))} | cut -d' ' -f1"],
            capture=True, sudo=True, check=False,
        ).stdout.strip()
        remote = run(
            ["ssh", user_host,
             f"sha256sum {_shellquote(os.path.join(remote_path, rel))} | cut -d' ' -f1"],
            capture=True, check=False,
        ).stdout.strip()
        ok = local and remote and local == remote
        LOG.info("%s  %s", "OK " if ok else "BAD", rel)
        if not ok:
            mismatches.append(rel)

    if mismatches:
        LOG.warning("%d/%d sampled files failed checksum.", len(mismatches), len(picks))
    else:
        LOG.info("All %d sampled files match.", len(picks))


def cmd_teardown(args: argparse.Namespace, cfg: dict) -> None:
    md = cfg["mount"]["md_device"]
    mountpoint = cfg["mount"]["mountpoint"]
    confirm(f"Unmount {mountpoint} and stop {md}?", assume_yes=args.yes)
    run(["umount", mountpoint], check=False, sudo=True)
    run(["vgchange", "-an"], check=False, sudo=True)
    run(["mdadm", "--stop", md], check=False, sudo=True)
    LOG.info("Teardown complete.")


def cmd_status(args: argparse.Namespace, cfg: dict) -> None:
    LOG.info("--- /proc/mdstat ---")
    run(["cat", "/proc/mdstat"], check=False, capture=True)
    LOG.info("--- mountpoint check (%s) ---", cfg["mount"]["mountpoint"])
    run(["mountpoint", cfg["mount"]["mountpoint"]], check=False, capture=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sp = p.add_subparsers(dest="command", required=True)

    s = sp.add_parser("inspect", help="Read-only survey of the source devices.")
    s.set_defaults(func=cmd_inspect)

    s = sp.add_parser("assemble", help="Read-only mdadm assemble + mount.")
    s.add_argument("--component", dest="components", action="append",
                   help="Source partition (e.g. /dev/sdb3). Repeat for each member.")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_assemble)

    s = sp.add_parser("copy", help="rsync the mounted filesystem to the Synology.")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_copy)

    s = sp.add_parser("verify", help="Compare counts and spot-check checksums.")
    s.set_defaults(func=cmd_verify)

    s = sp.add_parser("teardown", help="Unmount and stop the array.")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_teardown)

    s = sp.add_parser("status", help="Show current mount/array state.")
    s.set_defaults(func=cmd_status)

    args = p.parse_args()
    cfg = load_config()
    setup_logging(cfg.get("copy", {}).get("log_dir", "logs"), args.command)
    LOG.info("=== nas_recovery: %s ===", args.command)
    args.func(args, cfg)


if __name__ == "__main__":
    main()
