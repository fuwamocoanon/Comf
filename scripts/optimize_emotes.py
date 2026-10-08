#!/usr/bin/env python3
"""Shrink animated WebP emotes to fit Discord's 256 KB emoji limit.

Files that already fit are copied unchanged. Oversized files are re-encoded,
trying progressively stronger reductions until one fits:

  1. lossy re-encode at full size, full frame rate (highest quality that fits)
  2. same, with frames dropped (24 -> 16 -> 12 fps; timing is preserved)
  3. same, downscaled (Discord shows emotes at <=128px, so this is barely visible)

Usage:
    python scripts/optimize_emotes.py INPUT_DIR [OUTPUT_DIR] [--limit-kb 256]

Requires Pillow (pip install pillow).
"""

import argparse
import io
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageSequence

QUALITY_MIN = 30
QUALITY_MAX = 95
SIZE_STEPS = (256, 192, 160, 128)


def load_frames(path):
    """Return (frames, durations_ms, loop) with every frame fully composited as RGBA."""
    with Image.open(path) as im:
        loop = im.info.get("loop", 0)
        frames, durations = [], []
        for frame in ImageSequence.Iterator(im):
            frames.append(frame.convert("RGBA").copy())
            durations.append(frame.info.get("duration", 42))
    return frames, durations, loop


def decimate(frames, durations, keep_ratio):
    """Keep roughly `keep_ratio` of the frames, folding dropped frames' time into the kept ones."""
    if keep_ratio >= 1:
        return frames, durations
    out_frames, out_durations = [], []
    acc = 0.0
    for frame, duration in zip(frames, durations):
        acc += keep_ratio
        if acc >= 1 or not out_frames:
            if acc >= 1:
                acc -= 1
            out_frames.append(frame)
            out_durations.append(duration)
        else:
            out_durations[-1] += duration
    return out_frames, out_durations


def encode(frames, durations, loop, quality, size):
    if frames[0].width != size:
        frames = [f.resize((size, size), Image.LANCZOS) for f in frames]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=loop,
        lossless=False,
        quality=quality,
        alpha_quality=max(quality, 70),
        method=6,
        allow_mixed=True,
    )
    return buf.getvalue()


def best_quality(frames, durations, loop, size, limit):
    """Binary-search the highest quality that fits under `limit`. Returns bytes or None."""
    lo, hi, best = QUALITY_MIN, QUALITY_MAX, None
    while lo <= hi:
        mid = (lo + hi) // 2
        data = encode(frames, durations, loop, mid, size)
        if len(data) <= limit:
            best, lo = (data, mid), mid + 1
        else:
            hi = mid - 1
    return best


def optimize(path, limit):
    frames, durations, loop = load_frames(path)
    src_size = max(frames[0].size)
    fps = 1000 * len(frames) / max(sum(durations), 1)
    for target_fps in (fps, 16, 12):
        if target_fps > fps:
            continue
        f, d = decimate(frames, durations, target_fps / fps)
        for size in (s for s in SIZE_STEPS if s <= src_size):
            result = best_quality(f, d, loop, size, limit)
            if result:
                data, quality = result
                return data, f"q{quality} {size}px {len(f)} frames (~{target_fps:.0f}fps)"
    return None, "could not fit even at lowest settings"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path, nargs="?", help="default: <input_dir>_discord")
    parser.add_argument("--limit-kb", type=float, default=256, help="size limit in KiB (default 256)")
    args = parser.parse_args()

    out_dir = args.output_dir or args.input_dir.with_name(args.input_dir.name + "_discord")
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = int(args.limit_kb * 1024)

    files = sorted(p for p in args.input_dir.iterdir() if p.suffix.lower() == ".webp")
    if not files:
        sys.exit(f"No .webp files in {args.input_dir}")

    failed = 0
    for path in files:
        dest = out_dir / path.name
        size_kb = path.stat().st_size / 1024
        if path.stat().st_size <= limit:
            shutil.copy2(path, dest)
            print(f"  ok    {path.name}: {size_kb:.0f} KB (copied as-is)")
            continue
        data, note = optimize(path, limit)
        if data is None:
            failed += 1
            print(f"  FAIL  {path.name}: {size_kb:.0f} KB, {note}")
            continue
        dest.write_bytes(data)
        print(f"  shrunk {path.name}: {size_kb:.0f} KB -> {len(data) / 1024:.0f} KB ({note})")

    print(f"\n{len(files) - failed}/{len(files)} emotes ready in {out_dir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
