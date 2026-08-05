#!/usr/bin/env python3
"""
mod_convert_core — Conversion engine for Panasonic/JVC camcorder .MOD/.TOD files.

A .MOD file (as produced by the Panasonic SDR-S50 and many JVC Everio / Canon
camcorders) is simply an MPEG-2 Program Stream with an unusual extension. The
video is interlaced standard-definition (720x576 PAL / 720x480 NTSC) with a
non-square pixel aspect. This module wraps ffprobe/ffmpeg to:

  * detect the field order and container sample-aspect-ratio,
  * deinterlace with bwdif (50p field-doubling or 25p weave),
  * rescale to square pixels so the picture looks correct on modern displays,
  * carry the recording timestamp from the sidecar .MOI into the MP4 metadata,
  * or, in archive mode, losslessly remux the untouched streams into a .mkv.

The engine is intentionally free of any GUI dependency so it can be unit-tested
and reused headlessly. The Tkinter front-end lives in mod_convert_gui.py.
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SOURCE_EXTENSIONS = (".mod", ".tod")

MODE_MP4 = "mp4"          # H.264/AAC, bwdif field-doubling -> 50p, square pixels
MODE_MP4_25P = "mp4-25p"  # H.264/AAC, bwdif weave         -> 25p, square pixels
MODE_ARCHIVE = "archive"  # lossless remux, streams untouched -> .mkv
MODES = (MODE_MP4, MODE_MP4_25P, MODE_ARCHIVE)

# Aspect override tokens accepted from the UI / CLI.  "auto" means: derive the
# display aspect from the container sample-aspect-ratio (works for NTSC too).
ASPECT_AUTO = "auto"


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


@dataclass
class VideoInfo:
    """The subset of ffprobe output that drives the conversion."""

    width: int = 0
    height: int = 0
    sar: Fraction = field(default_factory=lambda: Fraction(1, 1))
    field_order: str = "progressive"   # tff | bff | progressive
    duration: float = 0.0              # seconds, may be 0 if unknown

    @property
    def is_interlaced(self) -> bool:
        return self.field_order in ("tff", "bff")

    @property
    def parity(self) -> str:
        """bwdif parity argument matching the detected field order."""
        return "bff" if self.field_order == "bff" else "tff"


def which_ffmpeg(name: str = "ffmpeg") -> Optional[str]:
    """Return the resolved path to ffmpeg/ffprobe or None if not installed."""
    return shutil.which(name)


def probe_video(path: str, ffprobe: str = "ffprobe") -> VideoInfo:
    """Run ffprobe and return a VideoInfo for the first video stream.

    Raises FileNotFoundError if ffprobe is not installed and RuntimeError if the
    file has no readable video stream.
    """
    if which_ffmpeg(ffprobe) is None:
        raise FileNotFoundError(f"{ffprobe} not found on PATH")

    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,sample_aspect_ratio,field_order:format=duration",
        "-of", "default=noprint_wrappers=1",
        path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    return _parse_probe(out.stdout)


def _parse_probe(text: str) -> VideoInfo:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()

    info = VideoInfo()
    info.width = int(values.get("width") or 0)
    info.height = int(values.get("height") or 0)
    info.sar = _parse_ratio(values.get("sample_aspect_ratio"))
    fo = (values.get("field_order") or "progressive").lower()
    info.field_order = fo if fo in ("tff", "bff", "progressive") else "progressive"
    try:
        info.duration = float(values.get("duration") or 0.0)
    except ValueError:
        info.duration = 0.0
    return info


def _parse_ratio(text: Optional[str]) -> Fraction:
    """Parse an "a:b" or "a/b" ratio; fall back to 1:1 for '0:1', 'N/A', etc."""
    if not text:
        return Fraction(1, 1)
    text = text.strip().replace(":", "/")
    if text in ("", "0/1", "0/0", "N/A"):
        return Fraction(1, 1)
    try:
        frac = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return Fraction(1, 1)
    return frac if frac > 0 else Fraction(1, 1)


# --------------------------------------------------------------------------- #
# .MOI sidecar parsing
# --------------------------------------------------------------------------- #


def moi_sidecar_path(mod_path: str) -> Optional[str]:
    """Return the .MOI whose basename matches the .MOD, if it exists.

    MOV00F.MOD pairs with MOV00F.MOI — *not* with a differently-numbered
    sidecar that merely sits in the same folder.
    """
    base, _ = os.path.splitext(mod_path)
    for ext in (".MOI", ".moi", ".Moi"):
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate
    return None


def read_moi_datetime(moi_path: str) -> Optional[_dt.datetime]:
    """Extract the recording timestamp from a JVC/Panasonic .MOI header.

    The header stores the wall-clock recording time as:
        offset 0x06  year   (uint16, big-endian)
        offset 0x08  month  (uint8)
        offset 0x09  day    (uint8)
        offset 0x0A  hour   (uint8)
        offset 0x0B  minute (uint8)
        offset 0x0C  second (uint8)

    Returns a naive datetime (camera local time) or None if the bytes do not
    form a plausible date — different firmware revisions vary, so we validate
    rather than trust blindly.
    """
    try:
        with open(moi_path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if len(head) < 13:
        return None

    year = (head[6] << 8) | head[7]
    month, day, hour, minute, second = head[8], head[9], head[10], head[11], head[12]
    try:
        stamp = _dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    if not (2000 <= year <= 2099):
        return None
    return stamp


def read_moi_aspect_flag(moi_path: str) -> Optional[int]:
    """Return the raw aspect/format flag byte (e.g. 0x54 for PAL 16:9), if any.

    Informational only — the conversion never depends on it, because the
    container SAR is the authoritative source and can be overridden explicitly.
    """
    try:
        with open(moi_path, "rb") as fh:
            data = fh.read(0x100)
    except OSError:
        return None
    # The format flag lives just past the timestamp block on the units this was
    # validated against.  Kept narrow and best-effort.
    return data[0x80] if len(data) > 0x80 else None


# --------------------------------------------------------------------------- #
# Aspect / scaling maths
# --------------------------------------------------------------------------- #


def _even(n: float) -> int:
    """Round to the nearest positive even integer (H.264 needs even dimensions)."""
    val = int(round(n / 2.0)) * 2
    return max(val, 2)


def square_pixel_size(info: VideoInfo, aspect_override: str = ASPECT_AUTO) -> tuple[int, int]:
    """Compute the square-pixel output size (width, height).

    With aspect_override == "auto" the display aspect comes from the container
    SAR: output_w = width * sar, height unchanged.  With an explicit "a:b" the
    output is sized so output_w/output_h == a/b directly, which repairs files
    whose SAR flag is wrong.
    """
    height = info.height or 576
    if aspect_override and aspect_override != ASPECT_AUTO:
        dar = _parse_ratio(aspect_override)
        width = _even(height * dar)
    else:
        width = _even((info.width or 720) * info.sar)
    return width, height


# --------------------------------------------------------------------------- #
# Job description + command building
# --------------------------------------------------------------------------- #


@dataclass
class ConvertOptions:
    mode: str = MODE_MP4
    crf: int = 20
    aspect: str = ASPECT_AUTO          # "auto" | "16:9" | "4:3" | "a:b"
    audio_bitrate: str = "192k"
    output_dir: Optional[str] = None   # None -> alongside the source file
    overwrite: bool = False


def output_path_for(src: str, opts: ConvertOptions) -> str:
    """Return the destination path for a given source and options."""
    ext = ".mkv" if opts.mode == MODE_ARCHIVE else ".mp4"
    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = opts.output_dir or os.path.dirname(os.path.abspath(src))
    return os.path.join(out_dir, base + ext)


def build_video_filter(info: VideoInfo, opts: ConvertOptions) -> Optional[str]:
    """Build the -vf chain, or None when no filtering is needed (archive)."""
    if opts.mode == MODE_ARCHIVE:
        return None

    parts: list[str] = []
    if info.is_interlaced:
        bwdif_mode = "send_field" if opts.mode == MODE_MP4 else "send_frame"
        parts.append(f"bwdif=mode={bwdif_mode}:parity={info.parity}")
    width, height = square_pixel_size(info, opts.aspect)
    parts.append(f"scale={width}:{height}:flags=lanczos")
    parts.append("setsar=1")
    return ",".join(parts)


def build_command(
    src: str,
    info: VideoInfo,
    opts: ConvertOptions,
    creation_time: Optional[_dt.datetime] = None,
    ffmpeg: str = "ffmpeg",
    progress_arg: bool = False,
) -> list[str]:
    """Assemble the full ffmpeg argv for one file.

    When progress_arg is True, "-progress pipe:1 -nostats" is appended so the
    caller can parse machine-readable progress from stdout.
    """
    dst = output_path_for(src, opts)
    cmd = [ffmpeg, "-hide_banner", "-fflags", "+genpts"]
    cmd += ["-y" if opts.overwrite else "-n", "-i", src]

    if opts.mode == MODE_ARCHIVE:
        cmd += ["-map", "0", "-c", "copy"]
    else:
        vf = build_video_filter(info, opts)
        cmd += ["-vf", vf]
        cmd += ["-c:v", "libx264", "-crf", str(opts.crf), "-preset", "medium",
                "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-b:a", opts.audio_bitrate]
        cmd += ["-movflags", "+faststart"]

    if creation_time is not None:
        # ffmpeg expects an ISO-8601 UTC-ish string; the camera clock is local,
        # but round-tripping it verbatim keeps the displayed time consistent.
        stamp = creation_time.strftime("%Y-%m-%dT%H:%M:%S")
        cmd += ["-metadata", f"creation_time={stamp}"]

    if progress_arg:
        cmd += ["-progress", "pipe:1", "-nostats"]

    cmd += [dst]
    return cmd


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #


def discover_sources(paths: Iterable[str], recursive: bool = True) -> list[str]:
    """Expand a mix of files and folders into a sorted list of .MOD/.TOD files."""
    found: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            if recursive:
                for root, _dirs, files in os.walk(path):
                    for name in files:
                        if name.lower().endswith(SOURCE_EXTENSIONS):
                            found.append(os.path.join(root, name))
            else:
                for name in os.listdir(path):
                    full = os.path.join(path, name)
                    if os.path.isfile(full) and name.lower().endswith(SOURCE_EXTENSIONS):
                        found.append(full)
        elif os.path.isfile(path) and path.lower().endswith(SOURCE_EXTENSIONS):
            found.append(path)
    # De-duplicate while preserving a stable, sorted order.
    return sorted(dict.fromkeys(os.path.abspath(p) for p in found))


# --------------------------------------------------------------------------- #
# Running a conversion with progress
# --------------------------------------------------------------------------- #

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


@dataclass
class ConvertResult:
    src: str
    dst: str
    ok: bool
    message: str = ""


ProgressCallback = Callable[[float], None]   # fraction 0.0 .. 1.0
LogCallback = Callable[[str], None]


def convert_file(
    src: str,
    opts: ConvertOptions,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> ConvertResult:
    """Convert one file, streaming progress via callbacks.

    Returns a ConvertResult; never raises for the ordinary "ffmpeg failed"
    case — the message carries the reason so batch runs can continue.
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    dst = output_path_for(src, opts)
    if os.path.exists(dst) and not opts.overwrite:
        return ConvertResult(src, dst, False, f"skipped, exists: {os.path.basename(dst)}")

    try:
        info = probe_video(src, ffprobe)
    except (FileNotFoundError, RuntimeError) as exc:
        return ConvertResult(src, dst, False, f"probe failed: {exc}")

    creation_time = None
    moi = moi_sidecar_path(src)
    if moi:
        creation_time = read_moi_datetime(moi)
        if creation_time:
            log(f"  recording time from {os.path.basename(moi)}: {creation_time}")

    out_dir = os.path.dirname(dst)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    cmd = build_command(src, info, opts, creation_time, ffmpeg, progress_arg=True)
    log("  " + " ".join(_shell_quote(a) for a in cmd))

    duration = info.duration or 0.0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        return ConvertResult(src, dst, False, f"{ffmpeg} not found on PATH")

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if should_cancel and should_cancel():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            _cleanup_partial(dst)
            return ConvertResult(src, dst, False, "cancelled")
        if on_progress and duration > 0:
            secs = _progress_seconds(line)
            if secs is not None:
                on_progress(min(secs / duration, 1.0))

    proc.wait()
    if proc.returncode != 0:
        _cleanup_partial(dst)
        return ConvertResult(src, dst, False, f"ffmpeg exited {proc.returncode}")

    _copy_file_times(src, dst)
    if on_progress:
        on_progress(1.0)
    return ConvertResult(src, dst, True, os.path.basename(dst))


