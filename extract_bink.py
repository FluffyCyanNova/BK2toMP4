#!/usr/bin/env python3
"""
extract_bink.py - carve Bink 1/Bink 2 video streams out of an RPF9 archive.

Works without knowing the RPF9 TOC key: Bink streams inside such archives are
stored as plain data, not inside the encrypted directory metadata. We scan the
archive for Bink magic bytes and validate each hit against the Bink file
structure (dimensions, frame rate, frame index monotonicity). Only strictly
valid Bink streams are extracted.

Algorithm ported from the clean-room C# implementation in
https://github.com/breadbbbb/rpf9-explorer (Core/Rpf9MediaRecovery.cs).

Usage:
    python extract_bink.py <input.rpf> <output_dir> [--list]
"""
import argparse
import os
import struct
import sys
import time
from pathlib import Path

PAGE_SIZE = 4096
MAX_RECORDS = 20_000
MAX_FRAMES = 1_000_000
READ_BUDGET = 64 * 1024 * 1024
MAX_CONSECUTIVE_ZERO_PAGES = 256

# --- Colors ------------------------------------------------------------------
if os.name == "nt":
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        pass

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(c: str) -> str: return c if USE_COLOR else ""
RESET  = _c("\033[0m");  BOLD = _c("\033[1m");  DIM = _c("\033[2m")
ACCENT = _c("\033[38;5;215m")  # warm orange
ACC2   = _c("\033[38;5;180m")
MUTE   = _c("\033[38;5;244m")
SOFT   = _c("\033[38;5;250m")
OK     = _c("\033[38;5;108m")
ERR    = _c("\033[38;5;174m")


def align_page(v: int) -> int:
    return (v + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)


def fmt_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024: return f"{n:,.1f} {u}"
        n /= 1024
    return f"{n:,.1f} PB"


class BinkInfo:
    __slots__ = ("sig", "length", "frames", "width", "height",
                 "fps_num", "fps_den", "tracks")
    def __init__(self, sig, length, frames, width, height, fps_num, fps_den, tracks):
        self.sig, self.length = sig, length
        self.frames, self.width, self.height = frames, width, height
        self.fps_num, self.fps_den, self.tracks = fps_num, fps_den, tracks

    @property
    def fps(self): return self.fps_num / self.fps_den if self.fps_den else 0.0
    @property
    def duration(self): return self.frames * self.fps_den / self.fps_num if self.fps_num else 0.0

    def describe(self) -> str:
        return (f"{self.width:,}x{self.height:,} @ {self.fps:.3f} fps  "
                f"{self.duration:.2f}s  {self.frames:,} frames  "
                f"{self.tracks} audio")


def read_validated(f, offset: int, available: int, budget: list[int]) -> BinkInfo | None:
    """Parse and validate a Bink header at `offset`. Returns BinkInfo or None.

    budget is a single-element list so we can mutate the remaining byte budget
    across repeated calls (keeps the full-file scan bounded).
    """
    if available < 48 or budget[0] < 64:
        return None
    prefix_len = min(64, available)
    f.seek(offset)
    prefix = f.read(prefix_len)
    budget[0] -= prefix_len
    if len(prefix) < 48:
        return None

    magic = prefix[:4]
    bink2 = prefix[:3] == b"KB2"
    bink1 = prefix[:3] == b"BIK"
    if (not bink1 and not bink2) or not (ord('a') <= prefix[3] <= ord('z')):
        return None

    def r(p: int) -> int: return struct.unpack_from("<I", prefix, p)[0]
    length  = r(4) + 8
    frames  = r(8); largest = r(12); repeated = r(16)
    width   = r(20); height = r(24)
    fps_num = r(28); fps_den = r(32)
    tracks  = r(40)

    if (length < 48 or length > available
        or not (0 < frames <= MAX_FRAMES)
        or repeated != frames
        or not (0 < largest <= length)
        or not (0 < width  <= 16384)
        or not (0 < height <= 16384)
        or not (0 < fps_num <= 1_000_000)
        or not (0 < fps_den <= 1_000_000)
        or tracks > 256):
        return None

    table_bytes = (frames + 1) * 4
    for extra in (4, 0):
        table_offset = 44 + extra + tracks * 12
        table_end = table_offset + table_bytes
        if table_end > length or table_bytes > budget[0]:
            continue
        f.seek(offset + table_offset)
        table = f.read(table_bytes)
        budget[0] -= table_bytes
        if len(table) < table_bytes:
            continue
        first = struct.unpack_from("<I", table, 0)[0] & ~1
        if not (table_end <= first <= table_end + 16):
            continue
        prev = first
        max_frame = 0
        valid = True
        for i in range(1, frames + 1):
            cur = struct.unpack_from("<I", table, i * 4)[0] & ~1
            if cur <= prev or cur > length:
                valid = False
                break
            d = cur - prev
            if d > max_frame:
                max_frame = d
            prev = cur
        if not valid or prev != length or max_frame > largest + 16:
            continue
        sig = magic.decode("ascii", "replace")
        return BinkInfo(sig, length, frames, width, height, fps_num, fps_den, tracks)

    return None


