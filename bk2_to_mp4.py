#!/usr/bin/env python3
"""
Batch-convert .bk2 (Bink 2) files to .mp4 using RAD Video Tools silently.

Requires: RAD Video Tools installed at C:\\Program Files (x86)\\RADVideo\\
          (or pass --rad <path>).

Usage:
    python bk2_to_mp4.py <input_dir> [output_dir] [--workers N] [--rad PATH]

Behaviour:
    - Recursively finds *.bk2 under input_dir (non-recursive by default; add --recursive).
    - Writes <basename>.mp4 next to input, or into output_dir if given.
    - Skips files whose .mp4 already exists (resumable).
    - Uses radvideo64.exe BinkConv ... /AutoClose with hidden window.
    - Prints a live progress line with ETA.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_RAD = r"C:\Program Files (x86)\RADVideo\radvideo64.exe"
CREATE_NO_WINDOW = 0x08000000  # suppresses console window on Windows

# --- Colors ------------------------------------------------------------------
# Windows 11 terminals handle ANSI natively. Enable VT processing just in case.
if os.name == "nt":
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def _c(code: str) -> str:
    return code if USE_COLOR else ""

# Palette: one warm accent + neutral greys. Matches the Claude Code CLI vibe.
RESET  = _c("\033[0m")
BOLD   = _c("\033[1m")
DIM    = _c("\033[2m")
ITAL   = _c("\033[3m")

ACCENT = _c("\033[38;5;215m")   # warm orange
ACC2   = _c("\033[38;5;180m")   # softer tan, for secondary
MUTE   = _c("\033[38;5;244m")   # medium grey
SOFT   = _c("\033[38;5;250m")   # near-white body text
FAINT  = _c("\033[38;5;239m")   # bar track
OK     = _c("\033[38;5;108m")   # muted green (success)
WARN   = _c("\033[38;5;179m")   # mellow amber
ERR    = _c("\033[38;5;174m")   # dusty red

def banner() -> str:
    """Minimal header — one accent, thin rule, no walls."""
    title = "bk2 → mp4"
    sub   = "RAD Video Tools • silent batch converter"
    rule  = "─" * 52
    return (
        f"\n  {ACCENT}{BOLD}{title}{RESET}  {MUTE}{sub}{RESET}\n"
        f"  {FAINT}{rule}{RESET}"
    )

def make_bar(frac: float, width: int = 32) -> str:
    """Thin single-line progress bar, one accent color, muted track."""
    frac = max(0.0, min(1.0, frac))
    full = int(frac * width)
    partials = " ╸━"  # heavy horizontal track, rounded start
    # Use a pair of block-ish characters: filled vs. faint.
    fill_ch  = "━"
    track_ch = "─"
    bar = fill_ch * full
    pad = track_ch * (width - full)
    return f"{ACCENT}{bar}{RESET}{FAINT}{pad}{RESET}"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def fmt_time(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def convert_one(rad: str, src: Path, dst: Path) -> tuple[Path, bool, str]:
    """Convert a single .bk2 to .mp4. Returns (src, success, message)."""
    if dst.exists() and dst.stat().st_size > 0:
        return src, True, "skipped (exists)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    # BinkConv: input output /AutoClose — closes the "Done!" dialog on completion.
    # CREATE_NO_WINDOW keeps the RAD GUI hidden.
    tmp = dst.with_suffix(".mp4.part")
    if tmp.exists():
        tmp.unlink()
    try:
        proc = subprocess.run(
            [rad, "BinkConv", str(src), str(tmp), "/AutoClose"],
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        if tmp.exists():
            tmp.unlink()
        return src, False, "timeout (>600s)"
    if proc.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        return src, False, f"exit {proc.returncode}"
    if not tmp.exists() or tmp.stat().st_size == 0:
        return src, False, "no output produced"
    tmp.rename(dst)
    return src, True, f"ok ({fmt_bytes(dst.stat().st_size)})"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" {MUTE}({default}){RESET}" if default else ""
    try:
        v = input(f"  {ACCENT}›{RESET} {SOFT}{prompt}{RESET}{suffix} ").strip()
    except EOFError:
        v = ""
    return v or default


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    d = f"{ACCENT}Y{RESET}{MUTE}/n{RESET}" if default else f"{MUTE}y/{RESET}{ACCENT}N{RESET}"
    v = ask(f"{prompt} ({d})").lower()
    if not v:
        return default
    return v.startswith("y")


def interactive_menu() -> argparse.Namespace:
    """Prompt the user for options when no CLI args were given."""
    print(banner())
    print()

    while True:
        inp = ask("Input directory", "")
        if not inp:
            print(f"    {ERR}required{RESET}"); continue
        inp_path = Path(inp.strip('"'))
        if inp_path.is_dir():
            break
        print(f"    {ERR}not a directory:{RESET} {MUTE}{inp_path}{RESET}")

    default_out = str(inp_path.parent / f"{inp_path.name}_mp4")
    out = ask("Output directory", default_out)
    out_path = Path(out.strip('"'))

    recursive = ask_yes_no("Recurse into subdirectories?", default=False)
    workers_raw = ask("Parallel workers", "1")
    try:
        workers = max(1, int(workers_raw))
    except ValueError:
        workers = 1

    rad = ask("radvideo64.exe", DEFAULT_RAD)

    print()
    print(f"  {MUTE}next step{RESET}")
    print(f"    {ACCENT}1{RESET}  {SOFT}preview{RESET}   {MUTE}list files, don't convert{RESET}")
    print(f"    {ACCENT}2{RESET}  {SOFT}convert{RESET}   {MUTE}start the batch{RESET}")
    print(f"    {ACCENT}3{RESET}  {SOFT}cancel{RESET}")
    choice = ask("", "1")
    if choice == "3":
        print(f"  {MUTE}cancelled.{RESET}")
        sys.exit(0)
    dry_run = (choice == "1")

    return argparse.Namespace(
        input_dir=inp_path, output_dir=out_path, rad=rad,
        recursive=recursive, workers=workers, dry_run=dry_run,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_dir", type=Path, nargs="?", help="Directory containing .bk2 files (interactive if omitted)")
    ap.add_argument("output_dir", type=Path, nargs="?", default=None,
                    help="Output directory (default: same as input)")
    ap.add_argument("--rad", default=DEFAULT_RAD, help=f"Path to radvideo64.exe (default: {DEFAULT_RAD})")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subdirectories")
    ap.add_argument("--workers", type=int, default=1,
                    help="Parallel workers (default 1; RAD GUI may dislike >1, try 2-4 at your own risk)")
    ap.add_argument("--dry-run", action="store_true", help="List work without converting")
    args = ap.parse_args()

    if args.input_dir is None:
        args = interactive_menu()

    if not args.input_dir.is_dir():
        print(f"ERROR: not a directory: {args.input_dir}", file=sys.stderr)
        return 2
    if not Path(args.rad).exists():
        print(f"ERROR: RAD tool not found: {args.rad}", file=sys.stderr)
        print("Install RAD Video Tools from https://www.radgametools.com/down/Bink/RADTools.exe", file=sys.stderr)
        return 2

    pattern = "**/*.bk2" if args.recursive else "*.bk2"
    srcs = sorted(args.input_dir.glob(pattern))
    if not srcs:
        print(f"No .bk2 files found in {args.input_dir}")
        return 0

    outdir = args.output_dir or args.input_dir
    jobs = []
    for s in srcs:
        rel = s.relative_to(args.input_dir) if args.recursive else Path(s.name)
        d = outdir / rel.with_suffix(".mp4")
        jobs.append((s, d))

    total_in = sum(s.stat().st_size for s, _ in jobs)
    pending = [(s, d) for s, d in jobs if not (d.exists() and d.stat().st_size > 0)]
    skipped = len(jobs) - len(pending)

    print()
    print(f"  {MUTE}input   {RESET} {SOFT}{args.input_dir}{RESET}")
    print(f"  {MUTE}output  {RESET} {SOFT}{outdir}{RESET}")
    print(f"  {MUTE}found   {RESET} {SOFT}{len(jobs)} files{RESET}  {MUTE}·{RESET}  {ACC2}{fmt_bytes(total_in)}{RESET}")
    print(f"  {MUTE}pending {RESET} {SOFT}{len(pending)}{RESET}  {MUTE}·  {skipped} already converted{RESET}")
    print()
    if args.dry_run:
        print(f"  {MUTE}preview{RESET}")
        for s, d in pending[:20]:
            print(f"    {MUTE}·{RESET} {SOFT}{s.name}{RESET}")
        if len(pending) > 20:
            print(f"    {MUTE}  … and {len(pending)-20} more{RESET}")
        print()
        if sys.stdin.isatty() and ask_yes_no("Convert these files now?", default=False):
            args.dry_run = False
        else:
            return 0

    start = time.monotonic()
    done = 0
    done_bytes = 0
    failed: list[tuple[Path, str]] = []
    total_bytes = sum(s.stat().st_size for s, _ in pending)

    def report(force: bool = False) -> None:
        elapsed = time.monotonic() - start
        rate = done_bytes / elapsed if elapsed > 0 else 0
        eta = (total_bytes - done_bytes) / rate if rate > 0 else 0
        frac = done_bytes / total_bytes if total_bytes else 1.0
        pct = 100 * frac
        term_w = shutil.get_terminal_size((100, 20)).columns
        bar_w = max(20, min(44, term_w - 60))
        bar = make_bar(frac, bar_w)
        line = (
            f"\r  {bar}  {ACCENT}{pct:5.1f}%{RESET}  "
            f"{SOFT}{done}{MUTE}/{len(pending)}{RESET}  "
            f"{MUTE}·{RESET}  {ACC2}{fmt_bytes(rate)}/s{RESET}  "
            f"{MUTE}·{RESET}  {SOFT}eta {fmt_time(eta)}{RESET}"
        )
        sys.stdout.write(line + " " * 6)
        sys.stdout.flush()

    print(f"  {MUTE}converting{RESET}")
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(convert_one, args.rad, s, d): (s, d) for s, d in pending}
            for f in as_completed(futures):
                s, _ = futures[f]
                _, ok, msg = f.result()
                done += 1
                done_bytes += s.stat().st_size
                if not ok:
                    failed.append((s, msg))
                    print(f"\r  {ERR}✗{RESET} {s.name}  {MUTE}{msg}{RESET}" + " " * 30)
                report()
    else:
        for s, d in pending:
            _, ok, msg = convert_one(args.rad, s, d)
            done += 1
            done_bytes += s.stat().st_size
            if not ok:
                failed.append((s, msg))
                print(f"\r  {ERR}✗{RESET} {s.name}  {MUTE}{msg}{RESET}" + " " * 30)
            report()

    report(force=True)
    print()
    elapsed = time.monotonic() - start
    ok_count = done - len(failed)
    print()
    if failed:
        print(f"  {WARN}done{RESET}  {MUTE}·  {SOFT}{ok_count}{RESET}{MUTE} ok  ·  {ERR}{len(failed)}{RESET}{MUTE} failed  ·  {fmt_time(elapsed)}{RESET}")
        print()
        for s, m in failed[:20]:
            print(f"    {ERR}✗{RESET} {SOFT}{s.name}{RESET}  {MUTE}{m}{RESET}")
        return 1
    print(f"  {OK}done{RESET}  {MUTE}·  {SOFT}{ok_count} files{RESET}{MUTE}  ·  {fmt_time(elapsed)}{RESET}")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        rc = 130
    # Keep the window open when double-clicked
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
    sys.exit(rc)
