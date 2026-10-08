#!/usr/bin/env python3
"""Rename emote files so Discord accepts their names.

Discord emoji names may only contain letters, numbers and underscores, and must
be 2-32 characters long. This removes hyphens (and any other disallowed
characters) from every .webp/.png/.gif/.jpg in a folder, renaming in place.

Usage:
    python scripts/rename_emotes.py FOLDER [--dry-run] [--hyphen _]

  --dry-run   show what would be renamed without touching anything
  --hyphen X  replace hyphens with X instead of removing them (e.g. --hyphen _)
"""

import argparse
import re
import sys
from pathlib import Path

EXTENSIONS = {".webp", ".png", ".gif", ".jpg", ".jpeg"}


def clean_name(stem, hyphen):
    return re.sub(r"[^A-Za-z0-9_]", "", stem.replace("-", hyphen))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hyphen", default="", help="replacement for hyphens (default: remove)")
    args = parser.parse_args()

    files = sorted(p for p in args.folder.iterdir() if p.suffix.lower() in EXTENSIONS)
    if not files:
        sys.exit(f"No emote images in {args.folder}")

    renamed = problems = 0
    for path in files:
        stem = clean_name(path.stem, args.hyphen)
        if not 2 <= len(stem) <= 32:
            problems += 1
            print(f"  WARN  {path.name}: '{stem}' is {len(stem)} chars (Discord needs 2-32), rename it by hand")
        if stem == path.stem:
            continue
        dest = path.with_name(stem + path.suffix)
        if dest.exists():
            problems += 1
            print(f"  SKIP  {path.name}: {dest.name} already exists")
            continue
        if not args.dry_run:
            path.rename(dest)
        renamed += 1
        print(f"  {path.name} -> {dest.name}")

    verb = "would rename" if args.dry_run else "renamed"
    print(f"\n{verb} {renamed} of {len(files)} files" + (f", {problems} need attention" if problems else ""))


if __name__ == "__main__":
    main()