def recover(path: Path) -> list[tuple[int, BinkInfo]]:
    """Scan an RPF9 for valid Bink streams. Returns [(offset, info), ...]."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        header = f.read(64)
    if len(header) < 64 or header[:4] != b"9FPR":
        raise ValueError(f"{path.name}: not an RPF9 archive (magic != '9FPR')")

    metadata_end = struct.unpack_from("<I", header, 4)[0]
    if metadata_end < 0x150 or metadata_end > READ_BUDGET or metadata_end > size:
        raise ValueError(f"{path.name}: metadataEnd out of range: 0x{metadata_end:X}")

    position = align_page(metadata_end)
    if position > size - 48:
        return []

    entries: list[tuple[int, BinkInfo]] = []
    budget = [READ_BUDGET]
    zero_pages = 0
    with open(path, "rb") as f:
        while (len(entries) < MAX_RECORDS
               and position <= size - 48
               and budget[0] >= 64):
            info = read_validated(f, position, size - position, budget)
            if info is None:
                count = min(PAGE_SIZE, size - position)
                if not entries or count > budget[0] or zero_pages >= MAX_CONSECUTIVE_ZERO_PAGES:
                    break
                f.seek(position)
                page = f.read(count)
                budget[0] -= count
                if any(b != 0 for b in page):
                    break
                zero_pages += 1
                position += PAGE_SIZE
                continue
            zero_pages = 0
            entries.append((position, info))
            position = align_page(position + info.length)
    return entries


def ext_for(sig: str) -> str:
    return ".bk2" if sig.startswith("KB2") else ".bik"


def extract(path: Path, entries: list[tuple[int, BinkInfo]], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    buf_size = 4 * 1024 * 1024
    with open(path, "rb") as src:
        for i, (offset, info) in enumerate(entries, 1):
            dst = outdir / f"clip_{i:04d}_at_{offset:016X}{ext_for(info.sig)}"
            src.seek(offset)
            remaining = info.length
            with open(dst, "wb") as g:
                while remaining > 0:
                    chunk = src.read(min(buf_size, remaining))
                    if not chunk:
                        break
                    g.write(chunk)
                    remaining -= len(chunk)
            if i % 25 == 0 or i == 1 or i == len(entries):
                print(f"\r  {MUTE}wrote{RESET} {SOFT}{i}/{len(entries)}{RESET}" + " " * 20, end="", flush=True)
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="RPF9 archive file")
    ap.add_argument("output", type=Path, nargs="?", help="Output directory")
    ap.add_argument("--list", action="store_true", help="List streams only, don't extract")
    args = ap.parse_args()

    if not args.input.is_file():
        print(f"{ERR}not a file:{RESET} {args.input}", file=sys.stderr); return 2
    if not args.list and args.output is None:
        print(f"{ERR}output directory required{RESET} (or pass --list)", file=sys.stderr); return 2

    rule = "─" * 52
    print(f"\n  {ACCENT}{BOLD}RPF9 → Bink{RESET}  {MUTE}carve video streams from an RPF9 archive{RESET}")
    print(f"  {DIM}{rule}{RESET}\n")
    print(f"  {MUTE}input {RESET} {SOFT}{args.input}{RESET}  {MUTE}·  {fmt_bytes(args.input.stat().st_size)}{RESET}")

    t0 = time.monotonic()
    try:
        entries = recover(args.input)
    except ValueError as e:
        print(f"  {ERR}{e}{RESET}"); return 2
    scan_time = time.monotonic() - t0

    total_payload = sum(info.length for _, info in entries)
    print(f"  {MUTE}found {RESET} {SOFT}{len(entries)}{RESET} {MUTE}streams  ·  {fmt_bytes(total_payload)}  ·  {scan_time:.1f}s scan{RESET}")
    print()

    if args.list:
        for i, (offset, info) in enumerate(entries, 1):
            print(f"  {MUTE}[{i:4}]{RESET} {ACC2}{info.sig}{RESET}  "
                  f"off={SOFT}0x{offset:010X}{RESET}  "
                  f"len={SOFT}{fmt_bytes(info.length)}{RESET}  "
                  f"{MUTE}{info.describe()}{RESET}")
        return 0

    print(f"  {MUTE}extracting to{RESET} {SOFT}{args.output}{RESET}")
    t1 = time.monotonic()
    extract(args.input, entries, args.output)
    print(f"  {OK}done{RESET}  {MUTE}·  {len(entries)} files  ·  {time.monotonic() - t1:.1f}s{RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        sys.exit(130)
