#!/usr/bin/env python3
# SDR-S50 Converter — convert Panasonic/JVC camcorder .MOD/.TOD files
# Copyright (C) 2026  larszu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
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
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SOURCE_EXTENSIONS = (".mod", ".tod")

MODE_MP4 = "mp4"          # H.264/AAC, bwdif field-doubling -> 50p, square pixels
MODE_MP4_25P = "mp4-25p"  # H.264/AAC, bwdif weave         -> 25p, square pixels
MODE_ARCHIVE = "archive"  # lossless remux, streams untouched
MODES = (MODE_MP4, MODE_MP4_25P, MODE_ARCHIVE)

# Container choices for archive mode.
CONTAINER_MKV = "mkv"
CONTAINER_MP4 = "mp4"
ARCHIVE_CONTAINERS = (CONTAINER_MKV, CONTAINER_MP4)

# Field-order handling for deinterlacing.
FIELD_AUTO = "auto"
FIELD_TFF = "tff"
FIELD_BFF = "bff"
FIELD_ORDERS = (FIELD_AUTO, FIELD_TFF, FIELD_BFF)

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

    # Descriptive extras — used for the info panel and for carrying the
    # source's colour characteristics into the encode.
    codec: str = ""
    frame_rate: str = ""               # e.g. "25/1"
    color_space: str = ""
    color_primaries: str = ""
    color_trc: str = ""
    color_range: str = ""
    container: str = ""
    audio_codec: str = ""
    audio_rate: str = ""
    audio_channels: str = ""
    audio_bitrate_src: str = ""

    @property
    def is_interlaced(self) -> bool:
        return self.field_order in ("tff", "bff")

    @property
    def parity(self) -> str:
        """bwdif parity argument matching the detected field order."""
        return "bff" if self.field_order == "bff" else "tff"

    @property
    def fps(self) -> float:
        try:
            return float(Fraction(self.frame_rate))
        except (ValueError, ZeroDivisionError):
            return 0.0

    @property
    def dar(self) -> str:
        """Display aspect ratio as a reduced 'a:b' string."""
        if not self.width or not self.height:
            return "?"
        ratio = Fraction(self.width, self.height) * self.sar
        return f"{ratio.numerator}:{ratio.denominator}"


# Environment escape hatch: point at a folder holding ffmpeg/ffprobe to override
# both the bundled copies and whatever is on PATH.
FFMPEG_DIR_ENV = "SDRS50_FFMPEG_DIR"


def _bundle_dirs() -> list[str]:
    """Folders that may hold ffmpeg/ffprobe shipped alongside this app.

    Covers the layouts PyInstaller produces: the onefile extraction directory
    (sys._MEIPASS), the directory of the frozen executable, and the sibling
    folders inside a macOS .app bundle (Contents/{Frameworks,Resources,MacOS}),
    since PyInstaller's placement of added binaries differs between versions
    and between onefile/onedir builds.
    """
    dirs: list[str] = []

    override = os.environ.get(FFMPEG_DIR_ENV)
    if override:
        dirs.append(override)

    def add_with_siblings(base: str) -> None:
        dirs.append(base)
        parent = os.path.dirname(base)
        for sibling in ("Frameworks", "Resources", "MacOS"):
            dirs.append(os.path.join(parent, sibling))

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        add_with_siblings(meipass)
    if getattr(sys, "frozen", False):
        add_with_siblings(os.path.dirname(os.path.abspath(sys.executable)))

    # Running from source: allow a vendor/ folder next to the modules.
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.join(here, "vendor"))
    dirs.append(here)
    return dirs


def which_ffmpeg(name: str = "ffmpeg") -> Optional[str]:
    """Resolve ffmpeg/ffprobe, preferring a bundled copy over PATH.

    Returns an absolute path, or None if the tool cannot be found anywhere.
    """
    # An explicit path was handed in — use it as given.
    if os.path.isabs(name) or os.sep in name:
        return name if os.path.isfile(name) else shutil.which(name)

    exe = name
    if sys.platform == "win32" and not exe.lower().endswith(".exe"):
        exe += ".exe"

    for directory in _bundle_dirs():
        candidate = os.path.normpath(os.path.join(directory, exe))
        if not os.path.isfile(candidate):
            continue
        if not os.access(candidate, os.X_OK):
            # Bundled binaries occasionally lose the exec bit in transit;
            # restore it rather than silently falling through to PATH.
            try:
                os.chmod(candidate, os.stat(candidate).st_mode | 0o111)
            except OSError:
                continue
            if not os.access(candidate, os.X_OK):
                continue
        return candidate

    return shutil.which(name)


