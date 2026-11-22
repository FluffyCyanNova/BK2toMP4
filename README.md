# BK2toMP4

Convert Bink 2 (`.bk2`) video files to `.mp4` in batch on Windows, with a small
companion script to carve Bink streams out of `RPF9` archives when the
directory table is encrypted.

Two scripts, no build step, pure Python standard library. Tested on Windows 11.

```
bk2_to_mp4.py    Batch convert *.bk2 to *.mp4 via RAD Video Tools (silent).
extract_bink.py  Carve Bink streams out of an RPF9 archive (optional).
```

---

## Legal

These scripts process files you already have. **Bring your own input.** Do not
use this repo to distribute, trade, or redistribute copyrighted content. No
third-party data is included here and none should ever be committed to a fork
of this repo. The `.gitignore` is set up to reject common media and archive
extensions so this can't happen by accident.

The Bink codec is proprietary and owned by [RAD Game Tools](https://www.radgametools.com/)
(part of Epic Games). `bk2_to_mp4.py` drives their free RAD Video Tools
installer at `C:\Program Files (x86)\RADVideo\radvideo64.exe`. This repo does
not ship or redistribute any part of Bink.

---

## Requirements

| Script | OS | Dependencies |
|---|---|---|
| `extract_bink.py` | Any | Python 3.10+ (stdlib only — no `pip install`) |
| `bk2_to_mp4.py`   | Windows 10 / 11 | Python 3.10+ **and** [RAD Video Tools](https://www.radgametools.com/down/Bink/RADTools.exe) |

RAD Video Tools is a free ~1.3 MB installer signed by Epic Games Inc. Install
with defaults — the scripts expect it at `C:\Program Files (x86)\RADVideo\radvideo64.exe`.
If you install it somewhere else, pass `--rad "C:\path\to\radvideo64.exe"`
when running `bk2_to_mp4.py`.

---

## Install

```powershell
git clone <this-repo-url>
cd BK2toMP4

# 1. Install Python 3.10+ (python.org or the Microsoft Store)
# 2. Install RAD Video Tools (only if you want the MP4 conversion step)
#    https://www.radgametools.com/down/Bink/RADTools.exe
# 3. Run
python bk2_to_mp4.py
```

Nothing else to install — no `pip install`, no build step, no virtualenv.

---

## Usage

### bk2_to_mp4.py — batch convert .bk2 to .mp4

Double-click the file, or run it with no arguments for an interactive menu:

```powershell
python bk2_to_mp4.py
```

Or drive it from the command line:

```powershell
python bk2_to_mp4.py <input_dir> [output_dir] [--recursive] [--workers N] [--dry-run]
```

| flag | meaning |
|---|---|
| `<input_dir>` | folder containing `*.bk2` files |
| `[output_dir]` | where to write `*.mp4` (default: `<input_dir>_mp4` sibling) |
| `--recursive` | descend into subdirectories |
| `--workers N` | parallel converter processes (default 1; RAD's GUI may not love >1) |
| `--dry-run` | list files, don't convert |
| `--rad PATH` | override the path to `radvideo64.exe` |

The script:

- skips files whose `.mp4` already exists (resumable);
- writes to a `.mp4.part` temp file and renames on success (no half-files);
- uses `BinkConv ... /AutoClose` with a hidden window so you never see a RAD
  dialog or have to click anything.

### extract_bink.py — carve Bink streams from an RPF9

For `RPF9` archives whose directory tables are AES-CBC encrypted and whose
key is not public. Bink streams inside such archives are stored as plain
data, so a scan + header validation finds them reliably.

```powershell
python extract_bink.py <input.rpf> [output_dir] [--list]
```

Produces files named `clip_<index>_at_<offset_hex>.bk2` (and `.bik` for Bink 1).

Only strictly valid Bink files are carved: the header is parsed, dimensions
and frame rate are range-checked, and the frame index table must be
monotonically increasing and sum to exactly the file length. False positives
from random magic-byte collisions are rejected.

Then feed that output directory into `bk2_to_mp4.py`.

---

## Example workflow

```powershell
# 1. Extract all Bink streams from your own RPF9 archive
python extract_bink.py "D:\path\to\your_archive.rpf" "D:\out\clips_bk2"

# 2. Convert them to MP4 (H.264 + AAC via RAD's BinkConv)
python bk2_to_mp4.py "D:\out\clips_bk2" "D:\out\clips_mp4"
```

A 2560×1440, 13-second Bink 2 clip converts in a few seconds and comes out
around 55 MB as H.264 + AAC. Silent clips with no audio tracks work the same
way; `BinkConv` just writes a video-only MP4.

---

## Credits

- The Bink-recovery algorithm in `extract_bink.py` is a Python port of the
  clean-room C# implementation in
  [breadbbbb/rpf9-explorer](https://github.com/breadbbbb/rpf9-explorer)
  (`Core/Rpf9MediaRecovery.cs`). Credit for the heuristics belongs there.
- The MP4 conversion is done by [RAD Video Tools](https://www.radgametools.com/)
  via its `BinkConv` command. This repo does not ship, link, or distribute any
  RAD code.

---

## License

MIT — see [LICENSE](LICENSE). This applies to the scripts in this repo only;
Bink Video and RAD Video Tools have their own licenses, and third-party
content is not covered.