def _progress_seconds(line: str) -> Optional[float]:
    """Parse a second value from either -progress (out_time_ms=...) or stats."""
    if line.startswith("out_time_ms="):
        try:
            return int(line.split("=", 1)[1]) / 1_000_000.0
        except ValueError:
            return None
    if line.startswith("out_time="):
        m = _TIME_RE.search(line)
    else:
        m = _TIME_RE.search(line)
    if m:
        h, mnt, s = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + float(s)
    return None


def _cleanup_partial(dst: str) -> None:
    try:
        if os.path.exists(dst):
            os.remove(dst)
    except OSError:
        pass


def _copy_file_times(src: str, dst: str) -> None:
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def _shell_quote(arg: str) -> str:
    if arg and re.fullmatch(r"[A-Za-z0-9_./:+=-]+", arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# Batch helper
# --------------------------------------------------------------------------- #


def estimate_output_mb(source_mb: float, opts: ConvertOptions) -> float:
    """Very rough size hint used by the GUI before conversion.

    Calibrated against the sample clip in the analysis (6.3 MB source):
    CRF 20 -> ~1.1x, CRF 22 -> ~0.73x, archive -> ~1.0x.
    """
    if opts.mode == MODE_ARCHIVE:
        return source_mb
    # Linear-ish around the measured points; clamped to something sane.
    factor = 1.1 * math.pow(0.5, (opts.crf - 20) / 6.0)
    if opts.mode == MODE_MP4_25P:
        factor *= 0.72  # half the frames
    return max(source_mb * factor, 0.1)