def probe_video(path: str, ffprobe: str = "ffprobe") -> VideoInfo:
    """Run ffprobe and return a VideoInfo for the first video stream.

    Raises FileNotFoundError if ffprobe is not installed and RuntimeError if the
    file has no readable video stream.
    """
    resolved = which_ffmpeg(ffprobe)
    if resolved is None:
        raise FileNotFoundError(f"{ffprobe} not found (bundled or on PATH)")

    cmd = [
        resolved, "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,sample_aspect_ratio,"
        "field_order,r_frame_rate,color_space,color_primaries,color_transfer,"
        "color_range,sample_rate,channels,bit_rate"
        ":format=duration,format_name",
        "-of", "default=noprint_wrappers=1",
        path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    return _parse_probe(out.stdout)


# ffprobe spells top/bottom-field-first several ways depending on where the
# flag came from; normalise them all.
_FIELD_ORDER_MAP = {
    "tt": "tff", "tff": "tff", "tb": "tff",
    "bb": "bff", "bff": "bff", "bt": "bff",
    "progressive": "progressive",
}


def _parse_probe(text: str) -> VideoInfo:
    """Parse ffprobe's flat key=value output into a VideoInfo.

    The output contains one block per stream plus the format block, so keys
    repeat. We keep the first video stream and the first audio stream.
    """
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # A repeated key means a new stream block started.
        if key in current:
            blocks.append(current)
            current = {}
        current[key] = val
    if current:
        blocks.append(current)

    def first(pred) -> dict[str, str]:
        for b in blocks:
            if pred(b):
                return b
        return {}

    video = first(lambda b: b.get("codec_type") == "video")
    if not video:
        # Output from a "-select_streams v" style probe omits codec_type;
        # fall back to whichever block carries picture dimensions.
        video = first(lambda b: "width" in b or "field_order" in b)
    audio = first(lambda b: b.get("codec_type") == "audio")
    fmt = first(lambda b: "format_name" in b or "duration" in b and "codec_type" not in b)

    info = VideoInfo()
    info.width = _int(video.get("width"))
    info.height = _int(video.get("height"))
    info.sar = _parse_ratio(video.get("sample_aspect_ratio"))
    fo = (video.get("field_order") or "progressive").lower()
    info.field_order = _FIELD_ORDER_MAP.get(fo, "progressive")
    info.codec = _clean(video.get("codec_name"))
    info.frame_rate = _clean(video.get("r_frame_rate"))
    info.color_space = _clean(video.get("color_space"))
    info.color_primaries = _clean(video.get("color_primaries"))
    info.color_trc = _clean(video.get("color_transfer"))
    info.color_range = _clean(video.get("color_range"))

    info.audio_codec = _clean(audio.get("codec_name"))
    info.audio_rate = _clean(audio.get("sample_rate"))
    info.audio_channels = _clean(audio.get("channels"))
    info.audio_bitrate_src = _clean(audio.get("bit_rate"))

    info.container = _clean(fmt.get("format_name"))
    try:
        info.duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    except ValueError:
        info.duration = 0.0
    return info


def _int(text: Optional[str]) -> int:
    try:
        return int(text or 0)
    except ValueError:
        return 0


def _clean(text: Optional[str]) -> str:
    """Normalise ffprobe's 'N/A'/'unknown' placeholders to an empty string."""
    value = (text or "").strip()
    return "" if value.lower() in ("", "n/a", "unknown", "unspecified") else value


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
        offset 0x06  year         (uint16, big-endian)
        offset 0x08  month        (uint8)
        offset 0x09  day          (uint8)
        offset 0x0A  hour         (uint8)
        offset 0x0B  minute       (uint8)
        offset 0x0C  milliseconds (uint16, big-endian)

    Note the last field: seconds are *not* a plain byte. They are carried as
    milliseconds-within-the-minute, so 0xBF68 = 49000 ms = second 49. Reading
    that byte as seconds yields values above 59 and silently loses the date.

    Returns a naive datetime (camera local time) or None if the bytes do not
    form a plausible date — firmware revisions vary, so we validate rather
    than trust blindly.
    """
    try:
        with open(moi_path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if len(head) < 14:
        return None

    year = (head[6] << 8) | head[7]
    month, day, hour, minute = head[8], head[9], head[10], head[11]
    millis = (head[12] << 8) | head[13]
    if millis >= 60_000:
        return None
    second, micro = divmod(millis, 1000)
    try:
        stamp = _dt.datetime(year, month, day, hour, minute, second, micro * 1000)
    except ValueError:
        return None
    if not (2000 <= year <= 2099):
        return None
    return stamp.replace(microsecond=0)


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
    # Container for archive mode. MKV keeps MPEG-2 + MP2 in their native
    # form; MP4 also remuxes losslessly but stores MP2 audio in a way some
    # players (notably QuickTime) will not decode.
    archive_container: str = CONTAINER_MKV
    # Field order for deinterlacing: "auto" trusts the stream flag, "tff"/"bff"
    # force it. Getting this wrong is the classic source of comb/judder
    # artefacts, so it is user-overridable.
    field_order: str = FIELD_AUTO
    # Carry the source's colour characteristics (PAL material is BT.470BG)
    # into the output instead of letting players assume BT.709.
    preserve_color: bool = True


def output_path_for(src: str, opts: ConvertOptions) -> str:
    """Return the destination path for a given source and options."""
    if opts.mode == MODE_ARCHIVE:
        ext = ".mp4" if opts.archive_container == CONTAINER_MP4 else ".mkv"
    else:
        ext = ".mp4"
    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = opts.output_dir or os.path.dirname(os.path.abspath(src))
    return os.path.join(out_dir, base + ext)


def effective_parity(info: VideoInfo, opts: ConvertOptions) -> str:
    """Resolve the bwdif parity actually used: explicit override, else probe."""
    if opts.field_order in (FIELD_TFF, FIELD_BFF):
        return opts.field_order
    return info.parity


def should_deinterlace(info: VideoInfo, opts: ConvertOptions) -> bool:
    """Whether to run bwdif.

    Interlaced sources are deinterlaced. A forced field order also implies
    deinterlacing, so users can rescue files whose flag claims progressive
    but whose content is interlaced.
    """
    if opts.mode == MODE_ARCHIVE:
        return False
    return info.is_interlaced or opts.field_order in (FIELD_TFF, FIELD_BFF)


def build_video_filter(info: VideoInfo, opts: ConvertOptions) -> Optional[str]:
    """Build the -vf chain, or None when no filtering is needed (archive)."""
    if opts.mode == MODE_ARCHIVE:
        return None

    parts: list[str] = []
    if should_deinterlace(info, opts):
        bwdif_mode = "send_field" if opts.mode == MODE_MP4 else "send_frame"
        parts.append(f"bwdif=mode={bwdif_mode}:parity={effective_parity(info, opts)}")
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
        if opts.preserve_color:
            # PAL camcorder material is BT.470BG. Without these tags players
            # fall back to BT.709 and the colours shift.
            if info.color_space:
                cmd += ["-colorspace", info.color_space]
            if info.color_primaries:
                cmd += ["-color_primaries", info.color_primaries]
            if info.color_trc:
                cmd += ["-color_trc", info.color_trc]
            if info.color_range:
                cmd += ["-color_range", info.color_range]
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
# Human-readable analysis
# --------------------------------------------------------------------------- #

_FIELD_ORDER_TEXT = {
    "tff": "interlaced, top field first (TFF)",
    "bff": "interlaced, bottom field first (BFF)",
    "progressive": "progressive",
}


def describe(path: str, ffprobe: str = "ffprobe") -> str:
    """Return a multi-line human-readable analysis of a source file.

    Used by the GUI's info panel and the CLI's --info flag so the user can see
    what they actually have — container, real codec, aspect, and above all the
    field order that drives deinterlacing.
    """
    lines: list[str] = [os.path.basename(path)]
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
        lines.append(f"  File          {size_mb:.1f} MB")
    except OSError:
        pass

    try:
        info = probe_video(path, ffprobe)
    except (FileNotFoundError, RuntimeError) as exc:
        lines.append(f"  Analysis failed: {exc}")
        return "\n".join(lines)

    container = info.container or "?"
    lines.append(f"  Container     {container}"
                 + ("   (a .MOD is really an MPEG-2 Program Stream)"
                    if "mpeg" in container.lower() else ""))
    lines.append(f"  Video         {info.codec or '?'} "
                 f"{info.width}x{info.height}")
    lines.append(f"  Aspect        SAR {info.sar.numerator}:{info.sar.denominator}"
                 f"  ->  DAR {info.dar}"
                 f"  ->  square-pixel output "
                 f"{square_pixel_size(info)[0]}x{square_pixel_size(info)[1]}")
    fps = info.fps
    lines.append(f"  Frame rate    {fps:g} fps"
                 + (f"  ->  {fps * 2:g} fps after field-doubling"
                    if info.is_interlaced else ""))
    lines.append(f"  Scan          {_FIELD_ORDER_TEXT.get(info.field_order, '?')}")
    if info.duration:
        lines.append(f"  Duration      {info.duration:.2f} s")
    colour = info.color_space or "untagged"
    lines.append(f"  Colour        {colour}"
                 + ("   (PAL)" if colour == "bt470bg" else ""))
    if info.audio_codec:
        bitrate = ""
        if info.audio_bitrate_src.isdigit():
            bitrate = f", {int(info.audio_bitrate_src) // 1000} kbit/s"
        lines.append(f"  Audio         {info.audio_codec}, "
                     f"{info.audio_rate or '?'} Hz, "
                     f"{info.audio_channels or '?'} ch{bitrate}")

    moi = moi_sidecar_path(path)
    if moi:
        stamp = read_moi_datetime(moi)
        lines.append(f"  Recorded      {stamp}   (from {os.path.basename(moi)})"
                     if stamp else
                     f"  Recorded      unreadable ({os.path.basename(moi)})")
    else:
        lines.append("  Recorded      no matching .MOI sidecar")

    if info.is_interlaced:
        lines.append(f"  -> Deinterlace with parity={info.parity}. "
                     f"If motion looks jerky or combed, force the other order.")
    return "\n".join(lines)


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

    resolved_ffmpeg = which_ffmpeg(ffmpeg)
    if resolved_ffmpeg is None:
        return ConvertResult(src, dst, False, f"{ffmpeg} not found (bundled or on PATH)")

    cmd = build_command(src, info, opts, creation_time, resolved_ffmpeg, progress_arg=True)
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
