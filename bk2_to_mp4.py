#!/usr/bin/env python3
"""
Batch-convert .bk2 (Bink 2) files to .mp4 using RAD Video Tools silently.

Requires: RAD Video Tools installed at C:\\Program Files (x86)\\RADVideo\\
          (or pass --rad <path>).

By FluffyCyanNova and Claude (Anthropic).

Usage:
    python bk2_to_mp4.py <input_dir> [output_dir] [--workers N] [--rad PATH]

Behaviour:
    - Recursively finds *.bk2 under input_dir (non-recursive by default; add --recursive).
    - Writes <basename>.mp4 next to input, or into output_dir if given.
    - Skips files whose .mp4 already exists (resumable).
    - Uses radvideo64.exe BinkConv ... /AutoClose on an invisible desktop, so it cannot take focus.
    - Pads odd widths/heights by one black pixel (H.264 needs even sizes).
    - Prints a live progress line with ETA.
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _visible_len(s: str) -> int:
    """Length of `s` as it will appear on screen (ignores ANSI colour codes)."""
    return len(_ANSI_RE.sub("", s))


DEFAULT_RAD = r"C:\Program Files (x86)\RADVideo\radvideo64.exe"
POLL_INTERVAL   = 0.1     # seconds between polls
NO_OUTPUT_GRACE = 30.0    # seconds to wait for the output file to appear
STALL_LIMIT     = 60.0    # seconds without output growth
HARD_TIMEOUT    = 1800.0  # absolute cap on a single conversion

if os.name == "nt":
    import ctypes
    from ctypes import wintypes as W

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _ENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    class _StartupInfo(ctypes.Structure):
        _fields_ = (
            [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR), ("title", W.LPWSTR)]
            + [(n, W.DWORD) for n in "x y xsize ysize xchars ychars fill flags".split()]
            + [("show", W.WORD), ("cb2", W.WORD), ("reserved2", ctypes.c_void_p),
               ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]
        )

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]

    for _dll, _name, _ret, *_args in (
        (_u32, "CreateDesktopW", W.HANDLE, W.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.c_void_p),
        (_u32, "CloseDesktop", W.BOOL, W.HANDLE),
        (_u32, "EnumDesktopWindows", W.BOOL, W.HANDLE, _ENUMPROC, W.LPARAM),
        (_u32, "GetClassNameW", ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int),
        (_k32, "CreateJobObjectW", W.HANDLE, ctypes.c_void_p, W.LPCWSTR),
        (_k32, "SetInformationJobObject", W.BOOL, W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD),
        (_k32, "AssignProcessToJobObject", W.BOOL, W.HANDLE, W.HANDLE),
        (_k32, "CreateProcessW", W.BOOL, W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
         W.BOOL, W.DWORD, ctypes.c_void_p, W.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p),
        (_k32, "ResumeThread", W.DWORD, W.HANDLE),
        (_k32, "WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD),
        (_k32, "GetExitCodeProcess", W.BOOL, W.HANDLE, ctypes.POINTER(W.DWORD)),
        (_k32, "TerminateProcess", W.BOOL, W.HANDLE, W.UINT),
        (_k32, "CloseHandle", W.BOOL, W.HANDLE),
    ):
        getattr(_dll, _name).restype = _ret
        getattr(_dll, _name).argtypes = _args


class OffscreenProcess:
    """A process on its own desktop, so it cannot take focus, that dies with this script."""

    def __init__(self, argv: list[str]):
        self.desktop = self.job = None
        self.info = _ProcessInfo()
        self.argv = argv

    def __enter__(self):
        try:
            self._start()
        except BaseException:
            self.__exit__()
            raise
        return self

    def _start(self) -> None:
        name = "bk2mp4_" + uuid.uuid4().hex
        self.desktop = _u32.CreateDesktopW(name, None, None, 0, 0x10000000, None)  # GENERIC_ALL
        self.job = _k32.CreateJobObjectW(None, None)
        if not self.desktop or not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = (ctypes.c_ubyte * (144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112))()
        ctypes.c_uint32.from_buffer(limits, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(self.job, 9, limits, len(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        si = _StartupInfo(cb=ctypes.sizeof(_StartupInfo), desktop=name)
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(self.argv))
        if not _k32.CreateProcessW(None, cmd, None, None, False, 0x4, None, None,  # CREATE_SUSPENDED
                                   ctypes.byref(si), ctypes.byref(self.info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _k32.AssignProcessToJobObject(self.job, self.info.process):
            raise ctypes.WinError(ctypes.get_last_error())
        _k32.ResumeThread(self.info.thread)

    def poll(self) -> int | None:
        if _k32.WaitForSingleObject(self.info.process, 0) != 0:
            return None
        code = W.DWORD()
        _k32.GetExitCodeProcess(self.info.process, ctypes.byref(code))
        return code.value

    def has_message_box(self) -> bool:
        found = []

        def check(hwnd, _):
            buf = ctypes.create_unicode_buffer(64)
            _u32.GetClassNameW(hwnd, buf, len(buf))
            if buf.value in ("RADTextBox", "#32770"):
                found.append(hwnd)
            return True

        _u32.EnumDesktopWindows(self.desktop, _ENUMPROC(check), 0)
        return bool(found)

    def __exit__(self, *_):
        if self.info.process:
            _k32.TerminateProcess(self.info.process, 1)
            _k32.WaitForSingleObject(self.info.process, 5000)
        for handle in (self.info.thread, self.info.process, self.job):
            if handle:
                _k32.CloseHandle(handle)
        if self.desktop:
            _u32.CloseDesktop(self.desktop)


def even_size_args(src: Path) -> list[str]:
    with src.open("rb") as f:
        header = f.read(28)
    if len(header) < 28:
        return []
    w, h = struct.unpack_from("<II", header, 20)
    if w % 2 == 0 and h % 2 == 0:
        return []
    return [f"/Pad{w + 1 if w % 2 else 0},{h + 1 if h % 2 else 0}"]


def first_track_args(src: Path) -> list[str]:
    """/InputTrack takes a track ID, and the first track's ID isn't always 0."""
    with src.open("rb") as f:
        header = f.read(44)
        if len(header) < 44:
            return []
        tracks = struct.unpack_from("<I", header, 40)[0]
        if not 0 < tracks <= 256:
            return []
        # KB2i and later have an extra 4-byte field before the three track tables.
        base = 48 if header[:3] == b"KB2" and header[3] >= ord("i") else 44
        f.seek(base + 8 * tracks)
        raw = f.read(4)
    if len(raw) < 4:
        return []
    track_id = struct.unpack("<I", raw)[0]
    return [f"/InputTrack{track_id}"] if track_id else []


# Redirected output can use a legacy code page that lacks the symbols we print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

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
    # BinkConv picks the format from the extension, so it must stay last. The
    # random part means we only ever remove a temp file this call created.
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex[:8]}.tmp{dst.suffix}")
    error = None
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        argv = [rad, "BinkConv", str(src), str(tmp), *even_size_args(src),
                *first_track_args(src), "/AutoClose"]
        with OffscreenProcess(argv) as proc:
            started = changed = time.monotonic()
            last_size = 0
            while (code := proc.poll()) is None:
                now = time.monotonic()
                size = tmp.stat().st_size if tmp.exists() else 0
                if size != last_size:
                    last_size, changed = size, now
                if proc.has_message_box():
                    error = "BinkConv showed an error"
                elif not size and now - started > NO_OUTPUT_GRACE:
                    error = f"no output after {NO_OUTPUT_GRACE:.0f}s"
                elif size and now - changed > STALL_LIMIT:
                    error = f"output stalled for {STALL_LIMIT:.0f}s"
                elif now - started > HARD_TIMEOUT:
                    error = f"hard timeout (>{HARD_TIMEOUT:.0f}s)"
                if error:
                    return src, False, error
                time.sleep(POLL_INTERVAL)
        if code:
            return src, False, f"exit {code}"
        if not tmp.exists() or not tmp.stat().st_size:
            return src, False, "no output produced"
        tmp.rename(dst)
        return src, True, f"ok ({fmt_bytes(dst.stat().st_size)})"
    except OSError as e:
        return src, False, str(e)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


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
        stats = (
            f"  {ACCENT}{pct:5.1f}%{RESET}  "
            f"{SOFT}{done}{MUTE}/{len(pending)}{RESET}  "
            f"{MUTE}·{RESET}  {ACC2}{fmt_bytes(rate)}/s{RESET}  "
            f"{MUTE}·{RESET}  {SOFT}eta {fmt_time(eta)}{RESET}"
        )
        # Budget what's left after "  " indent + stats, cap bar at 44.
        bar_w = max(8, min(44, term_w - _visible_len(stats) - 4))
        bar = make_bar(frac, bar_w)
        line = f"  {bar}{stats}"
        # If still over-wide (tiny terminal, long ETA strings), hard-trim.
        over = _visible_len(line) - (term_w - 1)
        if over > 0:
            # Trim from the end of the visible text (keeping ANSI codes intact
            # is messy; simplest is to drop stats from the end).
            visible = _ANSI_RE.sub("", line)[: max(0, term_w - 1)]
            line = visible + RESET
        # On a real terminal: \033[2K clears the current row entirely, \r
        # returns the cursor to column 0 — redraws cleanly even if the previous
        # line was wider. When stdout is piped (no tty), skip ANSI so the pipe
        # output stays readable.
        prefix = "\033[2K\r" if USE_COLOR else "\r"
        sys.stdout.write(prefix + line)
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
